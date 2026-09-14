# Chat-First Career Agent — Design Spec

*Status: approved in conversation 2026-09-13; supersedes the interaction model in
[lld-apply-button-v2.md](../../lld-apply-button-v2.md) (the agent engine, sandbox, and
state machine described there remain the substrate). Branch: `feat/agentic-apply-p0`
continues; this work lands as vertical slices S0–S7.*

## 0. Why

The first live run proved the agent can navigate a real ATS (SuccessFactors) end to end,
but the interaction model failed the user: questions ended the run, review happened in a
JSON blob, and the human did most of the applying by hand. The product must behave like a
chat with an assistant that *does the applying*: it works, asks only when it must — in the
chat, like a pop-up — remembers the answers, resumes after any interruption, and clicks
Apply itself once the human has approved what will be sent.

## 1. Architecture

```
Browser (React)                     FastAPI (single process)                  Subprocesses
┌─────────────────────┐   poll 3s   ┌────────────────────────────────┐        ┌──────────────┐
│ Chat shell           │◄──────────►│ /api/chat/*  /api/*            │        │ claude -p    │
│  sidebar: Home + one │            │  context.py / actions.py       │  stdin │  stream-json │
│  conversation per job│            │                                │◄──────►│  in + out    │
│  message list        │            │ Agent runner  ─────────────────┼────────┤  Playwright  │
│  prompt cards (ASK / │            │  one AgentRun per job, holds   │ stdout │  MCP → Chrome│
│  CONFIRM)            │            │  stdin open, parses ASK/CONFIRM│        └──────────────┘
│  drawers: queue,     │            │  RESULT, writes messages       │        ┌──────────────┐
│  applications,       │            │ Intent router (Home chat)  ────┼───────►│ claude -p    │
│  profile, memory,    │            │ Credential service (Fernet)    │        │ --json-schema│
│  logins, settings    │            │ Checkpoint/resume              │        └──────────────┘
└─────────────────────┘            └──────────────┬─────────────────┘
                                                  ▼
                                   SQLite career.db (+ conversation, message,
                                   agent_prompt, apply_checkpoint, site_credential;
                                   qa_bank extended)
```

Three in-process services; no new servers. Transport is the DB polled by the UI (SSE is
S8). All existing safety machinery stays: `SUBMISSION_IMPLEMENTED` kill switch, the
`application` state machine and partial unique index, the sandboxed `claude` command
(`--tools ""`, `--strict-mcp-config`, unsafe-code tool blocked), redacted input logging,
unknown-state hold on the send path.

## 2. Data model (all new tables via `init_schema`; new columns via `_add_column_if_missing`)

```sql
conversation(id PK, kind TEXT CHECK(kind IN ('home','job')), job_id INTEGER UNIQUE NULL
             REFERENCES job(id), title TEXT, created_at, updated_at)
message(id PK, conversation_id FK, role TEXT CHECK(role IN ('user','agent','system','prompt')),
        content TEXT, payload TEXT NULL /* json */, created_at)
agent_prompt(id PK, job_id FK, conversation_id FK, kind TEXT, payload TEXT /* json */,
             status TEXT CHECK(status IN ('open','answered','expired')) DEFAULT 'open',
             answer TEXT NULL /* json */, created_at, answered_at NULL)
apply_checkpoint(job_id PK REFERENCES job(id), session_id TEXT, step TEXT,
                 answers TEXT /* json */, form_url TEXT, open_prompt_id INTEGER NULL,
                 status TEXT CHECK(status IN ('running','waiting','resumable','done')),
                 updated_at)
site_credential(id PK, domain TEXT UNIQUE, login_url TEXT, email TEXT,
                password_enc TEXT, created_by TEXT CHECK(created_by IN ('agent','user')),
                created_at, last_used_at NULL)
-- qa_bank gains: memory_key TEXT, kind TEXT, options_json TEXT, source_job_id INTEGER,
--               use_count INTEGER DEFAULT 0, last_used_at TEXT
```

`candidate_profile.toml` (gitignored, via `_save_toml`) gains: `gender` (default
`"decline"`), `[address]` (line1, city, state, postal_code, country), `[[work_history]]`
(company, title, start `YYYY-MM`, end `YYYY-MM`|"", current bool, description),
`[[education]]` (institution, degree, field, start, end).

`.env` gains `CREDENTIAL_KEY` (Fernet key). `cryptography` becomes a declared dependency.

## 3. Agent protocol (contract between prompt, runner, and parser)

The CLI runs with `--input-format stream-json --output-format stream-json` and an
explicit `--session-id <uuid4>`; `--no-session-persistence` is removed so `--resume` works.
A run is **multi-turn**: the model ends a turn whenever it needs the human; the CLI emits a
per-turn `result` message and waits on stdin. The run ends only on a `RESULT:` line or
process exit. Cost is the sum of per-turn `total_cost_usd`.

Lines the agent emits (all stamped with the run nonce, one per line, last-of-kind wins):

```
ASK:<nonce>:{"id":"q3","kind":"choice|text|approve|approve_account|need_password",
             "question":"...","options":["..."],"why":"...","memory_key":"notice_period"|null,
             "default":"..."|null,"sensitive":false,
             "domain":"careers.ses.com","email":"...","login_url":"https://...","url":"https://...",
             "terms_summary":"..."}   # account kinds
CONFIRM:<nonce>:{"fields":[{"label":"Full name","value":"..."}],"files":["..._Resume.docx"],
                 "account_actions":["created account at careers.ses.com"],"notes":"..."}
RESULT:<nonce>:APPLIED | DRAFT_READY | EXPIRED | CAPTCHA | LOGIN_ISSUE |
                NEEDS_ANSWER:<q> | FAILED:<reason>
```

Lines the backend sends on stdin (stream-json user messages whose text is):

```
ANSWER:<nonce>:{"id":"q3","answer":"30 days","remember":true}
ANSWER:<nonce>:{"id":"q7","answer":"approve","submitted":true}  # approve_account ("reject" / "exists")
ANSWER:<nonce>:{"id":"q9","submitted":true}                  # need_password (or "answer":"none")
DECISION:<nonce>:{"decision":"approve"|"cancel"|"change","changes":{"Phone":"+91..."}}
CONTINUE:<nonce>:{"step":"...","answers":{...}}                            # resume
```

Rules the prompt states: after emitting ASK or CONFIRM, end the turn and do nothing else
until the matching ANSWER/DECISION arrives; never create an account or accept terms except
after an approved `approve_account`; never type, read, or ask for a password — the backend fills it into the real page over CDP,
submits the form and clears the field while the agent waits (the page is untrusted, so the
LLM must never hold the secret); emit `need_password`/`approve_account` only with every other
field of the form complete;
before Apply, always CONFIRM with the complete field list; on DECISION change, apply the
changes and CONFIRM again; on cancel, output `RESULT:FAILED:cancelled`.

Precedence for answering a form field: APPLICANT PROFILE → PREFERENCES (keyed memory) →
KNOWN ANSWERS (literal match) → ASK. Stale volatile memory (30-day window) and uncertain
matches are asked with `default` pre-filled.

## 4. Slices

### S0 — Chat shell + message log
Tables `conversation`, `message`. `store.conversation_for_job(conn, job_id)` (creates on
first use), `store.post_message(conn, conversation_id, role, content, payload=None)`,
`store.messages_after(conn, conversation_id, after_id)`. Backfill: on first request each
existing `application`/`event` for a job renders as system messages in its conversation.
API: `GET /api/chat/conversations`, `GET /api/chat/{id}/messages?after=`,
`POST /api/chat/{id}/messages`. Frontend: `ChatShell` (sidebar, `MessageList`, `Composer`,
right rail of drawer buttons), routes `/` and `/chat/:id`; existing routes render inside a
`Drawer`. 3 s polling on the open conversation. Done when: portal opens on Home, every job
with an application has a conversation with its history, drawers open the old pages.

### S1 — Live agent stream
`apply/runner.py`: `AgentRun` wraps the subprocess; a reader thread consumes stream-json
and calls `on_text`, `on_tool(name, redacted_input)`, `on_turn_result(cost)`,
`on_line(text)`; `ats.submit()` passes callbacks that post agent messages (text) and
compact tool lines to the job conversation; worker lifecycle events post system messages.
Done when: a running draft shows its narration live in the chat.

### S2 — Two-way pop-ups (ASK / CONFIRM)
`--input-format stream-json`; `AgentRun.send(text)`; parsers `parse_ask`, `parse_confirm`
(pure, nonce-checked); on ASK/CONFIRM the runner inserts an `agent_prompt` row and a
`message(role='prompt', payload={prompt_id})`, sets checkpoint status `waiting`, pauses
the work watchdog, starts the answer-wait timer (30 min). `POST /api/chat/prompts/{id}/answer`
validates against the prompt kind, records the answer, resumes the watchdog, and sends
ANSWER/DECISION on stdin. CONFIRM in manual mode requires a decision; in auto mode the
backend auto-approves; the agent only clicks Apply when `SUBMISSION_IMPLEMENTED` (the prompt
says so; when False the run ends `DRAFT_READY` after CONFIRM). The `dry_run` flag is
retired in favour of the mode. Frontend: `PromptCard` (choice buttons / text / approve-reject)
and `ConfirmCard` (field table, Approve / Change an answer / Cancel). Done when: a real
draft asks in chat, the answer reaches the running session, and CONFIRM renders the
complete summary.

### S3 — Checkpoint & resume
`apply_checkpoint` written on start, every ASK/CONFIRM, every answer, and RESULT. Own
`--session-id`; `--no-session-persistence` removed. `actions.resume(job_id)`: if a
checkpoint is `resumable` → new run with `--resume <session_id>` and a CONTINUE message;
if resume fails → fresh run with checkpoint answers pinned. Worker: on startup, any
`running` checkpoint whose process is gone becomes `resumable`; a **Continue** button
appears in the job chat; auto mode auto-resumes once. Answer-wait expiry → checkpoint
`resumable`, process closed cleanly, no attempt consumed. Done when: killing the process
mid-form and clicking Continue finishes the same form.

### S4 — Personalized memory
`qa_bank` columns above; `store.qa_remember(question, answer, kind, options, memory_key,
source_job_id, is_volatile)`; `store.qa_by_key(key)`; every answered ASK with
`remember=true` is stored under the literal question and, when present, the `memory_key`.
Prompt: PREFERENCES section (key → value → last confirmed) before KNOWN ANSWERS; precedence
and default-prefill rules. `/memory` drawer (list, edit, delete, "ask me again" toggle,
last used). `use_count`/`last_used_at` bump when the agent reports a memory was used (the
CONFIRM payload lists `memory_used: [keys]`). Done when: a question answered on job A is not
asked on job B, and editing it on `/memory` changes what job C sends.

### S5 — Credential store
`security.py`: `encrypt(str)->str`, `decrypt(str)->str` with `CREDENTIAL_KEY`;
`store.credential_put/get/list/delete`. Protocol kinds `approve_account` and
`need_password`. The page is untrusted (it can prompt-inject the agent), so the LLM never holds a secret:
on approval the backend generates a 20-char password and, in one CDP session while the agent
waits, fills it into the REAL https page only when that page is on the approved domain, submits
the form (the Create click), stores it encrypted, and clears the field; `need_password` fills
and submits a saved login the same way. The agent is answered `submitted`/`none`/`exists`,
never a password, and continues from the resulting page. The Playwright MCP server is pinned
(`@playwright/mcp@0.0.80`, whose snapshot renders a password input's value) and its evaluate,
network and screenshot tools are disallowed. A submit counts only when the page navigates or
the filled fields detach; a page with more than one login form is refused; after the clear,
every input on the domain is scanned for the exact value (again after 500 ms), and a field
that keeps it means not submitted. The agent's Chrome profile never carries Login Data.

**Residual risk.** Not preventable from outside the page: a site that re-renders a failed
login with the password still in the field after the clear (the agent's next snapshot would
show it), and a show-password toggle the agent clicks against the prompt rules. A sign-up the
site rejects after submit still leaves the stored row, which the user deletes in Logins. Prompt: KNOWN LOGINS section lists domain+email only. `/logins` drawer
(list, delete, "created by"). Done when: an account-required site is handled with one
Approve in chat and the password is retrievable from the drawer.

### S6 — Full profile
`CandidateProfile` extension (see §2), `_save_toml` round-trip for nested arrays of tables,
`GET/PUT /api/profile`, `/profile` drawer (form with repeatable work/education rows),
APPLICANT PROFILE prompt rendering incl. work history and education. Done when: a form's
address, gender, mobile, and previous-experience sections are filled from the profile.

### S7 — Home chat commands
`web/intent.py`: `route(text) -> Intent` via `claude -p --json-schema --model haiku`,
intents: `find_jobs`, `show_queue`, `apply_to{job_ref}`, `pause_apply`, `resume_apply`,
`stop_apply`, `status`, `help`, `unknown`. `actions.home_message(text)` dispatches; spend or
submit intents post an `agent_prompt(kind='approve')` in Home first. Done when: "apply to
the SES role" starts that job's run after one Approve.

### S8 — later
SSE push, parallel workers, CAPTCHA, PDF résumé.

## 5. Testing (every slice)
Pure parsers and prompt sections: unit. Runner: a fake subprocess (pipes) round-trip test —
emit ASK, receive ANSWER, emit RESULT. Store/schema: SQLite temp DB. API: TestClient.
Frontend: `tsc -b` type check; manual check in the running portal is the acceptance step.
Nothing spawns a browser or `claude`, nothing spends credits.

## 6. Out of scope / risks accepted
Operator hooks still load in the agent session (needs `--restricted`, incompatible with
bypassPermissions). Playwright can navigate `file://`. Drafts can upload the résumé and
click Save on the employer site. These are recorded in LLD §6 and unchanged here.
