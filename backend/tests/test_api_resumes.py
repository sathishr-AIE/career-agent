import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_agent import db
from career_agent.web import api
from career_agent.web import app as web

BRIEF_TOML = """\
target_titles = ["AI Engineer"]
search_locations = ["Chennai"]
locations = ["Chennai"]
daily_cap = 5
"""


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.init_schema(c)
    brief = tmp_path / "career_brief.toml"
    brief.write_text(BRIEF_TOML, encoding="utf-8")
    monkeypatch.setattr(web, "DB_PATH", path)
    monkeypatch.setattr(web, "BRIEF_PATH", brief)
    return c


@pytest.fixture
def client(conn):
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_versions_link_their_job_and_name_the_tailoring_prompt(client, conn):
    """RS1: a version opens its job hub and says which tailoring prompt made it."""
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company, company_normalized,"
                 " title, title_normalized, url) VALUES ('fp1', 'ats', '1', 'Stripe', 'stripe',"
                 " 'Senior SRE', 'seniorsre', 'https://x/1')")
    conn.execute("INSERT INTO resume (version, path, job_id, content) VALUES (?, ?, ?, ?)",
                 ("tailored-1-r1", "a.docx", 1,
                  json.dumps({"summary": "s", "bullets": [], "prompt_version": "tailor-v1"})))
    # A row written before the tailor recorded its prompt version.
    conn.execute("INSERT INTO resume (version, path, job_id, content) VALUES (?, ?, ?, ?)",
                 ("tailored-1-r0", "b.docx", 1, json.dumps({"summary": "old", "bullets": []})))
    conn.commit()
    legacy, current = client.get("/api/resumes").json()["versions"]  # newest row first
    assert (current["version"], current["job_id"], current["prompt_version"]) == (
        "tailored-1-r1", 1, "tailor-v1")
    assert (legacy["job_id"], legacy["prompt_version"]) == (1, None)
