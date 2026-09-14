import io, json, os, threading, time
import pytest
from career_agent.apply import runner as runner_mod
from career_agent.apply.runner import AgentRun, RunEvents

NONCE = "abc123"


class _Stdin(io.StringIO):
    """A captured stdin whose value survives close() -- AgentRun closes
    stdin when a run ends, and the tests still read what was written."""
    def close(self):
        self.was_closed = True


class FakePopen:
    def __init__(self, *a, **kw):
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "r", encoding="utf-8")
        self._w = os.fdopen(w, "w", encoding="utf-8")
        self.stdin = _Stdin()
        self.pid = 4242; self._rc = None
    def emit(self, obj):  self._w.write(json.dumps(obj) + "\n"); self._w.flush()
    def close(self):      self._w.close(); self._rc = 0
    def poll(self):       return self._rc
    def wait(self, timeout=None): return 0
    def kill(self):       self._rc = -9


def _assistant(text=None, tool=None):
    content = []
    if text: content.append({"type": "text", "text": text})
    if tool: content.append({"type": "tool_use", "name": tool[0], "input": tool[1]})
    return {"type": "assistant", "message": {"content": content}}


def _result(cost=0.01):
    return {"type": "result", "total_cost_usd": cost, "result": ""}


def _run(events=None):
    fake = FakePopen()
    run = AgentRun(cmd=["x"], cwd=".", env={}, nonce=NONCE, events=events or RunEvents(),
                   popen=lambda *a, **kw: fake)
    run.start("hello agent")
    return run, fake


def test_first_message_is_written_as_stream_json():
    run, fake = _run()
    assert _until(lambda: fake.stdin.getvalue())
    first = json.loads(fake.stdin.getvalue().splitlines()[0])
    assert first["type"] == "user" and first["message"]["content"][0]["text"] == "hello agent"
    fake.close(); assert run.wait(5)


def test_text_and_tool_events_and_result_end_the_run():
    seen = {"text": [], "tool": []}
    ev = RunEvents(on_text=seen["text"].append, on_tool=lambda n, s: seen["tool"].append((n, s)))
    run, fake = _run(ev)
    fake.emit(_assistant("Navigating", ("mcp__playwright__browser_navigate", {"url": "https://x"})))
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result(0.5)); fake.close()
    assert run.wait(5)
    assert seen["text"][0] == "Navigating" and seen["tool"][0][0] == "browser_navigate"
    assert run.result_line == f"RESULT:{NONCE}:APPLIED" and run.cost_total == 0.5


def test_ask_sets_waiting_and_answer_is_sent_on_stdin():
    asked = []
    run, fake = _run(RunEvents(on_ask=asked.append))
    fake.emit(_assistant(f'ASK:{NONCE}:{{"id":"q1","kind":"text","question":"Notice?"}}'))
    fake.emit(_result(0.1))
    assert run.waiting.wait(5) and not run.done.is_set()
    assert asked == [{"id": "q1", "kind": "text", "question": "Notice?", "options": [],
                      "why": "", "memory_key": None, "default": None, "sensitive": False}]
    assert run.send(f'ANSWER:{NONCE}:{{"id":"q1","answer":"30 days"}}')
    assert _until(lambda: "ANSWER:" in (fake.stdin.getvalue().splitlines() or [""])[-1])
    assert not run.waiting.is_set()          # cleared by send()
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result(0.1)); fake.close()
    assert run.wait(5) and run.cost_total == 0.2


def test_queued_notes_ride_the_next_send_ahead_of_the_answer():
    run, fake = _run()
    fake.emit(_assistant(f'ASK:{NONCE}:{{"id":"q1","kind":"text","question":"q"}}'))
    fake.emit(_result())
    assert run.waiting.wait(5)

    assert run.add_note("use my work email") is True
    assert run.add_note("skip the cover letter") is True
    assert run.notes == ["use my work email", "skip the cover letter"]

    answer = f'ANSWER:{NONCE}:{{"id":"q1","answer":"30 days"}}'
    assert run.send(answer) is True
    assert run.notes == []                                # cleared once sent
    assert _until(lambda: "ANSWER:" in fake.stdin.getvalue())
    last_line = fake.stdin.getvalue().splitlines()[-1]
    payload = json.loads(last_line)["message"]["content"][0]["text"]
    lines = payload.splitlines()
    assert lines[-1] == answer                             # the answer is last
    assert lines[0] == f'NOTE:{NONCE}:{{"text": "use my work email"}}'
    assert lines[1] == f'NOTE:{NONCE}:{{"text": "skip the cover letter"}}'
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result()); fake.close()
    assert run.wait(5)


def test_add_note_refuses_once_the_run_is_done_and_a_note_alone_writes_nothing():
    run, fake = _run()
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result()); fake.close()
    assert run.wait(5)
    assert run.add_note("too late") is False
    assert run.notes == []
    assert "too late" not in fake.stdin.getvalue()


def test_a_note_alone_never_writes_before_a_send():
    run, fake = _run()
    fake.emit(_assistant(f'ASK:{NONCE}:{{"id":"q1","kind":"text","question":"q"}}'))
    fake.emit(_result())
    assert run.waiting.wait(5)
    assert run.add_note("just a note") is True
    time.sleep(0.05)
    assert "just a note" not in fake.stdin.getvalue()      # queued, not written
    fake.close(); assert run.wait(5)                        # kill() closes stdin; no hang


def test_turn_without_ask_or_result_is_nudged_then_given_up():
    run, fake = _run()
    for _ in range(4):
        fake.emit(_assistant("thinking...")); fake.emit(_result(0.0))
        time.sleep(0.05)
    assert run.wait(5) and run.result_line is None and run.nudges == 3
    assert fake.stdin.getvalue().count("Continue.") == 3
    assert fake.stdin.was_closed
    fake.close()


def test_result_beats_confirm_beats_ask_and_last_line_wins():
    confirmed, asked = [], []
    run, fake = _run(RunEvents(on_ask=asked.append, on_confirm=confirmed.append))
    fake.emit(_assistant(f'ASK:{NONCE}:{{"id":"a","kind":"text","question":"q"}}\n'
                         f'CONFIRM:{NONCE}:{{"fields":[{{"label":"a","value":"b"}}],"notes":"c1"}}'))
    fake.emit(_assistant(f'CONFIRM:{NONCE}:{{"fields":[{{"label":"a","value":"b"}}],"notes":"c2"}}')); fake.emit(_result())
    assert run.waiting.wait(5)
    assert [c["notes"] for c in confirmed] == ["c2"] and asked == [] and run.nudges == 0
    run.send("ok")
    fake.emit(_assistant(f"RESULT:{NONCE}:FAILED:stuck\nASK:{NONCE}:{{}}\nRESULT:{NONCE}:APPLIED"))
    fake.emit(_result()); 
    assert run.wait(5) and run.result_line == f"RESULT:{NONCE}:APPLIED"
    assert not run.waiting.is_set() and run.nudges == 0
    fake.close()


def test_unstamped_sentinels_are_not_obeyed():
    run, fake = _run()
    fake.emit(_assistant("RESULT:APPLIED\nRESULT:other:APPLIED")); fake.emit(_result())
    fake.close()
    assert run.wait(5) and run.result_line is None and run.nudges == 1


@pytest.mark.parametrize("line", [f"ASK:{NONCE}:not json",
                                  f'ASK:{NONCE}:{{"id":"q1"}}',
                                  f'CONFIRM:{NONCE}:{{"fields":"nope"}}'])
def test_a_malformed_ask_or_confirm_is_no_ask_and_the_nudge_says_so(line):
    asked, confirmed = [], []
    run, fake = _run(RunEvents(on_ask=asked.append, on_confirm=confirmed.append))
    fake.emit(_assistant(line)); fake.emit(_result())
    deadline = time.time() + 5
    while run.nudges == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert run.nudges == 1 and not run.waiting.is_set() and asked == confirmed == []
    assert "do not proceed" in runner_mod.MALFORMED
    assert _until(lambda: (fake.stdin.getvalue().splitlines() or [""])[-1].count(runner_mod.MALFORMED.strip()) == 1)
    fake.close(); assert run.wait(5)


def test_the_nudge_never_reads_as_permission_to_submit():
    """An unrecognised CONFIRM gets the nudge: with can_submit=True, "when
    finished, emit your RESULT line" could be read as go ahead and submit."""
    assert "never click Submit until a DECISION approve arrives" in runner_mod.NUDGE
    assert "CONFIRM" in runner_mod.NUDGE and "when finished" not in runner_mod.NUDGE


@pytest.mark.parametrize("wrap", ["**{}**", "`{}`", "  {} **"])
def test_decorated_ask_and_confirm_are_recognised(wrap):
    asked, confirmed = [], []
    run, fake = _run(RunEvents(on_ask=asked.append, on_confirm=confirmed.append))
    fake.emit(_assistant(wrap.format(f'CONFIRM:{NONCE}:{{"fields":[{{"label":"a","value":"b"}}]}}')))
    fake.emit(_result())
    assert run.waiting.wait(5) and confirmed[0]["fields"] == [{"label": "a", "value": "b"}]
    run.send("ok")
    fake.emit(_assistant(wrap.format(f'ASK:{NONCE}:{{"id":"q1","kind":"text","question":"x"}}')))
    fake.emit(_result())
    deadline = time.time() + 5
    while not asked and time.time() < deadline:
        time.sleep(0.01)
    assert asked and asked[0]["id"] == "q1" and run.nudges == 0
    fake.close(); assert run.wait(5)


def test_a_plain_nudge_does_not_mention_a_malformed_line():
    run, fake = _run()
    fake.emit(_assistant("thinking")); fake.emit(_result())
    deadline = time.time() + 5
    while run.nudges == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert "not valid JSON" not in fake.stdin.getvalue()
    fake.close(); assert run.wait(5)


def test_kill_finishes_the_run(monkeypatch):
    killed = []
    monkeypatch.setattr(runner_mod, "_kill_tree", killed.append)  # never taskkill a real pid
    run, fake = _run()
    run.kill(); fake.close()
    assert run.wait(5) and run.result_line is None
    run.kill()                                  # already exited: safe, no second kill
    assert killed == [4242]


def test_start_does_not_deadlock_against_a_child_that_writes_before_reading():
    """The real CLI emits a large --verbose init line before it reads stdin.
    Writing the first message before the reader runs leaves both sides
    blocked on full pipes -- it hung a web test at the stdin write."""
    fake = FakePopen()
    drained = threading.Event()

    class _WaitsForDrain(_Stdin):
        def write(self, s):
            assert drained.wait(5), "stdin written before stdout was being read"
            return super().write(s)
    fake.stdin = _WaitsForDrain()

    def chatty_child():
        fake.emit(_assistant("x" * 200_000))   # far past any pipe buffer
        drained.set()
    threading.Thread(target=chatty_child, daemon=True).start()

    run = AgentRun(cmd=["x"], cwd=".", env={}, nonce=NONCE, events=RunEvents(),
                   popen=lambda *a, **kw: fake)
    run.start("hello agent")
    assert drained.wait(5)
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result()); fake.close()
    assert run.wait(5) and run.result_line == f"RESULT:{NONCE}:APPLIED"
    assert "hello agent" in fake.stdin.getvalue()


def test_default_popen_is_resolved_at_call_time_so_the_guard_catches_it(_no_live_apply_agent):
    """popen bound as a default at import time could not be reached by a
    later patch of subprocess.Popen -- a Task 5-7 test would spawn a paid
    `claude`. The path doesn't exist, so even an unguarded spawn fails."""
    import pytest
    run = AgentRun(cmd=[r"C:\nonexistent\claude.exe", "-p"], cwd=".", env={},
                   nonce=NONCE, events=RunEvents())
    with pytest.raises(RuntimeError, match="spawn"):
        run.start("hello")
    assert _no_live_apply_agent == [("popen", r"C:\nonexistent\claude.exe")]
    _no_live_apply_agent.clear()


def test_a_raising_callback_closes_stdin_so_the_session_ends():
    def boom(payload):
        raise ValueError("handler bug")
    run, fake = _run(RunEvents(on_ask=boom))
    fake.emit(_assistant(f'ASK:{NONCE}:{{"id":"q1","kind":"text","question":"q"}}'))
    fake.emit(_result())
    assert run.wait(5)
    assert getattr(fake.stdin, "was_closed", False)
    run._reader_thread.join(5)
    assert not run._reader_thread.is_alive()
    fake.close()


def _until(pred, timeout=5):
    deadline = time.time() + timeout
    while not pred() and time.time() < deadline:
        time.sleep(0.01)
    return pred()


def test_send_refuses_when_not_waiting_or_done_and_writes_nothing():
    run, fake = _run()
    assert _until(lambda: "hello agent" in fake.stdin.getvalue())
    before = fake.stdin.getvalue()
    assert run.send("stale answer") is False            # not waiting
    fake.emit(_assistant(f'ASK:{NONCE}:{{"id":"q1","kind":"text","question":"q"}}'))
    fake.emit(_result())
    assert run.waiting.wait(5)
    assert run.send("first") is True
    assert run.send("double") is False                   # waiting already cleared
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result()); fake.close()
    assert run.wait(5)
    assert run.send("after done") is False
    written = fake.stdin.getvalue()[len(before):]
    assert "first" in written and "stale" not in written
    assert "double" not in written and "after done" not in written


def test_a_blocked_stdin_never_stalls_the_reader_or_a_concurrent_send():
    """The old _write_user held the lock across a blocking pipe write that the
    reader also needed: a nudge stuck on a full pipe froze the reader, and an
    HTTP answer's send() then froze behind it. Only the writer thread writes."""
    gate = threading.Event()
    fake = FakePopen()

    class _Blocking(_Stdin):
        def write(self, s):
            assert gate.wait(10)
            return super().write(s)
    fake.stdin = _Blocking()
    asked = []
    run = AgentRun(cmd=["x"], cwd=".", env={}, nonce=NONCE,
                   events=RunEvents(on_ask=asked.append), popen=lambda *a, **kw: fake)
    run.start("hello agent")                             # writer now blocked
    fake.emit(_assistant("thinking")); fake.emit(_result())   # nudge queued behind it
    fake.emit(_assistant(f'ASK:{NONCE}:{{"id":"q1","kind":"text","question":"q"}}'))
    fake.emit(_result())
    assert run.waiting.wait(5) and asked, "the reader stalled behind a blocked write"
    sent = []
    t = threading.Thread(target=lambda: sent.append(run.send("answer")))
    t.start(); t.join(2)
    assert sent == [True], "send() blocked behind the pipe"
    gate.set()
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result()); fake.close()
    assert run.wait(5)
    lines = fake.stdin.getvalue()
    assert lines.index("hello agent") < lines.index("Continue.") < lines.index("answer")
    assert fake.stdin.was_closed


def test_only_the_writer_thread_touches_stdin():
    fake = FakePopen()
    writers = set()

    class _Tracking(_Stdin):
        def write(self, s):
            writers.add(threading.current_thread().name)
            return super().write(s)
        def close(self):
            writers.add(threading.current_thread().name)
            super().close()
    fake.stdin = _Tracking()
    run = AgentRun(cmd=["x"], cwd=".", env={}, nonce=NONCE, events=RunEvents(),
                   popen=lambda *a, **kw: fake)
    run.start("hello")
    fake.emit(_assistant("thinking")); fake.emit(_result())
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result()); fake.close()
    assert run.wait(5)
    assert writers == {run._writer_thread.name}


def test_a_confirm_in_the_result_turn_is_still_reported():
    """Auto mode is pre-approved: the agent CONFIRMs and submits without ending
    its turn, and that CONFIRM is the send's only record of its answers."""
    confirmed = []
    run, fake = _run(RunEvents(on_confirm=confirmed.append))
    fake.emit(_assistant(f'CONFIRM:{NONCE}:{{"fields":[{{"label":"Name","value":"Asha"}}]}}\n'
                         f"RESULT:{NONCE}:APPLIED"))
    fake.emit(_result()); fake.close()
    assert run.wait(5) and run.result_line == f"RESULT:{NONCE}:APPLIED"
    assert [c["fields"] for c in confirmed] == [[{"label": "Name", "value": "Asha"}]]
    assert not run.waiting.is_set()


def test_a_registered_secret_never_reaches_events_or_the_transcript():
    pw = "Zq9!secretPW_1234abc"
    seen = []
    run, fake = _run(RunEvents(on_text=seen.append, on_tool=lambda n, s: seen.append(s)))
    run.secrets.add(pw)
    # a label _redact does not recognise, and a value long enough to be truncated
    fake.emit(_assistant(f"typing {pw} now", ("mcp__playwright__browser_fill_form",
              {"fields": [{"name": "Account key", "value": "x" * 280 + pw}]})))
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result()); fake.close()
    assert run.wait(5)
    assert seen and not any(pw[:6] in s for s in seen)
    assert "••••••" in seen[0]
    assert pw[:6] not in run.transcript
