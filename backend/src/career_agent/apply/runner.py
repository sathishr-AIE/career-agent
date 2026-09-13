"""AgentRun: one live `claude -p` session, stdin kept open (stream-json in
and out) so the human's answers reach the SAME session mid-form. The reader
thread turns the stream into events; turn boundaries (`result` messages)
decide whether the agent is waiting on us, finished, or needs a nudge."""
import json
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

from career_agent.apply.agent import (_USAGE_KEYS, _kill_tree, parse_ask,
                                      parse_confirm, strip_decoration,
                                      summarize_tool_input, user_message)

# Sent when a turn ends with no recognised line -- often a CONFIRM the parser
# refused. It must never read as "go ahead": with submission enabled, that
# would send an application nobody reviewed.
NUDGE = ("Continue. If you need something from the human, emit an ASK line. If you "
         "have filled the form, emit a CONFIRM line (one line, escape newlines as "
         "\\n) and wait -- never click Submit until a DECISION approve arrives "
         "(unless this run is pre-approved). Emit your RESULT line only when the run "
         "is finished.")
MALFORMED = (" Your last ASK/CONFIRM line was not valid JSON with the required keys "
             "-- re-emit it as one valid line; do not proceed.")
MAX_NUDGES = 3


@dataclass
class RunEvents:
    on_text: Callable[[str], None] = lambda s: None
    on_tool: Callable[[str, str], None] = lambda n, s: None
    on_turn_end: Callable[[float | None, dict], None] = lambda c, u: None
    on_ask: Callable[[dict], None] = lambda p: None
    on_confirm: Callable[[dict], None] = lambda p: None


class AgentRun:
    def __init__(self, cmd, cwd, env, nonce: str, events: RunEvents,
                 popen=None):
        self.cmd, self.cwd, self.env, self.nonce, self.events = cmd, cwd, env, nonce, events
        # Resolved per instance, not bound as a default at import time, so
        # a later patch of subprocess.Popen (tests/conftest.py's guard) wins.
        self._popen = popen or subprocess.Popen
        self._reader_thread: threading.Thread | None = None
        self.proc = None
        self.transcript = ""
        self._turn_buf: list[str] = []
        self._per_msg: dict = {}
        self.cost_total: float | None = None
        self.usage_total: dict = {}
        self.result_line: str | None = None
        self.done = threading.Event()
        self.waiting = threading.Event()   # ASK/CONFIRM emitted, turn ended
        self.nudges = 0
        self._lock = threading.Lock()       # send()'s check-and-clear only; never held across I/O
        # Only the writer thread touches proc.stdin: a blocking pipe write must
        # never stall the reader (nudges, close) or an HTTP answer's send().
        self._outbox: queue.Queue = queue.Queue()
        self._writer_thread: threading.Thread | None = None

    # -- process ------------------------------------------------------------
    def start(self, first_message: str) -> None:
        self.proc = self._popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=self.env, cwd=str(self.cwd), shell=False)
        # Queued before either thread runs: a reader that sees an early
        # RESULT queues the close, which must never overtake the prompt.
        self._write_user(first_message)
        # Reader FIRST: the CLI writes a large --verbose init line before it
        # reads stdin, so a write that waits on it with nobody draining
        # stdout deadlocks both processes on full pipes.
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        self._writer_thread = threading.Thread(target=self._writer, daemon=True)
        self._writer_thread.start()

    def _writer(self) -> None:
        while True:
            text = self._outbox.get()
            try:
                if text is None:
                    self.proc.stdin.close()
                    return
                self.proc.stdin.write(user_message(text))
                self.proc.stdin.flush()
            except Exception:
                if text is None:
                    return        # a dead child's pipe: nothing left to write to

    def _write_user(self, text: str) -> None:
        self._outbox.put(text)

    def send(self, text: str) -> bool:
        """Answer the agent's open ASK/CONFIRM. Refuses (False, nothing
        written) unless it is waiting on one: a stale or double answer must
        never land mid-turn."""
        with self._lock:
            if self.done.is_set() or not self.waiting.is_set():
                return False
            # Clear BEFORE writing: the reader may see the next turn's ASK the
            # instant the answer lands, and its set() must not be undone.
            self.waiting.clear()
            self._write_user(text)
            return True

    def _close_stdin(self) -> None:
        self._outbox.put(None)

    def kill(self) -> None:
        """Idempotent; a no-op kill for a process that already exited."""
        if self.proc is not None and self.proc.poll() is None:
            _kill_tree(self.proc.pid)
        self._close_stdin()
        self.done.set()

    def wait(self, timeout_s: float | None) -> bool:
        """True once the run is done AND stdin is flushed and closed (every
        done.set() is preceded by a close request)."""
        if not self.done.wait(timeout_s):
            return False
        if self._writer_thread is not None:
            self._writer_thread.join(timeout_s)
        return True

    # -- stream (same parsing rules as agent.consume_stream) ----------------
    def _append(self, line: str) -> None:
        self.transcript += line + "\n"
        self._turn_buf.append(line)

    def _reader(self) -> None:
        try:
            for raw in self.proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    self._append(raw)
                    continue
                t = msg.get("type")
                if t == "assistant":
                    self._add_usage(msg.get("message", {}))
                    for block in msg.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            self._append(block["text"])
                            self.events.on_text(block["text"])
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "").replace("mcp__playwright__", "")
                            summary = summarize_tool_input(block.get("input", {}))
                            self._append(f"  >> {name} {summary}")
                            self.events.on_tool(name, summary)
                elif t == "result":
                    cost = msg.get("total_cost_usd")
                    # ponytail: sums per-turn total_cost_usd per spec §3; if the CLI reports a process-cumulative total in multi-turn stream-json, take the LAST value per process instead (and sum across --resume processes) -- verify on the first live run.
                    if cost is not None:
                        self.cost_total = (self.cost_total or 0.0) + cost
                    if msg.get("result"):
                        self._append(msg["result"])
                    self.events.on_turn_end(cost, dict(self.usage_total))
                    self._end_of_turn()
        finally:
            # A raising callback ends this reader early: without EOF on
            # stdin, `claude` (and its Chrome) would idle on forever.
            self._close_stdin()
            self.done.set()

    def _add_usage(self, message: dict) -> None:
        # stream-json repeats a message's usage per content block: take the
        # largest per message id, as consume_stream does.
        usage = message.get("usage")
        if not usage:
            return
        seen = self._per_msg.setdefault(message.get("id") or object(), {})
        for k in _USAGE_KEYS:
            seen[k] = max(seen.get(k, 0), usage.get(k) or 0)
        self.usage_total = {k: sum(m.get(k, 0) for m in self._per_msg.values())
                            for k in _USAGE_KEYS}

    def _end_of_turn(self) -> None:
        """RESULT beats CONFIRM beats ASK within a turn; the last line of a
        kind wins. Only nonce-stamped lines count (see agent.result_prefix)."""
        turn, self._turn_buf = self._turn_buf, []
        lines = [strip_decoration(l) for t in turn for l in t.splitlines()]

        def last(kind):
            prefix = f"{kind}:{self.nonce}:"
            hits = [l for l in lines if l.startswith(prefix)]
            return hits[-1] if hits else None

        confirm, ask = last("CONFIRM"), last("ASK")
        res = last("RESULT")
        if res:
            # A pre-approved (auto) run CONFIRMs and submits in one turn: its
            # CONFIRM is still reported, or the send's answers are lost.
            payload = parse_confirm(confirm, self.nonce) if confirm else None
            if payload is not None:
                self.events.on_confirm(payload)
            self.result_line = res
            self._close_stdin()
            self.done.set()
            return
        # A malformed ASK/CONFIRM is no ask at all: nudge, and say why.
        if confirm:
            payload = parse_confirm(confirm, self.nonce)
            if payload is not None:
                self.waiting.set()
                self.events.on_confirm(payload)
                return
        elif ask:
            payload = parse_ask(ask, self.nonce)
            if payload is not None:
                self.waiting.set()
                self.events.on_ask(payload)
                return
        if self.nudges < MAX_NUDGES:
            self.nudges += 1
            self._write_user(NUDGE + (MALFORMED if confirm or ask else ""))
            return
        self._close_stdin()
        self.done.set()
