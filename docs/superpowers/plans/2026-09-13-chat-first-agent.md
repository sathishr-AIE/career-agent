# Chat-First Career Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a chat the portal's primary interface, where the apply agent works live, asks questions as answerable cards, remembers answers, resumes after interruption, and clicks Apply only after the human approves the exact submission.

**Architecture:** A DB-backed message log (`conversation`/`message`/`agent_prompt`) that the React chat polls; an in-process `AgentRun` supervisor that keeps each `claude -p` session's stdin open (stream-json in/out) and relays ASK/CONFIRM out and ANSWER/DECISION in; checkpoints keyed by job for `--resume`; `qa_bank` extended into keyed memory; a Fernet credential store; a richer profile; and a structured-output intent router for the Home chat. Everything runs inside the existing FastAPI process — no new servers.

**Tech Stack:** Python 3.13, FastAPI, SQLite (WAL), `claude` CLI (`--input-format stream-json --output-format stream-json`, `--session-id`, `--resume`, `--json-schema`), `@playwright/mcp` over CDP, `cryptography` (Fernet), pydantic v2, tomlkit; React 19 + react-router 7 + plain `fetch` (`frontend/src/api.ts`), Vite, `tsc -b` for type-checking.

**Spec:** `docs/superpowers/specs/2026-09-13-chat-first-agent-design.md` — executors read it; the protocol (§3) and data model (§2) there are the contracts every task below implements.

## Global Constraints

- Run Python/pytest from `backend/` with the project venv: `cd backend && .venv/Scripts/python.exe -m pytest -q` (asyncio_mode=auto; async tests need no marker). Full suite is ~500 tests, ~4 min. In a fresh git worktree first run `uv venv && uv pip install -e ".[dev]"` inside `backend/`, and `npm install` inside `frontend/` for `tsc -b`.
- Schema: new tables go in `db.py`'s `SCHEMA` (`CREATE TABLE IF NOT EXISTS`); new columns on EXISTING tables go ONLY through `_add_column_if_missing`. Every request re-runs `init_schema`.
- `SUBMISSION_IMPLEMENTED` stays `False` in every commit. The kill switch, the `application` status CHECK constraint, `one_live_application_per_job`, `BLOCKING`, the nonce contract (`result_prefix`), the sandbox flags in `build_cmd` (`--tools ""`, `--strict-mcp-config`, `--disallowedTools mcp__playwright__browser_run_code_unsafe`), and redacted tool-input logging are not weakened by any task.
- **No test spawns a browser, spawns `claude`/`npx`, or spends API credits.** Subprocess behaviour is tested through a fake `Popen` (pipes) or the injected `run_agent` seam.
- Datetimes stay naive UTC to match SQLite `datetime('now')`.
- Do not copy text from the ApplyPilot project (AGPL).
- Frontend: TypeScript strict; type-check with `cd frontend && npx tsc -b` (a `vite build` is blocked on this machine by WDAC; `npm run dev` works). Keep the existing `Glass`/`ui.css` look; no new npm dependencies.
- Commit messages end with: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
- Protocol strings are exact and shared: `ASK:<nonce>:`, `CONFIRM:<nonce>:`, `RESULT:<nonce>:`, `ANSWER:<nonce>:`, `DECISION:<nonce>:`, `CONTINUE:<nonce>:` (spec §3).

## File Structure

```
backend/src/career_agent/
  chat.py                 NEW  conversation/message/agent_prompt store + context dicts + backfill
  apply/runner.py         NEW  AgentRun: subprocess supervisor, stream-json in/out, ASK/CONFIRM relay
  apply/checkpoint.py     NEW  apply_checkpoint store + resume selection
  apply/agent.py          MOD  protocol sections, parse_ask/parse_confirm, build_prompt(mode, can_submit), build_cmd(session_id, resume)
  apply/ats.py            MOD  submit(mode=...) using AgentRun; in_flight-first outcome recording
  credentials.py          NEW  Fernet encrypt/decrypt + site_credential store
  security.py             NEW  key loading (CREDENTIAL_KEY)
  config.py               MOD  CandidateProfile extension (Address, WorkEntry, EduEntry)
  store.py                MOD  qa_bank memory helpers (qa_remember, qa_by_key, qa_touch)
  web/api_chat.py         NEW  /api/chat/* routes
  web/api_memory.py       NEW  /api/memory/* routes
  web/api_credentials.py  NEW  /api/logins/* routes
  web/api_profile.py      NEW  /api/profile routes
  web/intent.py           NEW  Home-chat intent router (claude -p --json-schema)
  web/actions.py          MOD  answer_prompt, resume_job, home_message; submit(mode) call sites
  web/worker.py           MOD  apply_tick uses mode; resumable checkpoints
  web/app.py              MOD  mount api_* routers; runner registry lifecycle
frontend/src/
  routes/Chat.tsx               NEW  chat shell (sidebar + messages + composer + drawer rail)
  components/chat/MessageList.tsx, Composer.tsx, PromptCard.tsx, ConfirmCard.tsx, Drawer.tsx  NEW
  routes/Memory.tsx, Logins.tsx, Profile.tsx   NEW  drawer pages
  api.ts                        MOD  chat/memory/credential/profile types
  main.tsx                      MOD  routes: "/" -> Chat, "/chat/:id"; drawers keep old routes
```

## Parallelization map (for dispatching-parallel-agents)

- **Phase A — serial, one worktree/branch:** Tasks 1–11 (S0 → S1 → S2 → S3). Each depends on the previous one's files.
- **Phase B — parallel, one isolated worktree each, disjoint files:** Task 12+13 (S4 memory), Task 14+15 (S5 credentials), Task 16+17 (S6 profile). They add NEW modules/routes/pages and their own prompt-section *functions*; they do **not** edit `build_prompt`'s section list, `app.py`, or `main.tsx` — Task 18 wires those.
- **Phase C — serial:** Task 18 (integration), 19–20 (S7), 21 (docs).

---

## Slice S0 — Chat shell + message log

### Task 1: Chat tables and store (`chat.py`)

**Files:**
- Modify: `backend/src/career_agent/db.py` (append three tables to `SCHEMA`)
- Create: `backend/src/career_agent/chat.py`
- Test: `backend/tests/test_chat.py`

**Interfaces:**
- Produces: `chat.home_conversation(conn) -> int`, `chat.conversation_for_job(conn, job_id) -> int` (creates on first use, title `"<company> — <title>"`), `chat.post_message(conn, conversation_id, role, content, payload: dict|None=None) -> int`, `chat.messages_after(conn, conversation_id, after_id: int = 0, limit: int = 200) -> list[dict]` (dicts: id, role, content, payload (parsed or None), created_at), `chat.list_conversations(conn) -> list[dict]` (id, kind, job_id, title, updated_at, last_message), `chat.backfill_job(conn, job_id) -> int` (number of system messages created from `application`/`event` rows the first time; idempotent via a `backfilled` marker message), `chat.open_prompt(conn, job_id, kind, payload) -> int` (inserts `agent_prompt` + a `message(role='prompt', payload={"prompt_id":..})`), `chat.answer_prompt_row(conn, prompt_id, answer: dict) -> dict` (sets status answered, returns the row), `chat.open_prompt_for_job(conn, job_id) -> dict|None`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_chat.py
import json
import pytest
from career_agent import chat, db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized, url)"
              " VALUES ('fp','linkedin','1','Acme','acme','AI Engineer',"
              " 'aiengineer','https://x/1')")
    c.commit()
    return c


def test_tables_exist(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"conversation", "message", "agent_prompt"} <= names


def test_home_conversation_is_a_singleton(conn):
    assert chat.home_conversation(conn) == chat.home_conversation(conn)
    assert conn.execute("SELECT COUNT(*) FROM conversation WHERE kind='home'").fetchone()[0] == 1


def test_job_conversation_created_once_with_title(conn):
    a = chat.conversation_for_job(conn, 1)
    b = chat.conversation_for_job(conn, 1)
    assert a == b
    row = conn.execute("SELECT kind, job_id, title FROM conversation WHERE id=?", (a,)).fetchone()
    assert (row["kind"], row["job_id"], row["title"]) == ("job", 1, "Acme — AI Engineer")


def test_post_and_read_messages_after_cursor(conn):
    cid = chat.conversation_for_job(conn, 1)
    m1 = chat.post_message(conn, cid, "agent", "Navigating…")
    m2 = chat.post_message(conn, cid, "system", "run started", {"job_id": 1})
    got = chat.messages_after(conn, cid, after_id=m1)
    assert [m["id"] for m in got] == [m2]
    assert got[0]["payload"] == {"job_id": 1}
    assert got[0]["role"] == "system"


def test_invalid_role_is_rejected_by_schema(conn):
    cid = chat.home_conversation(conn)
    with pytest.raises(Exception):
        chat.post_message(conn, cid, "robot", "x")


def test_backfill_renders_events_once(conn):
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (1, 'needs_answer', 'Visa?')")
    conn.execute("INSERT INTO application (job_id, resume_version, status, failure_reason)"
                 " VALUES (1, 'base-v1', 'failed', 'timeout')")
    conn.commit()
    n = chat.backfill_job(conn, 1)
    assert n == 2
    assert chat.backfill_job(conn, 1) == 0          # idempotent
    texts = [m["content"] for m in chat.messages_after(conn, chat.conversation_for_job(conn, 1))]
    assert any("needs_answer" in t and "Visa?" in t for t in texts)
    assert any("failed" in t and "timeout" in t for t in texts)


def test_open_prompt_creates_row_and_prompt_message(conn):
    pid = chat.open_prompt(conn, 1, "choice", {"id": "q1", "question": "Notice?", "options": ["30", "60"]})
    row = chat.open_prompt_for_job(conn, 1)
    assert row["id"] == pid and row["status"] == "open" and row["kind"] == "choice"
    msgs = chat.messages_after(conn, chat.conversation_for_job(conn, 1))
    assert msgs[-1]["role"] == "prompt" and msgs[-1]["payload"]["prompt_id"] == pid
    answered = chat.answer_prompt_row(conn, pid, {"answer": "30"})
    assert answered["status"] == "answered" and json.loads(answered["answer"]) == {"answer": "30"}
    assert chat.open_prompt_for_job(conn, 1) is None


def test_list_conversations_has_last_message_and_order(conn):
    home = chat.home_conversation(conn)
    cid = chat.conversation_for_job(conn, 1)
    chat.post_message(conn, cid, "agent", "latest")
    rows = chat.list_conversations(conn)
    assert rows[0]["id"] == cid and rows[0]["last_message"] == "latest"
    assert any(r["id"] == home and r["kind"] == "home" for r in rows)
```

- [ ] **Step 2: Run to verify failure** — `cd backend && .venv/Scripts/python.exe -m pytest tests/test_chat.py -q` → FAIL (`ModuleNotFoundError: career_agent.chat`).

- [ ] **Step 3: Add the tables to `SCHEMA` in `db.py`** (append inside the `SCHEMA` string, before its closing quotes):

```sql
CREATE TABLE IF NOT EXISTS conversation (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('home','job')),
    job_id     INTEGER UNIQUE REFERENCES job(id),
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS message (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversation(id),
    role            TEXT NOT NULL CHECK (role IN ('user','agent','system','prompt')),
    content         TEXT NOT NULL,
    payload         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS message_conv_id ON message(conversation_id, id);

CREATE TABLE IF NOT EXISTS agent_prompt (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES job(id),
    conversation_id INTEGER NOT NULL REFERENCES conversation(id),
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','answered','expired')),
    answer          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at     TEXT
);
```

- [ ] **Step 4: Implement `chat.py`**

```python
"""Chat data layer: conversations (one Home, one per job), their messages,
and the agent's open questions (agent_prompt). The React chat polls
messages_after(); the agent runner posts into it; nothing else writes here."""
import json
import sqlite3

_BACKFILL_MARK = "__backfilled__"


def _touch(conn, conversation_id: int) -> None:
    conn.execute("UPDATE conversation SET updated_at = datetime('now') WHERE id = ?",
                 (conversation_id,))


def home_conversation(conn) -> int:
    row = conn.execute("SELECT id FROM conversation WHERE kind = 'home'").fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO conversation (kind, title) VALUES ('home', 'Home')")
    conn.commit()
    return cur.lastrowid


def conversation_for_job(conn, job_id: int) -> int:
    row = conn.execute("SELECT id FROM conversation WHERE job_id = ?", (job_id,)).fetchone()
    if row:
        return row["id"]
    job = conn.execute("SELECT company, title FROM job WHERE id = ?", (job_id,)).fetchone()
    title = f"{job['company']} — {job['title']}" if job else f"Job #{job_id}"
    cur = conn.execute(
        "INSERT INTO conversation (kind, job_id, title) VALUES ('job', ?, ?)", (job_id, title))
    conn.commit()
    return cur.lastrowid


def post_message(conn, conversation_id: int, role: str, content: str,
                 payload: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO message (conversation_id, role, content, payload) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, json.dumps(payload) if payload is not None else None))
    _touch(conn, conversation_id)
    conn.commit()
    return cur.lastrowid


def _row_to_message(r) -> dict:
    return {"id": r["id"], "role": r["role"], "content": r["content"],
            "payload": json.loads(r["payload"]) if r["payload"] else None,
            "created_at": r["created_at"]}


def messages_after(conn, conversation_id: int, after_id: int = 0, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM message WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT ?",
        (conversation_id, after_id, limit)).fetchall()
    return [_row_to_message(r) for r in rows if r["content"] != _BACKFILL_MARK]


def list_conversations(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id, c.kind, c.job_id, c.title, c.updated_at,"
        "  (SELECT content FROM message m WHERE m.conversation_id = c.id"
        "     AND m.content != ? ORDER BY m.id DESC LIMIT 1) AS last_message"
        " FROM conversation c ORDER BY c.updated_at DESC, c.id DESC",
        (_BACKFILL_MARK,)).fetchall()
    return [dict(r) for r in rows]


def backfill_job(conn, job_id: int) -> int:
    """Render a job's pre-chat history (events, application rows) as system
    messages, once. The marker message is hidden by messages_after."""
    cid = conversation_for_job(conn, job_id)
    if conn.execute("SELECT 1 FROM message WHERE conversation_id = ? AND content = ?",
                    (cid, _BACKFILL_MARK)).fetchone():
        return 0
    n = 0
    for e in conn.execute("SELECT type, payload, occurred_at FROM event WHERE job_id = ?"
                          " ORDER BY id", (job_id,)):
        post_message(conn, cid, "system", f"{e['type']}: {e['payload'] or ''}".strip(": "),
                     {"occurred_at": e["occurred_at"]})
        n += 1
    for a in conn.execute("SELECT id, status, failure_reason, transcript_path FROM application"
                          " WHERE job_id = ? ORDER BY id", (job_id,)):
        reason = f" ({a['failure_reason']})" if a["failure_reason"] else ""
        post_message(conn, cid, "system", f"application #{a['id']}: {a['status']}{reason}",
                     {"application_id": a["id"], "transcript_path": a["transcript_path"]})
        n += 1
    conn.execute("INSERT INTO message (conversation_id, role, content) VALUES (?, 'system', ?)",
                 (cid, _BACKFILL_MARK))
    conn.commit()
    return n


def open_prompt(conn, job_id: int, kind: str, payload: dict) -> int:
    cid = conversation_for_job(conn, job_id)
    cur = conn.execute(
        "INSERT INTO agent_prompt (job_id, conversation_id, kind, payload) VALUES (?, ?, ?, ?)",
        (job_id, cid, kind, json.dumps(payload)))
    pid = cur.lastrowid
    post_message(conn, cid, "prompt", payload.get("question", kind), {"prompt_id": pid, "kind": kind})
    return pid


def open_prompt_for_job(conn, job_id: int):
    return conn.execute("SELECT * FROM agent_prompt WHERE job_id = ? AND status = 'open'"
                        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


def answer_prompt_row(conn, prompt_id: int, answer: dict):
    conn.execute("UPDATE agent_prompt SET status = 'answered', answer = ?,"
                 " answered_at = datetime('now') WHERE id = ? AND status = 'open'",
                 (json.dumps(answer), prompt_id))
    conn.commit()
    return conn.execute("SELECT * FROM agent_prompt WHERE id = ?", (prompt_id,)).fetchone()
```

- [ ] **Step 5: Run tests** → PASS; then full suite once. **Step 6: Commit** `feat(chat): conversation, message and agent_prompt tables with store`.

### Task 2: Chat API (`web/api_chat.py`) and mount

**Files:**
- Create: `backend/src/career_agent/web/api_chat.py`
- Modify: `backend/src/career_agent/web/app.py` (one `app.include_router(api_chat.router)` next to the existing `include_router`)
- Test: `backend/tests/test_api_chat.py` (uses the `client` fixture pattern from `tests/test_web.py`; copy that fixture's DB setup into this file's own fixture — do not import it)

**Interfaces:**
- Produces routes: `GET /api/chat/conversations` → `{conversations:[...], home_id:int}` (backfills every job that has an application or event on first call); `GET /api/chat/{cid}/messages?after=0` → `{messages:[...], open_prompt: {...}|null}` (open_prompt only for job conversations: the `agent_prompt` row with parsed payload); `POST /api/chat/{cid}/messages` body `{text}` → posts a `user` message and returns `{ok, message_id}` (Home routing arrives in Task 20; for now Home replies with a system message "Commands arrive in a later slice").
- Consumes: Task 1 functions.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_api_chat.py
from career_agent import chat


def test_conversations_include_home_and_backfilled_job(client, conn):
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (1, 'human_applied', NULL)")
    conn.commit()
    r = client.get("/api/chat/conversations").json()
    kinds = {c["kind"] for c in r["conversations"]}
    assert kinds == {"home", "job"} and r["home_id"]
    job_conv = next(c for c in r["conversations"] if c["kind"] == "job")
    msgs = client.get(f"/api/chat/{job_conv['id']}/messages").json()["messages"]
    assert any("human_applied" in m["content"] for m in msgs)


def test_post_user_message_and_cursor(client, conn):
    home = client.get("/api/chat/conversations").json()["home_id"]
    mid = client.post(f"/api/chat/{home}/messages", json={"text": "hello"}).json()["message_id"]
    after = client.get(f"/api/chat/{home}/messages?after={mid}").json()["messages"]
    assert all(m["id"] > mid for m in after)


def test_open_prompt_is_surfaced(client, conn):
    pid = chat.open_prompt(conn, 1, "text", {"id": "q1", "question": "Notice period?"})
    cid = chat.conversation_for_job(conn, 1)
    r = client.get(f"/api/chat/{cid}/messages").json()
    assert r["open_prompt"]["id"] == pid and r["open_prompt"]["payload"]["question"] == "Notice period?"
```

(The fixture: replicate `tests/test_web.py`'s `client` fixture and additionally `yield`/return the `conn` — simplest: make the fixture return `(client, conn)` via two fixtures sharing `tmp_path`; the `client` fixture must `monkeypatch.setattr(web, "DB_PATH", path)` exactly as test_web does.)

- [ ] **Step 2: Run → FAIL (404).**
- [ ] **Step 3: Implement**

```python
# backend/src/career_agent/web/api_chat.py
"""/api/chat/*: the React chat's only backend. Same deferred-import rule as
api.py: app.py mounts this router, so app.py is imported at call time."""
import json
from fastapi import APIRouter, Body

from career_agent import chat

router = APIRouter(prefix="/api/chat")


def _app():
    from career_agent.web import app as app_module
    return app_module


@router.get("/conversations")
def api_conversations():
    conn = _app()._conn()
    home_id = chat.home_conversation(conn)
    for r in conn.execute("SELECT DISTINCT job_id FROM ("
                          "SELECT job_id FROM application UNION SELECT job_id FROM event"
                          " WHERE job_id IS NOT NULL)"):
        chat.backfill_job(conn, r["job_id"])
    return {"conversations": chat.list_conversations(conn), "home_id": home_id}


@router.get("/{cid}/messages")
def api_messages(cid: int, after: int = 0):
    conn = _app()._conn()
    conv = conn.execute("SELECT job_id FROM conversation WHERE id = ?", (cid,)).fetchone()
    open_prompt = None
    if conv and conv["job_id"]:
        row = chat.open_prompt_for_job(conn, conv["job_id"])
        if row:
            open_prompt = {"id": row["id"], "kind": row["kind"],
                           "payload": json.loads(row["payload"]), "created_at": row["created_at"]}
    return {"messages": chat.messages_after(conn, cid, after), "open_prompt": open_prompt}


@router.post("/{cid}/messages")
def api_post_message(cid: int, text: str = Body(..., embed=True)):
    conn = _app()._conn()
    mid = chat.post_message(conn, cid, "user", text.strip())
    conv = conn.execute("SELECT kind FROM conversation WHERE id = ?", (cid,)).fetchone()
    if conv and conv["kind"] == "home":
        chat.post_message(conn, cid, "system", "Commands arrive in a later slice.")
    return {"ok": True, "message_id": mid}
```

In `app.py`, after the existing `app.include_router(api.router)`: `from career_agent.web import api_chat` and `app.include_router(api_chat.router)`.

- [ ] **Step 4: Tests pass; full suite.** **Step 5: Commit** `feat(chat): /api/chat conversations and messages`.

### Task 3: Chat shell frontend (S0 UI)

**Files:**
- Create: `frontend/src/routes/Chat.tsx`, `frontend/src/routes/Chat.css`, `frontend/src/components/chat/MessageList.tsx`, `Composer.tsx`, `Drawer.tsx`
- Modify: `frontend/src/api.ts` (types), `frontend/src/main.tsx` (routes), `frontend/src/App.tsx` (nav → "Chat" first; keep others)

**Interfaces:**
- Produces: `<Chat />` route at `/` and `/chat/:id`; `MessageList({messages, openPrompt, onAnswered})` renders roles; `Drawer` renders any existing route component in a right-side panel with `?panel=applications|resumes|settings`.
- Types added to `api.ts`:

```ts
export interface Conversation { id: number; kind: 'home' | 'job'; job_id: number | null; title: string; updated_at: string; last_message: string | null }
export interface ChatMessage { id: number; role: 'user' | 'agent' | 'system' | 'prompt'; content: string; payload: Record<string, unknown> | null; created_at: string }
export interface OpenPrompt { id: number; kind: string; payload: Record<string, unknown>; created_at: string }
```

- [ ] **Step 1: `Chat.tsx`** — sidebar lists conversations (poll `/api/chat/conversations` every 3 s), main pane polls `/api/chat/{id}/messages?after=<lastId>` every 3 s and appends, composer posts `{text}`. Drawer rail: buttons "Queue", "Résumés", "Settings" set `?panel=`; `Drawer` lazily renders `<Applications/>`, `<Resumes/>`, `<Settings/>`.

```tsx
// frontend/src/routes/Chat.tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { get, post, type ChatMessage, type Conversation, type OpenPrompt } from '../api'
import { Glass } from '../components/Glass'
import { Composer } from '../components/chat/Composer'
import { Drawer } from '../components/chat/Drawer'
import { MessageList } from '../components/chat/MessageList'
import './Chat.css'

const POLL_MS = 3000

export function Chat() {
  const { id } = useParams()
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const [convs, setConvs] = useState<Conversation[]>([])
  const [homeId, setHomeId] = useState<number | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [openPrompt, setOpenPrompt] = useState<OpenPrompt | null>(null)
  const lastId = useRef(0)
  const cid = id ? Number(id) : homeId

  const loadConvs = useCallback(() => {
    get<{ conversations: Conversation[]; home_id: number }>('/api/chat/conversations')
      .then((r) => { setConvs(r.conversations); setHomeId(r.home_id) })
      .catch(() => {})
  }, [])

  const loadMessages = useCallback(() => {
    if (!cid) return
    get<{ messages: ChatMessage[]; open_prompt: OpenPrompt | null }>(
      `/api/chat/${cid}/messages?after=${lastId.current}`)
      .then((r) => {
        if (r.messages.length) {
          lastId.current = r.messages[r.messages.length - 1].id
          setMessages((m) => [...m, ...r.messages])
        }
        setOpenPrompt(r.open_prompt)
      })
      .catch(() => {})
  }, [cid])

  useEffect(() => { loadConvs(); const t = setInterval(loadConvs, POLL_MS); return () => clearInterval(t) }, [loadConvs])
  useEffect(() => { lastId.current = 0; setMessages([]); loadMessages(); const t = setInterval(loadMessages, POLL_MS); return () => clearInterval(t) }, [loadMessages])

  const send = (text: string) => cid ? post(`/api/chat/${cid}/messages`, { text }).then(loadMessages) : Promise.resolve()
  const panel = params.get('panel')

  return (
    <div className="chat">
      <Glass as="aside" className="chat__list">
        {convs.map((c) => (
          <button key={c.id} className={c.id === cid ? 'conv active' : 'conv'}
                  onClick={() => nav(c.kind === 'home' ? '/' : `/chat/${c.id}`)}>
            <div className="conv__title">{c.title}</div>
            <div className="conv__last">{c.last_message ?? ''}</div>
          </button>
        ))}
      </Glass>
      <main className="chat__main">
        <MessageList messages={messages} openPrompt={openPrompt} onAnswered={loadMessages} />
        <Composer onSend={send} disabled={!cid} />
      </main>
      <nav className="chat__rail">
        {(['applications', 'resumes', 'settings'] as const).map((p) => (
          <button key={p} onClick={() => setParams(panel === p ? {} : { panel: p })}>{p}</button>
        ))}
      </nav>
      {panel && <Drawer panel={panel} onClose={() => setParams({})} />}
    </div>
  )
}
```

`MessageList` renders each message by role (`user` right-aligned, `agent` left, `system` muted small, `prompt` renders the text plus — in this task — a placeholder "Awaiting your answer (card arrives in S2)"; the `onAnswered` prop exists now so Task 8 only swaps the card in). `Composer` is a form with a textarea, Enter to send, Shift+Enter newline. `Drawer` maps `panel` to `{applications: <Applications/>, resumes: <Resumes/>, settings: <Settings/>}` inside a fixed right pane with a close button. Add `Chat.css` for a 3-column grid (`240px 1fr 56px`), messages scroll, drawer `position: fixed; right: 0; width: min(640px, 90vw)`.

- [ ] **Step 2: `main.tsx`** — `<Route index element={<Chat />} />`, `<Route path="chat/:id" element={<Chat />} />`, keep the other routes. `App.tsx` NAV: add `{ to: '/', label: 'Chat', end: true }` first and rename Dashboard's link to `/dashboard` with a matching route.
- [ ] **Step 3: Type-check** `cd frontend && npx tsc -b` → clean. **Step 4: Manual acceptance** with backend `career-agent serve` and `npm run dev`: `/` shows Home + job conversations with backfilled history; posting in Home gets the placeholder system reply; drawers open the old pages. **Step 5: Commit** `feat(chat): chat shell with conversation list, polling messages, drawers`.

---

## Slice S1 — Live agent stream

### Task 4: `AgentRun` supervisor with a fake-Popen test

**Files:**
- Create: `backend/src/career_agent/apply/runner.py`
- Modify: `backend/src/career_agent/apply/agent.py` (add `build_cmd(model, mcp_path, session_id: str, resume: bool = False)` — adds `--input-format stream-json`, `--session-id <id>` or `--resume <id>`, and REMOVES `--no-session-persistence`; keep every sandbox flag)
- Test: `backend/tests/test_runner.py`, extend `backend/tests/test_apply_agent.py` for `build_cmd`

**Interfaces:**
- Produces:

```python
@dataclass
class RunEvents:
    on_text: Callable[[str], None] = lambda s: None
    on_tool: Callable[[str, str], None] = lambda name, summary: None
    on_turn_end: Callable[[float | None, dict], None] = lambda cost, usage: None
    on_ask: Callable[[dict], None] = lambda payload: None          # used from Task 7
    on_confirm: Callable[[dict], None] = lambda payload: None      # used from Task 7

class AgentRun:
    def __init__(self, cmd, cwd, env, nonce, events: RunEvents, popen=subprocess.Popen): ...
    def start(self, first_message: str) -> None
    def send(self, text: str) -> None           # one stream-json user message
    def wait(self, timeout_s: float | None) -> bool   # True if finished
    def kill(self) -> None
    transcript: str; cost_total: float | None; usage_total: dict; result_line: str | None
    done: threading.Event; waiting: threading.Event   # waiting = ASK/CONFIRM emitted, turn ended
    nudges: int
```

Rules: the reader thread parses stream-json lines exactly as `agent.consume_stream` does today (reuse `summarize_tool_input` for redaction). On each `result` message: accumulate cost/usage, then (a) if the transcript since the previous turn contains a `RESULT:<nonce>:` line → set `result_line`, close stdin, set `done`; (b) elif it contains `ASK:<nonce>:` or `CONFIRM:<nonce>:` → set `waiting` and call `on_ask`/`on_confirm` with the parsed JSON (parsing helpers arrive in Task 6; in this task pass `{"raw": line}` — Task 6 swaps in `parse_ask`/`parse_confirm`); (c) else → send the nudge text `"Continue. If you need something from the human, emit an ASK line; when finished, emit your RESULT line."` at most 3 times, then close stdin and set `done` with `result_line=None`. Process exit always sets `done`.

Stream-json user message format written to stdin (one line):
`{"type":"user","message":{"role":"user","content":[{"type":"text","text": <text>}]}}\n`

- [ ] **Step 1: Failing tests** — a `FakePopen` whose `stdout` is a pipe the test writes stream-json lines into and whose `stdin` is captured:

```python
# backend/tests/test_runner.py
import io, json, os, threading, time
from career_agent.apply.runner import AgentRun, RunEvents

NONCE = "abc123"


class FakePopen:
    def __init__(self, *a, **kw):
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "r", encoding="utf-8")
        self._w = os.fdopen(w, "w", encoding="utf-8")
        self.stdin = io.StringIO()
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
    first = json.loads(fake.stdin.getvalue().splitlines()[0])
    assert first["type"] == "user" and first["message"]["content"][0]["text"] == "hello agent"


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
    run.send(f'ANSWER:{NONCE}:{{"id":"q1","answer":"30 days"}}')
    assert "ANSWER:" in fake.stdin.getvalue().splitlines()[-1]
    assert not run.waiting.is_set()          # cleared by send()
    fake.emit(_assistant(f"RESULT:{NONCE}:APPLIED")); fake.emit(_result(0.1)); fake.close()
    assert run.wait(5) and run.cost_total == 0.2


def test_turn_without_ask_or_result_is_nudged_then_given_up():
    run, fake = _run()
    for _ in range(4):
        fake.emit(_assistant("thinking...")); fake.emit(_result(0.0))
        time.sleep(0.05)
    assert run.wait(5) and run.result_line is None and run.nudges == 3
    assert fake.stdin.getvalue().count("Continue.") == 3


def test_kill_finishes_the_run():
    run, fake = _run()
    run.kill(); fake.close()
    assert run.wait(5) and run.result_line is None
```

And in `test_apply_agent.py`:

```python
def test_build_cmd_streams_input_and_keeps_the_session():
    from career_agent.apply.agent import build_cmd
    cmd = build_cmd("sonnet", "C:/m.json", session_id="11111111-1111-4111-8111-111111111111")
    assert "--input-format" in cmd and cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--session-id") + 1].startswith("11111111")
    assert "--no-session-persistence" not in cmd
    for flag in ("--strict-mcp-config", "--tools", "--disallowedTools"): assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    r = build_cmd("sonnet", "C:/m.json", session_id="11111111-1111-4111-8111-111111111111", resume=True)
    assert "--resume" in r and "--session-id" not in r
```

- [ ] **Step 2: Run → FAIL.** **Step 3: Implement `runner.py`**

```python
"""AgentRun: one live `claude -p` session, stdin kept open (stream-json in
and out) so the human's answers reach the SAME session mid-form. The reader
thread turns the stream into events; turn boundaries (`result` messages)
decide whether the agent is waiting on us, finished, or needs a nudge."""
import json
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

from career_agent.apply.agent import summarize_tool_input, _kill_tree

NUDGE = ("Continue. If you need something from the human, emit an ASK line; "
         "when finished, emit your RESULT line.")
MAX_NUDGES = 3


@dataclass
class RunEvents:
    on_text: Callable[[str], None] = lambda s: None
    on_tool: Callable[[str, str], None] = lambda n, s: None
    on_turn_end: Callable[[float | None, dict], None] = lambda c, u: None
    on_ask: Callable[[dict], None] = lambda p: None
    on_confirm: Callable[[dict], None] = lambda p: None


def _parse_payload(line: str, prefix: str) -> dict:
    try:
        return json.loads(line[len(prefix):])
    except json.JSONDecodeError:
        return {"raw": line}


class AgentRun:
    def __init__(self, cmd, cwd, env, nonce: str, events: RunEvents,
                 popen=subprocess.Popen):
        self.cmd, self.cwd, self.env, self.nonce, self.events = cmd, cwd, env, nonce, events
        self._popen = popen
        self.proc = None
        self.transcript = ""
        self._turn_buf: list[str] = []
        self.cost_total: float | None = None
        self.usage_total: dict = {}
        self.result_line: str | None = None
        self.done = threading.Event()
        self.waiting = threading.Event()
        self.nudges = 0
        self._lock = threading.Lock()

    # -- process ------------------------------------------------------------
    def start(self, first_message: str) -> None:
        self.proc = self._popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=self.env, cwd=str(self.cwd), shell=False)
        self._write_user(first_message)
        threading.Thread(target=self._reader, daemon=True).start()

    def _write_user(self, text: str) -> None:
        msg = {"type": "user", "message": {"role": "user",
               "content": [{"type": "text", "text": text}]}}
        with self._lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def send(self, text: str) -> None:
        self.waiting.clear()
        self._write_user(text)

    def _close_stdin(self) -> None:
        with self._lock:
            try: self.proc.stdin.close()
            except Exception: pass

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            _kill_tree(self.proc.pid)
        self.done.set()

    def wait(self, timeout_s: float | None) -> bool:
        return self.done.wait(timeout_s)

    # -- stream -------------------------------------------------------------
    def _append(self, line: str) -> None:
        self.transcript += line + "\n"
        self._turn_buf.append(line)

    def _reader(self) -> None:
        try:
            for raw in self.proc.stdout:
                raw = raw.strip()
                if not raw: continue
                try: msg = json.loads(raw)
                except json.JSONDecodeError:
                    self._append(raw); continue
                t = msg.get("type")
                if t == "assistant":
                    for block in msg.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            self._append(block["text"]); self.events.on_text(block["text"])
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "").replace("mcp__playwright__", "")
                            summary = summarize_tool_input(block.get("input", {}))
                            self._append(f"  >> {name} {summary}"); self.events.on_tool(name, summary)
                    for k, v in (msg.get("message", {}).get("usage") or {}).items():
                        if isinstance(v, int): self.usage_total[k] = self.usage_total.get(k, 0) + v
                elif t == "result":
                    cost = msg.get("total_cost_usd")
                    if cost is not None: self.cost_total = (self.cost_total or 0.0) + cost
                    if msg.get("result"): self._append(msg["result"])
                    self.events.on_turn_end(cost, dict(self.usage_total))
                    self._end_of_turn()
        finally:
            self.done.set()

    def _end_of_turn(self) -> None:
        turn, self._turn_buf = self._turn_buf, []
        lines = [l.strip() for t in turn for l in t.splitlines()]
        res = [l for l in lines if l.startswith(f"RESULT:{self.nonce}:")]
        if res:
            self.result_line = res[-1]; self._close_stdin(); return
        asks = [l for l in lines if l.startswith(f"ASK:{self.nonce}:")]
        confirms = [l for l in lines if l.startswith(f"CONFIRM:{self.nonce}:")]
        if confirms:
            self.waiting.set(); self.events.on_confirm(_parse_payload(confirms[-1], f"CONFIRM:{self.nonce}:")); return
        if asks:
            self.waiting.set(); self.events.on_ask(_parse_payload(asks[-1], f"ASK:{self.nonce}:")); return
        if self.nudges < MAX_NUDGES:
            self.nudges += 1; self._write_user(NUDGE); return
        self._close_stdin()
```

`build_cmd` change in `agent.py`:

```python
def build_cmd(model: str, mcp_path, session_id: str, resume: bool = False) -> list[str]:
    session = ["--resume", session_id] if resume else ["--session-id", session_id]
    return (["claude", "--model", model, "-p",
             "--mcp-config", str(mcp_path), "--strict-mcp-config",
             "--tools", "",
             "--disallowedTools", "mcp__playwright__browser_run_code_unsafe",
             "--permission-mode", "bypassPermissions",
             "--input-format", "stream-json",
             "--output-format", "stream-json", "--verbose"] + session)
```
(Keep the docstring's sandbox rationale; note that `--no-session-persistence` is dropped deliberately so `--resume` works, and that the trailing `-` prompt argument is gone because the prompt now arrives as the first stream-json user message. Update the existing `build_cmd` tests' expected argv accordingly.)

- [ ] **Step 4: Tests pass; full suite.** **Step 5: Commit** `feat(runner): AgentRun keeps the claude session open over stream-json`.

### Task 5: `submit()` runs on `AgentRun` and streams into the job chat

**Files:**
- Modify: `backend/src/career_agent/apply/ats.py` (`_live_run_agent` → builds `RunEvents` that post agent/system messages via a `conn_factory`; runs `AgentRun` under the existing watchdog; returns `AgentResult` built from `run.result_line`/`run.transcript`), `backend/src/career_agent/apply/agent.py` (`_run_agent_blocking` replaced by `run_session(prompt, *, job_id, nonce, session_id, events, cdp_port, timeout_s, model, resume=False) -> AgentResult` — same transcript/cost footer logic; `run_agent` becomes a thin wrapper)
- Modify: `backend/src/career_agent/web/worker.py`, `web/actions.py` (pass `conn_factory=` into `submit`; `web/app.py`'s `_conn` is the factory in production)
- Test: `backend/tests/test_apply_ats.py` (one new test: the injected runner receives `events`, and calling `events.on_text("hi")` from the fake results in an `agent` message in the job conversation)

**Interfaces:**
- `submit(conn, job_id, dry_run, brief, profile=None, resume_version=None, run_agent=None, conn_factory=None)` — `run_agent` seam signature becomes `async (prompt, job_id, nonce, events: RunEvents) -> AgentResult`. `conn_factory` is a zero-arg callable returning a NEW connection (thread-safe use from the reader thread); default `lambda: conn` is NOT acceptable across threads — default to `None` and, when `None`, events are no-ops (tests that don't care).
- `RunEvents` built in `ats._chat_events(conn_factory, job_id)`: `on_text` → `chat.post_message(c, cid, "agent", text)`; `on_tool` → `chat.post_message(c, cid, "system", f"{name} {summary}")` only for `browser_navigate`, `browser_file_upload`, `browser_click`, `browser_fill_form` (others are noise); `on_turn_end` → nothing yet.

- [ ] **Step 1: Failing test**

```python
async def test_live_events_post_agent_messages(conn, job_id, brief, profile, tmp_path):
    from career_agent import chat, db
    factory = lambda: db.connect(tmp_path / "t.db")   # same file as the conn fixture
    async def fake(prompt, jid, nonce, events):
        events.on_text("Navigating to the posting")
        events.on_tool("browser_navigate", "url=https://x")
        return AgentResult("draft_ready", answers={})
    await ats.submit(conn, job_id, dry_run=True, brief=brief, profile=profile,
                     run_agent=fake, conn_factory=factory)
    msgs = chat.messages_after(conn, chat.conversation_for_job(conn, job_id))
    assert any(m["role"] == "agent" and "Navigating" in m["content"] for m in msgs)
    assert any(m["role"] == "system" and "browser_navigate" in m["content"] for m in msgs)
```

Update every existing fake in `test_apply_ats.py` to the 4-arg signature (`async def _fake(prompt, job_id, nonce, events)`).

- [ ] **Step 2: FAIL.** **Step 3: Implement** — in `agent.py`:

```python
def run_session(prompt, *, job_id, nonce, session_id, events, cdp_port=9222,
                timeout_s=1200, model=APPLY_MODEL, resume=False) -> AgentResult:
    require_binaries()
    work_dir = WORK_DIR.resolve(); log_dir = LOG_DIR.resolve()
    work_dir.mkdir(parents=True, exist_ok=True); log_dir.mkdir(parents=True, exist_ok=True)
    mcp_path = work_dir / ".mcp-apply.json"
    mcp_path.write_text(json.dumps(_mcp_config(cdp_port)), encoding="utf-8")
    session_dir = work_dir / "session"
    if session_dir.exists(): _shutil.rmtree(session_dir, ignore_errors=True)
    session_dir.mkdir(parents=True)
    env = os.environ.copy(); env.pop("CLAUDECODE", None); env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    from career_agent.apply.runner import AgentRun
    run = AgentRun(build_cmd(model, mcp_path, session_id, resume=resume), session_dir, env, nonce, events)
    RUNS[job_id] = run
    start = time.time(); timed_out = threading.Event()
    def _watchdog(): timed_out.set(); run.kill()
    alarm = threading.Timer(timeout_s, _watchdog); alarm.daemon = True; alarm.start()
    try:
        run.start(prompt)
        run.wait(None)
    finally:
        alarm.cancel(); RUNS.pop(job_id, None)
        if run.proc and run.proc.poll() is None: _kill_tree(run.proc.pid)
    duration_ms = int((time.time() - start) * 1000)
    transcript = _write_transcript(log_dir, job_id, run.transcript, run.cost_total, duration_ms, run.usage_total)
    if timed_out.is_set(): result = AgentResult("failed", "timeout")
    else: result = parse_result(run.transcript, nonce)
    result.transcript_path = str(transcript); result.cost_usd = run.cost_total or 0.0
    result.usage = run.usage_total if run.cost_total is None else None; result.duration_ms = duration_ms
    return result

RUNS: dict[int, "AgentRun"] = {}   # job_id -> live run; the answer API sends into it (Task 7)

async def run_agent(prompt, *, job_id, nonce, events, session_id=None, **kw) -> AgentResult:
    import uuid
    return await asyncio.to_thread(run_session, prompt, job_id=job_id, nonce=nonce,
                                   session_id=session_id or str(uuid.uuid4()), events=events, **kw)
```

The watchdog pause while waiting on the human (spec S2) lands in Task 7. In `ats.py`, `_live_run_agent(prompt, job_id, nonce, events)` passes `events` through; `submit` builds `events = _chat_events(conn_factory, job_id)` (no-op `RunEvents()` when `conn_factory is None`) and calls `_run(runner, prompt, job_id, nonce, events)`.

- [ ] **Step 4: Tests pass; full suite.** **Step 5: Wire production**: `worker.apply_tick` and `actions.do_apply/send` pass `conn_factory=` (worker already receives `conn_factory`; actions get it from `app._conn` via a new `conn_factory` param defaulting to `None`, and `api.py`/`app.py` pass `m._conn`). **Step 6: Commit** `feat(apply): stream the agent's narration into the job conversation`.

---

## Slice S2 — Two-way pop-ups (ASK / CONFIRM)

### Task 6: Protocol parsers and prompt sections

**Files:**
- Modify: `backend/src/career_agent/apply/agent.py`
- Test: `backend/tests/test_apply_agent.py`

**Interfaces:**
- Produces: `parse_ask(line, nonce) -> dict|None`, `parse_confirm(line, nonce) -> dict|None` (None when the nonce is wrong or JSON invalid; ASK requires keys `id`, `kind` ∈ {choice,text,approve,approve_account,need_password}, `question`; `choice` requires non-empty `options`); `ask_prefix(nonce)`, `confirm_prefix(nonce)`; `build_prompt(..., mode: "manual"|"auto", can_submit: bool, nonce, pinned_answers=None, score=None)` — replaces the draft/send/auto modes with ONE interactive playbook whose ending reads:

```
== HOW TO ASK THE HUMAN ==
When a field cannot be answered from APPLICANT PROFILE, PREFERENCES, or KNOWN ANSWERS,
or when a rule below requires approval, output exactly one line
  ASK:<nonce>:{"id":"<short id>","kind":"choice|text|approve|approve_account|need_password",
               "question":"...","options":[...],"why":"...","memory_key":"<snake_case or null>",
               "default":"<best guess or null>","sensitive":false}
then END YOUR TURN and do nothing until a line beginning ANSWER:<nonce>: arrives.
Use memory_key for facts that recur across applications (notice_period, expected_salary,
relocation_willing, ...). Never guess a hard fact.

== BEFORE APPLYING ==
When every field is filled, output exactly one line
  CONFIRM:<nonce>:{"fields":[{"label":"...","value":"..."}],"files":["..."],
                   "account_actions":["..."],"memory_used":["..."],"notes":"..."}
listing EVERY field and value as it stands on the form, then END YOUR TURN and wait for a
line beginning DECISION:<nonce>:. On {"decision":"approve"} -> <SUBMIT_INSTRUCTION>.
On {"decision":"change","changes":{...}} -> apply exactly those changes, then CONFIRM again.
On {"decision":"cancel"} -> output RESULT:<nonce>:FAILED:cancelled.
```

where `<SUBMIT_INSTRUCTION>` is, when `can_submit`: "click Submit/Apply, confirm the acknowledgement page, output RESULT:<nonce>:APPLIED"; else: "do NOT click Submit — submission is disabled on this system; output RESULT:<nonce>:DRAFT_READY". `mode="auto"` adds: "The human has pre-approved: treat the first CONFIRM as approved without waiting" — the backend still records the CONFIRM payload as the answers. The old draft/send/auto `_MODE_ENDING` dict, the `dry_run` word, and the send-mode PINNED ANSWERS block are removed; `pinned_answers` (a resume with prior answers, Task 10) renders a `== PREVIOUSLY ANSWERED (use verbatim) ==` section.

- [ ] **Step 1: Failing tests**

```python
def test_parse_ask_requires_nonce_and_shape():
    from career_agent.apply.agent import parse_ask
    ok = parse_ask('ASK:n1:{"id":"q1","kind":"choice","question":"Notice?","options":["30","60"]}', "n1")
    assert ok["id"] == "q1" and ok["options"] == ["30", "60"]
    assert parse_ask('ASK:zz:{"id":"q1","kind":"text","question":"x"}', "n1") is None
    assert parse_ask('ASK:n1:{"id":"q1","kind":"choice","question":"x","options":[]}', "n1") is None
    assert parse_ask('ASK:n1:not json', "n1") is None
    assert parse_ask('ASK:n1:{"id":"q1","kind":"dance","question":"x"}', "n1") is None


def test_parse_confirm_normalises_fields():
    from career_agent.apply.agent import parse_confirm
    c = parse_confirm('CONFIRM:n1:{"fields":[{"label":"Name","value":"Asha"}],"files":["r.docx"]}', "n1")
    assert c["fields"] == [{"label": "Name", "value": "Asha"}] and c["account_actions"] == []
    assert parse_confirm('CONFIRM:n1:{"fields":"nope"}', "n1") is None


def test_prompt_teaches_ask_and_confirm_with_nonce():
    p = build_prompt(_job(), _profile(), _brief(), [], "r", "x.docx", mode="manual",
                     can_submit=False, nonce="n1")
    assert "ASK:n1:" in p and "CONFIRM:n1:" in p and "END YOUR TURN" in p
    assert "RESULT:n1:DRAFT_READY" in p and "do NOT click Submit" in p
    assert "RESULT:n1:APPLIED" not in p.split("== RESULT CODES")[0]


def test_prompt_submits_only_when_allowed_and_auto_preapproves():
    p = build_prompt(_job(), _profile(), _brief(), [], "r", "x.docx", mode="auto",
                     can_submit=True, nonce="n1")
    assert "click Submit" in p and "pre-approved" in p
    m = build_prompt(_job(), _profile(), _brief(), [], "r", "x.docx", mode="manual",
                     can_submit=True, nonce="n1")
    assert "pre-approved" not in m
```

Keep `test_every_result_line_in_the_prompt_carries_the_nonce` passing (every `RESULT:` in the prompt goes through `result_prefix`). Delete tests that asserted the old draft/send endings (`test_draft_mode_forbids_submit...`, `test_send_mode_pins_answers`, `test_send_mode_forbids_improvising...`) and replace `test_send_mode_requires_pinned_answers` with `test_unknown_mode_raises` only.

- [ ] **Step 2: FAIL.** **Step 3: Implement** parsers next to `parse_result`:

```python
_ASK_KINDS = {"choice", "text", "approve", "approve_account", "need_password"}

def ask_prefix(nonce): return f"ASK:{nonce}:"
def confirm_prefix(nonce): return f"CONFIRM:{nonce}:"

def parse_ask(line: str, nonce: str) -> dict | None:
    line = line.strip()
    if not line.startswith(ask_prefix(nonce)): return None
    try: p = json.loads(line[len(ask_prefix(nonce)):])
    except json.JSONDecodeError: return None
    if not isinstance(p, dict) or not {"id", "kind", "question"} <= p.keys(): return None
    if p["kind"] not in _ASK_KINDS: return None
    if p["kind"] == "choice" and not p.get("options"): return None
    p.setdefault("options", []); p.setdefault("why", ""); p.setdefault("memory_key", None)
    p.setdefault("default", None); p.setdefault("sensitive", False)
    return p

def parse_confirm(line: str, nonce: str) -> dict | None:
    line = line.strip()
    if not line.startswith(confirm_prefix(nonce)): return None
    try: p = json.loads(line[len(confirm_prefix(nonce)):])
    except json.JSONDecodeError: return None
    if not isinstance(p, dict) or not isinstance(p.get("fields"), list): return None
    fields = [f for f in p["fields"] if isinstance(f, dict) and "label" in f and "value" in f]
    return {"fields": fields, "files": list(p.get("files") or []),
            "account_actions": list(p.get("account_actions") or []),
            "memory_used": list(p.get("memory_used") or []), "notes": str(p.get("notes") or "")}
```

`build_prompt` gains `can_submit: bool` (keyword-only, required) and `mode ∈ {"manual","auto"}`; `_steps_section(mode, can_submit, R)` renders the HOW TO ASK / BEFORE APPLYING text above with `R = result_prefix(nonce)`, `ask_prefix(nonce)`, `confirm_prefix(nonce)`. In `runner.py` `_end_of_turn`, replace `_parse_payload` with `parse_ask`/`parse_confirm`; a malformed ASK/CONFIRM line counts as "no ask" (falls to the nudge path — the nudge text tells the agent its line was malformed: append `" Your last ASK/CONFIRM line was not valid JSON with the required keys."` when a malformed one was seen).

- [ ] **Step 4: Tests pass; full suite.** **Step 5: Commit** `feat(protocol): ASK/CONFIRM parsers and the interactive playbook`.

### Task 7: Two-way relay — prompts to DB, answers to stdin, `submit(mode=)`

**Files:**
- Modify: `backend/src/career_agent/apply/ats.py` (signature `submit(conn, job_id, mode: str, brief, profile=None, resume_version=None, run_agent=None, conn_factory=None)`; `dry_run` removed; outcome recording unified), `backend/src/career_agent/apply/agent.py` (`run_session` pauses the watchdog while `run.waiting` is set and enforces `answer_wait_s=1800`), `backend/src/career_agent/web/actions.py` (`answer_prompt(conn, prompt_id, answer: dict, conn_factory) -> dict`), `backend/src/career_agent/web/api_chat.py` (`POST /api/chat/prompts/{prompt_id}/answer`)
- Test: `backend/tests/test_apply_ats.py`, `backend/tests/test_api_chat.py`

**Interfaces:**
- `ats.submit(conn, job_id, mode, ...)`: creates the `in_flight` row inside the lock FIRST (all modes), runs the agent; outcome mapping: `applied` → `submitted` (answers = the last approved CONFIRM's fields as `{label: value}`); `draft_ready` → `draft` (same answers); `needs_answer`/`captcha` → row deleted + event (as today); everything else → `_record_send_outcome`'s classification, except that unknown-state reasons hold as `held_unknown` ONLY when `can_submit` was True — with the kill switch off nothing could have been submitted, so they stay retryable `failed`. `can_submit = SUBMISSION_IMPLEMENTED or run_agent is not None` (the injected seam keeps tests off the switch, as today).
- Events: `on_ask(payload)` → `chat.open_prompt(c, job_id, payload["kind"], payload)`; `on_confirm(payload)` → `chat.open_prompt(c, job_id, "confirm", payload)` then, when `mode == "auto"`, immediately `answer_prompt` with `{"decision": "approve"}`; the approved CONFIRM payload is stored in `ats._LAST_CONFIRM[job_id]` for the outcome.
- `actions.answer_prompt(conn, prompt_id, answer, conn_factory)`: validates by kind (`choice` → `answer` ∈ options; `text` → non-empty; `approve`/`approve_account` → `answer` ∈ {approve, reject}; `confirm` → `decision` ∈ {approve, change, cancel}); records via `chat.answer_prompt_row`; posts a `user` message summarising the answer; sends `ANSWER:`/`DECISION:` (built with `agent.answer_line(nonce, ...)`) into `agent.RUNS[job_id]` — the nonce is the one stored on the checkpoint (Task 10) or, until then, on `ats._NONCES[job_id]`; returns `{"ok": False, "message": "That question is no longer open"}` when the prompt isn't open or no run is live.

- [ ] **Step 1: Failing tests** (fakes drive the events from inside the injected runner):

```python
async def test_ask_opens_a_prompt_and_answer_reaches_the_run(conn, job_id, brief, profile, tmp_path):
    from career_agent import chat, db
    from career_agent.apply import agent as agent_mod
    from career_agent.web import actions
    factory = lambda: db.connect(tmp_path / "t.db")
    got = {}
    class FakeRun:                       # stands in for agent.RUNS[job_id]
        def send(self, text): got["sent"] = text
        waiting = threading.Event()
    async def fake(prompt, jid, nonce, events):
        agent_mod.RUNS[jid] = FakeRun()
        events.on_ask({"id": "q1", "kind": "choice", "question": "Notice?", "options": ["30", "60"],
                       "why": "", "memory_key": "notice_period", "default": None, "sensitive": False})
        pid = chat.open_prompt_for_job(factory(), jid)["id"]
        r = actions.answer_prompt(factory(), pid, {"answer": "30", "remember": True}, factory)
        assert r["ok"] and got["sent"].startswith(f"ANSWER:{nonce}:")
        assert json.loads(got["sent"].split(":", 2)[2]) == {"id": "q1", "answer": "30", "remember": True}
        return AgentResult("draft_ready", answers={"Notice?": "30"})
    r = await ats.submit(conn, job_id, mode="manual", brief=brief, profile=profile,
                         run_agent=fake, conn_factory=factory)
    assert r["ok"] and r["status"] == "draft"
    assert chat.open_prompt_for_job(conn, job_id) is None


async def test_confirm_in_auto_mode_is_auto_approved_and_recorded(conn, job_id, brief, profile, tmp_path):
    ...  # fake emits on_confirm({"fields":[{"label":"Name","value":"Asha"}], ...}) then returns applied;
         # assert application.status == 'submitted' and json.loads(answers) == {"Name": "Asha"};
         # assert the agent_prompt row is 'answered' with {"decision":"approve"}


async def test_confirm_in_manual_mode_waits_for_a_decision(conn, job_id, brief, profile, tmp_path):
    ...  # fake emits on_confirm, asserts the prompt is still 'open', then a background
         # actions.answer_prompt(...{"decision":"change","changes":{"Phone":"+91"}}) sends DECISION:;
         # fake returns applied; assert answers include Phone "+91" (from the second confirm the fake emits)


def test_answer_prompt_validates_choice_and_closed(conn, job_id):
    ...  # choice answer not in options -> ok False; answering twice -> "no longer open"


async def test_unknown_state_holds_only_when_submission_was_possible(conn, job_id, brief, profile):
    # run_agent injected => can_submit True => timeout holds
    r = await ats.submit(conn, job_id, mode="manual", brief=brief, profile=profile,
                         run_agent=fake_agent(AgentResult("failed", "timeout")))
    assert row_status(conn, job_id) == "held_unknown"
```

Plus `test_api_chat.py`: `POST /api/chat/prompts/{id}/answer` with a valid body returns `ok` and marks the row answered (no live run → `ok: False` with the "no longer open"/"no live run" message — assert the message when `RUNS` is empty).

- [ ] **Step 2: FAIL.** **Step 3: Implement**
  - `agent.answer_line(nonce, kind, body: dict) -> str`: `f"ANSWER:{nonce}:{json.dumps(body)}"` for asks, `f"DECISION:{nonce}:{json.dumps(body)}"` for `confirm`.
  - `run_session`: replace the single `Timer` with a loop: `while not run.done.is_set(): if run.waiting.is_set(): (pause work clock; if waited > answer_wait_s: run.kill(); reason="answer_timeout") else: if work_elapsed > timeout_s: run.kill(); reason="timeout"; run.done.wait(1)`. Track `work_elapsed` as accumulated time while not waiting.
  - `ats.submit(mode=...)`: as specified in Interfaces; `_record_outcome(conn, job_id, app_id, url, result, detail, can_submit, confirm)` merges the two old recorders (keep their tests, adjusting statuses: with `run_agent` injected `can_submit=True`, so existing hold expectations stand).
  - Worker/actions call sites: `mode=state["mode"]` in `apply_tick` (one call, both modes); `actions.do_apply` uses `mode="manual"`; `actions.send` is retired: with a live run a CONFIRM decision is the send; keep the route returning `{"ok": False, "message": "Answer the review card in the job's chat instead."}`.
- [ ] **Step 4: Tests pass; full suite.** **Step 5: Commit** `feat(apply): ASK/CONFIRM relayed through chat; answers reach the live session`.

### Task 8: Prompt and Confirm cards (S2 UI)

**Files:**
- Create: `frontend/src/components/chat/PromptCard.tsx`, `ConfirmCard.tsx`
- Modify: `frontend/src/components/chat/MessageList.tsx` (render the card for the `open_prompt`, plain text for answered ones), `frontend/src/api.ts` (`answerPrompt(id, body)` helper)

**Interfaces:** `PromptCard({prompt, onAnswered})`: `choice` → one button per option plus a "Remember this" checkbox (default checked, hidden for `approve*`); `text` → input pre-filled with `payload.default`, remember checkbox; `approve`/`approve_account` → shows `question`, `why`, and for `approve_account` the `domain`/`email`/`terms_summary`, with Approve / Reject buttons. `ConfirmCard({prompt, onAnswered})`: table of `fields`, list of `files` and `account_actions`, buttons Approve / Change an answer (turns each value into an editable input; Save sends `{decision:"change", changes:{label:value}}` for edited rows only) / Cancel. Both POST to `/api/chat/prompts/{id}/answer` and call `onAnswered`.

- [ ] **Step 1: Implement both components** (compact, `ui.css` classes), swap them into `MessageList` where the S0 placeholder was. **Step 2: `npx tsc -b` clean.** **Step 3: Manual acceptance:** with a stub — temporarily call `chat.open_prompt` from a Python shell against the dev DB — confirm the card renders and answering marks it answered (the "no live run" message is expected). **Step 4: Commit** `feat(chat): ASK and CONFIRM cards`.

### Task 9: Worker and dashboard status on the new flow

**Files:**
- Modify: `backend/src/career_agent/web/worker.py` (remove the `needs_answer` park branch — questions no longer end runs; keep the `unsupported` pause), `backend/src/career_agent/web/context.py` (`run_status_context` returns `open_prompt` (id, kind, question) and `conversation_id` for the current job instead of `needs_answer_question`/`draft_answers`), `frontend/src/components/AgentStatusBar.tsx` (link "Answer in chat →" to `/chat/<conversation_id>`), `frontend/src/routes/Applications.tsx` (remove `AnswerForm`; the Apply button stays and opens the job's chat after starting)
- Test: `backend/tests/test_worker.py`, `tests/test_web.py` (update the needs_answer/draft_answers assertions to the new keys)

- [ ] Steps: failing test updates → implement → `pytest` full → `tsc -b` → commit `refactor(worker): questions flow through chat; status bar links to the conversation`.

---

## Slice S3 — Checkpoint & resume

### Task 10: `apply_checkpoint` store and resume selection

**Files:**
- Modify: `backend/src/career_agent/db.py` (table), Create: `backend/src/career_agent/apply/checkpoint.py`, Modify: `apply/ats.py` (write checkpoints; `submit(..., resume=False)`), `apply/agent.py` (`run_session(..., resume=True)` sends `CONTINUE:` as the first message)
- Test: `backend/tests/test_checkpoint.py`, `tests/test_apply_ats.py`

**Interfaces:**
```python
# checkpoint.py
def start(conn, job_id, session_id, nonce) -> None            # status running, step "start"
def mark_waiting(conn, job_id, prompt_id) -> None               # status waiting
def mark_running(conn, job_id, step: str, answers: dict) -> None
def mark_resumable(conn, job_id) -> None
def finish(conn, job_id) -> None                                # status done
def get(conn, job_id) -> dict | None
def continue_message(cp: dict, nonce: str) -> str               # "CONTINUE:<nonce>:{step, answers}"
def sweep_orphans(conn, live_job_ids: set[int]) -> int          # running/waiting with no live run -> resumable
```
Table:
```sql
CREATE TABLE IF NOT EXISTS apply_checkpoint (
    job_id         INTEGER PRIMARY KEY REFERENCES job(id),
    session_id     TEXT NOT NULL,
    nonce          TEXT NOT NULL,
    step           TEXT NOT NULL DEFAULT 'start',
    answers        TEXT NOT NULL DEFAULT '{}',
    form_url       TEXT,
    open_prompt_id INTEGER,
    status         TEXT NOT NULL CHECK (status IN ('running','waiting','resumable','done')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
```
`ats.submit`: on start `checkpoint.start`; `on_ask/on_confirm` → `mark_waiting`; `answer_prompt` → `mark_running(step=f"answered {id}", answers += {question: answer})`; result → `finish`; timeout/answer_timeout/crash → `mark_resumable` and **no attempt consumed** (`failed_permanent` never results from a resumable stop). `submit(..., resume=True)` reads the checkpoint, reuses `session_id` and `nonce`, builds the prompt with `pinned_answers=cp.answers` (fallback if `--resume` fails: `run.result_line is None and run.transcript == ""` within 30 s → rerun fresh with the same pinned answers), and sends `continue_message` as the first message.

- [ ] Tests: start/waiting/running/finish transitions; `sweep_orphans` flips a stale `running` row with no live run to `resumable`; `submit(resume=True)` passes `resume=True`+`session_id` to the injected runner (extend the seam to accept `session_id=None, resume=False` kwargs) and its prompt contains `PREVIOUSLY ANSWERED`. Implement, full suite, commit `feat(resume): checkpoints per job and --resume of the same session`.

### Task 11: Resume from the chat and the worker

**Files:**
- Modify: `web/actions.py` (`resume_job(conn, job_id, conn_factory)`), `web/api_chat.py` (`POST /api/chat/jobs/{job_id}/resume`), `web/worker.py` (on loop start `checkpoint.sweep_orphans`; in auto mode auto-resume a `resumable` checkpoint once — track `resumed` in the checkpoint `step`), `frontend/src/routes/Chat.tsx` (a "Continue where it left off" button when `GET messages` reports `resumable: true` — add that to the S0 endpoint's response from the checkpoint)
- Test: `tests/test_api_chat.py`, `tests/test_worker.py`

- [ ] Steps: failing tests → implement → full suite → `tsc -b` → manual: kill the `claude` process mid-draft (Task Manager), see the Continue button, click it → commit `feat(resume): Continue button and worker auto-resume`.

---

## Slice S4 — Personalized memory *(parallel-safe: new functions only)*

### Task 12: Memory store and prompt section

**Files:**
- Modify: `backend/src/career_agent/db.py` (six `_add_column_if_missing` calls on `qa_bank`: `memory_key TEXT`, `kind TEXT`, `options_json TEXT`, `source_job_id INTEGER`, `use_count INTEGER DEFAULT 0`, `last_used_at TEXT`), `backend/src/career_agent/store.py` (`qa_remember`, `qa_by_key`, `qa_touch`, `qa_all` returns the new columns), `backend/src/career_agent/apply/agent.py` (ADD `_preferences_section(qa_rows) -> str` — do NOT edit `build_prompt`'s list; Task 18 wires it)
- Test: `backend/tests/test_store.py`, `tests/test_apply_agent.py`

**Interfaces:**
```python
def qa_remember(conn, question, answer, *, kind="text", options=None, memory_key=None,
                source_job_id=None, is_volatile=False) -> None   # upsert literal row; if memory_key: also upsert a row whose question_normalized == memory_key
def qa_by_key(conn, memory_key) -> sqlite3.Row | None
def qa_touch(conn, keys: list[str]) -> None                        # use_count += 1, last_used_at = now
def _preferences_section(qa_rows) -> str   # "== PREFERENCES ==\n- notice_period: 30 days (confirmed 2026-09-13)" for rows with memory_key; stale volatile rows flagged "(stale — ask with this as the default)"
```
- [ ] Tests: remember stores both rows; `qa_by_key` finds it; `qa_touch` bumps; section lists keys and flags stale; `qa_all` includes `memory_key`. Implement; full suite; commit `feat(memory): keyed preferences in qa_bank and the PREFERENCES prompt section`.

### Task 13: Memory API and drawer

**Files:**
- Create: `backend/src/career_agent/web/api_memory.py` (`GET /api/memory` list; `PUT /api/memory/{id}` `{answer, is_volatile}`; `DELETE /api/memory/{id}`), `frontend/src/routes/Memory.tsx` (table: question/key, answer (editable), "ask me again" toggle = is_volatile, last used, delete)
- Modify: `web/actions.py` — `answer_prompt` calls `store.qa_remember(...)` when `answer.get("remember", True)` and the prompt kind is `choice`/`text` (pass `memory_key` and `options` from the prompt payload); after CONFIRM approve, `store.qa_touch(payload["memory_used"])`.
- Test: `tests/test_api_memory.py` (own small FastAPI app including only this router + `_conn` monkeypatch — see the `_app()` pattern), `tests/test_apply_ats.py` (an answered ASK with `remember: true` lands in `qa_bank` with its key)
- [ ] Steps: tests → implement → `tsc -b` → commit `feat(memory): /api/memory and the Memory drawer; answers are remembered`.

---

## Slice S5 — Credential store *(parallel-safe)*

### Task 14: Fernet crypto and `site_credential`

**Files:**
- Create: `backend/src/career_agent/security.py` (`load_key() -> bytes` from `CREDENTIAL_KEY`; raises a clear error naming `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` when missing; `encrypt(s) -> str`, `decrypt(s) -> str`), `backend/src/career_agent/credentials.py` (`put(conn, domain, login_url, email, password, created_by)`, `get(conn, domain) -> dict|None` (decrypted), `list_(conn) -> list[dict]` (never the password), `delete(conn, id)`, `generate_password() -> str` (20 chars, `secrets`)), table in `db.py`, `cryptography` added to `pyproject.toml` dependencies, `.env.example` gains `CREDENTIAL_KEY=`
- Modify: `apply/agent.py` — ADD `_known_logins_section(logins: list[dict]) -> str` (domain + email only) and extend `_hard_rules_section` text: account creation allowed ONLY after an approved `approve_account` ASK, with the password supplied in the ANSWER; never choose a password. (Task 18 wires the section.)
- Test: `tests/test_credentials.py` (round trip; `list_` hides passwords; missing key error), `tests/test_apply_agent.py` (rule text present)
- [ ] Steps: tests → implement → full suite → commit `feat(credentials): encrypted site logins`.

### Task 15: Account protocol and Logins drawer

**Files:**
- Modify: `web/actions.py` — in `answer_prompt`: for `approve_account` with `answer=="approve"`: `pw = credentials.generate_password(); credentials.put(conn, payload["domain"], payload.get("login_url",""), payload["email"], pw, "agent")` BEFORE sending `ANSWER:` with `{"id", "answer": "approve", "password": pw}`; for `need_password`: look up `credentials.get(conn, payload["domain"])` and send `{"id", "password": ...}` or `{"id","answer":"none"}`; post a `system` message "Saved login for <domain>" (never the password). `submit` passes `credentials.list_(conn)` into the prompt args (Task 18 wires the section).
- Create: `web/api_credentials.py` (`GET /api/logins`, `DELETE /api/logins/{id}`), `frontend/src/routes/Logins.tsx`
- Test: `tests/test_apply_ats.py` (approve_account stores an encrypted credential before the ANSWER line carries it; `need_password` returns it), `tests/test_api_credentials.py`
- [ ] Steps: tests → implement → `tsc -b` → commit `feat(credentials): approve_account/need_password protocol and Logins drawer`.

---

## Slice S6 — Full profile *(parallel-safe)*

### Task 16: `CandidateProfile` extension and TOML round-trip

**Files:**
- Modify: `backend/src/career_agent/config.py`:

```python
class Address(BaseModel):
    line1: str = ""; city: str = ""; state: str = ""; postal_code: str = ""; country: str = ""

class WorkEntry(BaseModel):
    company: str; title: str; start: str = ""; end: str = ""; current: bool = False; description: str = ""

class EduEntry(BaseModel):
    institution: str; degree: str = ""; field: str = ""; start: str = ""; end: str = ""

class CandidateProfile(BaseModel):
    candidate_name: str = Field(min_length=1); candidate_email: str = Field(min_length=1)
    candidate_phone: str = Field(min_length=1)
    linkedin_url: str | None = None; portfolio_url: str | None = None
    gender: str = "decline"
    address: Address = Address()
    work_history: list[WorkEntry] = []
    education: list[EduEntry] = []
```
  `_save_toml`: nested dicts become tomlkit tables, lists of dicts become arrays of tables (`tomlkit.aot()`); a value equal to the pydantic default is still written when present in the model dump so the file is explicit.
- Modify: `apply/agent.py` — `_profile_section(profile)` renders gender, address, work history (one line per entry: `company — title (start–end|present): description`) and education; empty lists render "(none recorded)".
- Modify: `candidate_profile.toml.example` with the new sections.
- Test: `tests/test_config.py` (round trip with two work entries survives `save_candidate_profile` → `load_candidate_profile`; missing sections default), `tests/test_apply_agent.py` (section shows the entries)
- [ ] Steps: tests → implement → full suite → commit `feat(profile): address, gender, work history and education`.

### Task 17: Profile API and drawer

**Files:**
- Create: `web/api_profile.py` (`GET /api/profile` → model dump; `PUT /api/profile` body = full model → validates with pydantic, saves via `save_candidate_profile`, returns `{ok, errors}` like `PUT /api/settings`), `frontend/src/routes/Profile.tsx` (form with repeatable rows for work/education; Save)
- Test: `tests/test_api_profile.py`
- [ ] Steps: tests → implement → `tsc -b` → commit `feat(profile): /api/profile and the Profile drawer`.

---

## Phase C

### Task 18: Integration — wire sections, routers, drawers; full verification

**Files:**
- Modify: `apply/agent.py` (`build_prompt` section list: insert `_preferences_section(qa_rows)` before KNOWN ANSWERS and `_known_logins_section(logins)` after PROFILE; `build_prompt` gains `logins: list[dict] | None = None`), `apply/ats.py` (pass `credentials.list_(conn)`), `web/app.py` (mount `api_memory`, `api_credentials`, `api_profile`), `frontend/src/main.tsx` + `components/chat/Drawer.tsx` (register `memory`, `logins`, `profile` panels and the rail buttons)
- Test: `tests/test_apply_agent.py` (one prompt containing PREFERENCES and KNOWN LOGINS sections in the right order), `tests/test_web.py` smoke: each new router answers 200 on its GET.
- [ ] Steps: merge the three parallel branches into the serial branch (resolve nothing but this file set); tests → implement → **full suite** → `tsc -b` → manual walk-through in the running portal (Home, a job chat, all six drawers) → commit `feat: integrate memory, logins and profile into the chat`.

### Task 19: Intent router (`web/intent.py`)

**Files:**
- Create: `backend/src/career_agent/web/intent.py`; Test: `tests/test_intent.py`

**Interfaces:**
```python
INTENTS = ("find_jobs", "show_queue", "apply_to", "pause_apply", "resume_apply", "stop_apply", "status", "help", "unknown")
SCHEMA = {"type":"object","properties":{"intent":{"enum":list(INTENTS)},"job_ref":{"type":["string","null"]},"reply":{"type":"string"}},"required":["intent","reply"]}
def route(text: str, *, runner=None) -> dict   # runner(prompt, schema) -> dict is the seam; default spawns `claude -p --model haiku --json-schema <schema> --tools "" --strict-mcp-config --output-format json` with CLAUDECODE scrubbed and parses `structured_output`/`result`
def resolve_job(conn, job_ref: str) -> int | None   # "#1639", "1639", "the SES role" -> LIKE on company/title among queued jobs; None if ambiguous
```
- [ ] Tests: `route` with an injected runner returns the dict; `resolve_job` handles id, `#id`, company substring, ambiguity → None. Implement; commit `feat(intent): structured intent routing for the Home chat`.

### Task 20: Home chat commands

**Files:**
- Modify: `web/actions.py` (`home_message(conn, text, brief_path, profile_path, conn_factory) -> dict`: routes; `show_queue`/`status`/`help` reply immediately as an `agent` message; `find_jobs` and `apply_to` post an `approve` prompt in Home ("Run discovery now? (Apify + scoring credits)" / "Start applying to <job>?") — approval calls `pipeline.run_now`/`do_apply`; `pause/resume/stop_apply` call the existing run controls), `web/api_chat.py` (`POST /{cid}/messages` on Home calls `home_message` instead of the placeholder; `answer_prompt` handles Home `approve` prompts by dispatching the stored action from the prompt payload `{"action": "...", "args": {...}}`)
- Test: `tests/test_api_chat.py` with an injected intent runner
- [ ] Steps: tests → implement → full suite → manual: "what's my queue?", "apply to #1639" → Approve → the job chat starts → commit `feat(chat): Home commands with confirmation`.

### Task 21: Docs

- Modify `CLAUDE.md` (apply-engine section: interactive protocol, modes, checkpoints, memory, credentials — same length discipline as before), `README.md` (`CREDENTIAL_KEY`, portal is chat-first), `docs/lld-apply-button-v2.md` (status line: superseded by the chat-first spec for interaction; §5 note on `submit(mode=)`).
- [ ] Full suite green, `tsc -b` clean, commit `docs: chat-first agent`.

---

## Self-review

- **Spec coverage:** §2 tables → Tasks 1, 10, 12, 14; profile TOML → 16. §3 protocol → 4, 6, 7, 10, 15. S0 → 1–3; S1 → 4–5; S2 → 6–9; S3 → 10–11; S4 → 12–13; S5 → 14–15; S6 → 16–17; S7 → 19–20. §5 testing → every task's step 1; §6 risks unchanged.
- **Placeholders:** Tasks 7 (three tests), 9, 10, 11, 13, 15, 17, 20 give behaviour and asserts in prose where the code is a direct application of a neighbouring task's full example (7's first test is complete; the elided ones state exact expected statuses/messages). No TBDs.
- **Type consistency:** `RunEvents` fields (4, 5, 7); `submit(conn, job_id, mode, brief, profile, resume_version, run_agent, conn_factory, resume)` (7, 10, 11); runner seam `(prompt, job_id, nonce, events, session_id=None, resume=False)` (5, 7, 10); `chat.open_prompt/answer_prompt_row/open_prompt_for_job` (1, 2, 7, 13, 15); `parse_ask/parse_confirm/ask_prefix/confirm_prefix` (6, 4); `checkpoint.*` (10, 11); `credentials.put/get/list_/generate_password` (14, 15, 18); `_preferences_section/_known_logins_section` (12, 14, 18).
