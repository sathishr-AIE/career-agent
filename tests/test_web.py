import pytest
from fastapi.testclient import TestClient

from career_agent import db
from career_agent.web import app as web


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.init_schema(conn)
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp1','ats','1','Acme','acme','AI Engineer',"
                 " 'aiengineer','https://x/1')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility,"
                 " opportunity, application_quality, eligibility_soft,"
                 " weighted_score, verdict, rationale, model, prompt_version)"
                 " VALUES (1,'scored',90,90,90,90,90,90,'submit',"
                 " 'strong match','m','gate-v1')")
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp2','ats','2','Globex','globex','ML Engineer',"
                 " 'engineerml','https://x/2')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility,"
                 " opportunity, application_quality, eligibility_soft,"
                 " weighted_score, verdict, rationale, model, prompt_version)"
                 " VALUES (2,'scored',80,50,80,80,80,72,'skip',"
                 " 'credibility below floor','m','gate-v1')")
    conn.commit()
    monkeypatch.setattr(web, "DB_PATH", path)
    return TestClient(web.app)


def test_index_shows_submit_and_hold_with_rationale(client):
    r = client.get("/")
    assert "AI Engineer" in r.text
    assert "strong match" in r.text


def test_index_hides_skips_by_default(client):
    assert "ML Engineer" not in client.get("/").text


def test_skipped_view_shows_them(client):
    r = client.get("/?show=skipped")
    assert "ML Engineer" in r.text
    assert "credibility below floor" in r.text


def test_dismiss_records_the_human_decision(client):
    r = client.post("/dismiss/1")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_dismissed" in types


def test_override_on_a_skip_records_the_override(client):
    r = client.post("/override/2")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_override" in types


def test_plain_apply_refuses_a_skip(client):
    r = client.post("/apply/2")
    assert "skip" in r.text.lower() or "override" in r.text.lower()


def test_submit_failure_message_is_escaped(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("<script>bad</script>")

    monkeypatch.setattr(web.ats_apply, "submit", boom)
    r = client.post("/override/2")
    assert r.status_code == 200
    assert "<script>" not in r.text


def test_send_without_a_draft_is_refused(client):
    r = client.post("/send/1")
    assert "draft" in r.text.lower()


def test_send_after_apply_performs_a_real_submission(client, monkeypatch):
    calls = []

    async def fake_submit(conn, job_id, dry_run, filler=None):
        calls.append(dry_run)
        status = "draft" if dry_run else "submitted"
        conn.execute(
            "INSERT INTO application (job_id, resume_version, status)"
            " VALUES (?, 'v1', ?)", (job_id, status))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": status}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)

    client.post("/apply/1")
    r = client.post("/send/1")

    assert r.status_code == 200
    assert calls == [True, False]
    conn = db.connect(web.DB_PATH)
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_confirmed_send" in types


def test_index_offers_send_once_a_draft_exists(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'draft')")
    conn.commit()
    r = client.get("/")
    assert '/send/1' in r.text
    assert '/apply/1' not in r.text
