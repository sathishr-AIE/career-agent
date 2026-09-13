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
