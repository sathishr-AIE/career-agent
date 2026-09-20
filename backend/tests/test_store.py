import json
import sqlite3

import pytest

from career_agent import db, store
from career_agent.apply import ats as ats_apply
from career_agent.config import CareerBrief
from career_agent.models import Job, Verdict

BRIEF = CareerBrief(target_titles=["AI Engineer"], search_locations=["Chennai"],
                    locations=["Chennai"], staleness_days=30)

V = Verdict(role_fit=90, credibility=90, opportunity=90,
            application_quality=90, eligibility_soft=90,
            verdict="submit", rationale="ok")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(**kw):
    base = dict(source="ats", external_id="1", company="Acme",
                title="AI Engineer", location="Chennai")
    return Job(**{**base, **kw})


def test_upsert_inserts_new_and_skips_duplicate_fingerprints(conn):
    assert store.upsert_jobs(conn, [_job()], BRIEF) == 1
    assert store.upsert_jobs(conn, [_job(source="linkedin", external_id="9")],
                             BRIEF) == 0
    assert conn.execute("SELECT COUNT(*) n FROM job").fetchone()["n"] == 1


def test_upsert_drops_stale(conn):
    assert store.upsert_jobs(conn, [_job(posted_at="2019-01-01")], BRIEF) == 0


def test_unscored_excludes_current_version_but_not_old(conn):
    store.upsert_jobs(conn, [_job()], BRIEF)
    job_id = conn.execute("SELECT id FROM job").fetchone()["id"]
    assert len(store.unscored_jobs(conn, "gate-v1", 10)) == 1

    store.save_assessment(conn, job_id, V, "m", "gate-v1")
    assert store.unscored_jobs(conn, "gate-v1", 10) == []
    # bumping the prompt version invalidates the old assessment
    assert len(store.unscored_jobs(conn, "gate-v2", 10)) == 1


def test_unscored_excludes_hard_skipped(conn):
    store.upsert_jobs(conn, [_job()], BRIEF)
    job_id = conn.execute("SELECT id FROM job").fetchone()["id"]
    store.save_hard_skip(conn, job_id, "location outside accepted set")
    assert store.unscored_jobs(conn, "gate-v1", 10) == []


def test_unscored_excludes_merged_jobs(conn):
    store.upsert_jobs(conn, [_job(), _job(company="Globex")], BRIEF)
    a, b = [r["id"] for r in conn.execute("SELECT id FROM job ORDER BY id")]
    conn.execute("UPDATE job SET merged_into_job_id = ? WHERE id = ?", (a, b))
    assert len(store.unscored_jobs(conn, "gate-v1", 10)) == 1


def test_facts_returns_claims(conn):
    conn.execute("INSERT INTO fact (claim, evidence) VALUES ('Built RAG', 'proj X')")
    assert store.facts(conn) == ["Built RAG (evidence: proj X)"]


def test_settings_default_to_sonnet_and_25(conn):
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-sonnet-5"
    assert s["max_score_per_run"] == 25


def test_save_settings_round_trips(conn):
    store.save_settings(conn, "claude-haiku-4-5", 50)
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-haiku-4-5"
    assert s["max_score_per_run"] == 50


def test_save_settings_rejects_an_unknown_model(conn):
    with pytest.raises(ValueError, match="unknown scoring model"):
        store.save_settings(conn, "gpt-4", 25)
    assert store.get_settings(conn)["scoring_model"] == "claude-sonnet-5"


def test_save_settings_rejects_a_negative_cap(conn):
    with pytest.raises(ValueError):
        store.save_settings(conn, "claude-sonnet-5", -1)
    assert store.get_settings(conn)["max_score_per_run"] == 25


def test_save_settings_allows_zero_cap(conn):
    """0 is meaningful: discovery and the hard filter run, scoring does not."""
    store.save_settings(conn, "claude-sonnet-5", 0)
    assert store.get_settings(conn)["max_score_per_run"] == 0


def _seed_one(conn, company="Acme") -> int:
    store.upsert_jobs(conn, [_job(company=company)], BRIEF)
    return conn.execute("SELECT id FROM job WHERE company = ?",
                        (company,)).fetchone()["id"]


def _seed_facts(conn, n=10):
    for i in range(n):
        conn.execute("INSERT INTO fact (claim, evidence) VALUES (?, ?)",
                     (f"claim {i}", f"evidence {i}"))
    conn.commit()


def test_fact_rows_returns_ids_paired_with_formatted_text(conn):
    _seed_facts(conn, n=2)
    rows = store.fact_rows(conn)
    assert rows == [(1, "claim 0 (evidence: evidence 0)"),
                    (2, "claim 1 (evidence: evidence 1)")]


def test_latest_resume_version_is_none_with_no_rows(conn):
    job_id = _seed_one(conn)
    assert store.latest_resume_version(conn, job_id) is None


def test_latest_resume_version_returns_the_newest_row(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'a.docx', ?)", (job_id,))
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r2', 'b.docx', ?)", (job_id,))
    conn.commit()
    assert store.latest_resume_version(conn, job_id) == "tailored-1-r2"


def test_resume_version_for_falls_back_to_the_constant(conn):
    job_id = _seed_one(conn)
    assert store.resume_version_for(conn, job_id) == ats_apply.RESUME_VERSION


def test_resume_version_for_prefers_a_tailored_row(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'a.docx', ?)", (job_id,))
    conn.commit()
    assert store.resume_version_for(conn, job_id) == "tailored-1-r1"


def test_next_resume_version_counts_existing_rows_for_that_job(conn):
    job_id = _seed_one(conn)
    assert store.next_resume_version(conn, job_id) == f"tailored-{job_id}-r1"
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES (?, 'a.docx', ?)",
                 (f"tailored-{job_id}-r1", job_id))
    conn.commit()
    assert store.next_resume_version(conn, job_id) == f"tailored-{job_id}-r2"


def test_insert_resume_stores_a_new_row(conn):
    job_id = _seed_one(conn)
    version = store.insert_resume(conn, job_id, "tailored-1-r1", "a.docx", "{}")
    assert version == "tailored-1-r1"
    row = conn.execute("SELECT * FROM resume WHERE version = ?",
                       (version,)).fetchone()
    assert row["job_id"] == job_id
    assert row["path"] == "a.docx"


def test_insert_resume_on_a_version_collision_returns_the_winner(conn):
    job_id = _seed_one(conn)
    store.insert_resume(conn, job_id, "tailored-1-r1", "a.docx", "{}")
    # A second insert under the same version string (the concurrent-click
    # race) must not raise -- it reports back the row that actually won.
    version = store.insert_resume(conn, job_id, "tailored-1-r1", "b.docx", "{}")
    assert version == "tailored-1-r1"
    assert conn.execute(
        "SELECT COUNT(*) n FROM resume WHERE version = ?",
        ("tailored-1-r1",)).fetchone()["n"] == 1


def test_mark_applied_promotes_an_existing_draft(conn):
    """The normal path: 'Open & track' left a draft, and the user then
    applied on the site. Promote that row rather than inserting a second,
    so one application attempt stays one row."""
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " answers) VALUES (?, 'base-v1', 'draft', '{\"note\": \"x\"}')",
                 (job_id,))
    conn.commit()

    app_id = store.mark_applied(conn, job_id, "2026-08-20")

    rows = conn.execute("SELECT * FROM application WHERE job_id = ?",
                        (job_id,)).fetchall()
    assert len(rows) == 1, "promoted, not duplicated"
    assert rows[0]["id"] == app_id
    assert rows[0]["status"] == "submitted"
    assert rows[0]["submitted_at"] == "2026-08-20"
    assert rows[0]["answers"] is None, (
        "a manual application's contents are genuinely unknown; the draft's "
        "answers are precisely what was NOT sent, so NULL says so rather "
        "than inheriting the stub filler's placeholder")


def test_mark_applied_inserts_when_there_is_no_draft(conn):
    """Applying straight from the job board without tracking it first."""
    job_id = _seed_one(conn)

    app_id = store.mark_applied(conn, job_id, "2026-08-19")

    row = conn.execute("SELECT * FROM application WHERE id = ?",
                       (app_id,)).fetchone()
    assert row["status"] == "submitted"
    assert row["submitted_at"] == "2026-08-19"
    assert row["answers"] is None, (
        "a manual application's contents are genuinely unknown; NULL says so "
        "rather than inheriting the stub filler's placeholder")
    assert row["resume_version"] == ats_apply.RESUME_VERSION


def test_mark_applied_logs_the_human_decision(conn):
    job_id = _seed_one(conn)
    store.mark_applied(conn, job_id, "2026-08-20")
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_marked_applied" in types


def test_mark_applied_refuses_a_job_that_already_has_one(conn):
    """The partial unique index one_live_application_per_job makes double
    marking structurally impossible. Confirm it actually fires."""
    job_id = _seed_one(conn)
    store.mark_applied(conn, job_id, "2026-08-20")
    with pytest.raises(sqlite3.IntegrityError):
        store.mark_applied(conn, job_id, "2026-08-21")


def test_mark_applied_uses_the_tailored_resume_when_one_exists(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'x.docx', ?)", (job_id,))
    conn.commit()

    store.mark_applied(conn, job_id, "2026-08-22")

    row = conn.execute("SELECT resume_version FROM application"
                       " WHERE job_id = ?", (job_id,)).fetchone()
    assert row["resume_version"] == "tailored-1-r1"


def test_qa_normalize_collapses_punctuation_and_case(conn):
    assert store.qa_normalize("Notice period?") == store.qa_normalize("notice period")


def test_qa_lookup_returns_none_for_an_unseen_question(conn):
    assert store.qa_lookup(conn, "Notice period?") is None


def test_qa_upsert_then_lookup_round_trips(conn):
    store.qa_upsert(conn, "Notice period?", "30 days", is_volatile=True)
    row = store.qa_lookup(conn, "notice period")
    assert row["answer"] == "30 days"
    assert row["is_volatile"] == 1
    assert row["last_confirmed_at"] is not None


def test_qa_upsert_on_an_existing_question_overwrites_and_reconfirms(conn):
    store.qa_upsert(conn, "Notice period?", "30 days", is_volatile=True)
    first = store.qa_lookup(conn, "notice period")["last_confirmed_at"]
    store.qa_upsert(conn, "Notice period?", "60 days", is_volatile=True)
    row = store.qa_lookup(conn, "notice period")
    assert row["answer"] == "60 days"
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 1


def test_application_has_taxonomy_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(application)")}
    assert {"failure_reason", "transcript_path"} <= cols


def test_qa_all_returns_every_row(conn):
    store.qa_upsert(conn, "Visa status?", "Citizen", is_volatile=True)
    store.qa_upsert(conn, "Years of Python?", "6", is_volatile=False)
    rows = store.qa_all(conn)
    assert {r["question_normalized"] for r in rows} == \
        {store.qa_normalize("Visa status?"),
         store.qa_normalize("Years of Python?")}
    # Verify all required fields are present
    for row in rows:
        assert "question_normalized" in row.keys()
        assert "answer" in row.keys()
        assert "is_volatile" in row.keys()
        assert "last_confirmed_at" in row.keys()


def test_qa_all_includes_the_s4_memory_columns(conn):
    store.qa_upsert(conn, "Visa status?", "Citizen", is_volatile=True)
    row = store.qa_all(conn)[0]
    assert {"memory_key", "kind", "use_count"} <= set(row.keys())
    assert row["memory_key"] is None
    assert row["use_count"] == 0


def test_qa_upsert_leaves_new_columns_at_defaults(conn):
    """qa_upsert (the pre-S4 caller) never touches memory_key/kind/etc, so
    an ordinary literal answer must not accidentally look keyed."""
    store.qa_upsert(conn, "Notice period?", "30 days", is_volatile=True)
    row = store.qa_lookup(conn, "notice period")
    assert row["memory_key"] is None
    assert row["kind"] is None
    assert row["options_json"] is None
    assert row["source_job_id"] is None
    assert row["use_count"] == 0
    assert row["last_used_at"] is None


def test_qa_remember_with_a_memory_key_writes_both_rows(conn):
    store.qa_remember(conn, "What's your notice period?", "30 days",
                      kind="text", options=["30 days", "60 days"],
                      memory_key="notice_period", source_job_id=7,
                      is_volatile=True)

    literal = store.qa_lookup(conn, "What's your notice period?")
    keyed = store.qa_by_key(conn, "notice_period")

    assert literal is not None and keyed is not None
    assert literal["question_normalized"] != keyed["question_normalized"]
    assert literal["question_normalized"] == store.qa_normalize(
        "What's your notice period?")
    assert keyed["question_normalized"] == store.qa_normalize("notice_period")

    for row in (literal, keyed):
        assert row["answer"] == "30 days"
        assert row["kind"] == "text"
        assert json.loads(row["options_json"]) == ["30 days", "60 days"]
        assert row["source_job_id"] == 7
        assert row["is_volatile"] == 1
        assert row["last_confirmed_at"] is not None

    # Only the canonical keyed row carries memory_key -- otherwise the
    # PREFERENCES section would print the same preference twice.
    assert literal["memory_key"] is None
    assert keyed["memory_key"] == "notice_period"


def test_qa_remember_without_a_memory_key_writes_only_the_literal_row(conn):
    store.qa_remember(conn, "Years of Python?", "6")
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 1
    row = store.qa_lookup(conn, "Years of Python?")
    assert row["memory_key"] is None
    assert row["kind"] == "text"


def test_qa_remember_twice_updates_and_does_not_duplicate(conn):
    store.qa_remember(conn, "Notice period?", "30 days",
                      memory_key="notice_period")
    first_confirmed = store.qa_by_key(conn, "notice_period")["last_confirmed_at"]

    store.qa_remember(conn, "Notice period?", "60 days",
                      memory_key="notice_period")

    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 2
    keyed = store.qa_by_key(conn, "notice_period")
    assert keyed["answer"] == "60 days"
    assert keyed["last_confirmed_at"] >= first_confirmed
    literal = store.qa_lookup(conn, "Notice period?")
    assert literal["answer"] == "60 days"


def test_qa_by_key_is_case_and_punctuation_insensitive(conn):
    # Task 13: memory_key must itself be snake_case (see the rejection tests
    # below), so this exercises qa_by_key's own normalization on case/trailing
    # punctuation variants of a valid key -- not on the stored key's shape.
    store.qa_remember(conn, "Notice period?", "30 days",
                      memory_key="notice_period")
    for variant in ("notice_period", "NOTICE_PERIOD", "Notice_Period."):
        assert store.qa_by_key(conn, variant) is not None
        assert store.qa_by_key(conn, variant)["answer"] == "30 days"


def test_qa_by_key_returns_none_when_absent(conn):
    assert store.qa_by_key(conn, "notice_period") is None


def test_qa_touch_bumps_use_count_and_last_used_at(conn):
    store.qa_remember(conn, "Notice period?", "30 days",
                      memory_key="notice_period")
    store.qa_touch(conn, ["notice_period"])
    row = store.qa_by_key(conn, "notice_period")
    assert row["use_count"] == 1
    assert row["last_used_at"] is not None

    store.qa_touch(conn, ["notice_period"])
    assert store.qa_by_key(conn, "notice_period")["use_count"] == 2


def test_qa_touch_is_a_no_op_for_unknown_keys(conn):
    store.qa_touch(conn, ["does_not_exist"])  # must not raise
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 0


# -- Task 13: memory_key validation, qa_update/qa_delete twin rules, memory_list --

@pytest.mark.parametrize("key", ["notice period", "Notice_Period", "gender", "a_"])
def test_qa_remember_rejects_a_non_snake_case_memory_key(conn, key):
    store.qa_remember(conn, "Some question?", "answer", memory_key=key)
    # literal row still stored, but no keyed row -- only the literal exists
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 1
    literal = store.qa_lookup(conn, "Some question?")
    assert literal is not None and literal["memory_key"] is None


@pytest.mark.parametrize("key", ["notice_period", "expected_salary_inr"])
def test_qa_remember_accepts_a_snake_case_memory_key(conn, key):
    store.qa_remember(conn, "Some question?", "answer", memory_key=key)
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 2
    assert store.qa_by_key(conn, key) is not None


def test_qa_remember_logs_the_rejected_key(conn, caplog):
    with caplog.at_level("WARNING"):
        store.qa_remember(conn, "Some question?", "answer", memory_key="Notice_Period")
    assert "Notice_Period" in caplog.text


def test_qa_update_on_a_keyed_row_updates_its_literal_twin(conn):
    store.qa_remember(conn, "Notice period?", "30 days", memory_key="notice_period",
                      source_job_id=7)
    # a different job's literal row that happens to share the answer text --
    # must not be touched by an update to the (job 7) keyed row.
    store.qa_remember(conn, "How much notice?", "30 days", source_job_id=8)

    keyed = store.qa_by_key(conn, "notice_period")
    assert store.qa_update(conn, keyed["id"], "45 days", True) is True

    assert store.qa_by_key(conn, "notice_period")["answer"] == "45 days"
    twin = store.qa_lookup(conn, "Notice period?")
    assert twin["answer"] == "45 days"
    other = store.qa_lookup(conn, "How much notice?")
    assert other["answer"] == "30 days"


def test_qa_update_on_an_unkeyed_literal_only_updates_itself(conn):
    store.qa_upsert(conn, "Years of experience?", "6", is_volatile=False)
    row = store.qa_lookup(conn, "Years of experience?")
    assert store.qa_update(conn, row["id"], "7", False) is True
    assert store.qa_lookup(conn, "Years of experience?")["answer"] == "7"


def test_qa_update_unknown_id_returns_false(conn):
    assert store.qa_update(conn, 999, "x", False) is False


def test_qa_delete_removes_a_keyed_row_and_its_literal_twin(conn):
    store.qa_remember(conn, "Notice period?", "30 days", memory_key="notice_period",
                      source_job_id=7)
    keyed = store.qa_by_key(conn, "notice_period")
    assert store.qa_delete(conn, keyed["id"]) is True
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 0


def test_qa_delete_unknown_id_returns_false(conn):
    assert store.qa_delete(conn, 999) is False


def test_memory_list_hides_literal_twins_but_shows_untwinned_literals(conn):
    store.qa_remember(conn, "Notice period?", "30 days", memory_key="notice_period",
                      source_job_id=7)
    store.qa_upsert(conn, "Years of experience?", "6", is_volatile=False)
    items = store.memory_list(conn)
    by_label = {i["label"]: i for i in items}

    assert "notice_period" in by_label
    assert by_label["notice_period"]["is_preference"] is True
    assert by_label["notice_period"]["answer"] == "30 days"

    literal_label = store.qa_normalize("Notice period?")
    assert literal_label not in by_label, "the literal twin must be hidden"

    exp_label = store.qa_normalize("Years of experience?")
    assert exp_label in by_label
    assert by_label[exp_label]["is_preference"] is False
    assert by_label[exp_label]["answer"] == "6"

    for item in items:
        assert {"id", "label", "answer", "kind", "options", "is_volatile",
               "last_confirmed_at", "use_count", "last_used_at",
               "is_preference"} <= item.keys()


# -- Fix round 1: twin_key (explicit link) replaces (answer, source_job_id) --
# A review found the coincidental-matching scheme unsafe: a keyed row and an
# UNRELATED literal row from the SAME job that happen to share an answer
# text (e.g. two different yes/no questions both answered "Yes") must not be
# linked. These write the collision on purpose and assert it does NOT fire.

def _seed_collision(conn):
    store.qa_remember(conn, "Willing to relocate?", "Yes",
                      memory_key="willing_to_relocate", source_job_id=5)
    store.qa_upsert(conn, "Are you authorized to work in India?", "Yes",
                    is_volatile=False)
    # Force the same source_job_id an unsafe (answer, source_job_id) scheme
    # would have keyed off of -- qa_upsert itself never sets source_job_id.
    conn.execute("UPDATE qa_bank SET source_job_id = 5 WHERE question_normalized = ?",
                (store.qa_normalize("Are you authorized to work in India?"),))
    conn.commit()


def test_qa_update_leaves_an_unrelated_same_job_same_answer_row_untouched(conn):
    _seed_collision(conn)
    keyed = store.qa_by_key(conn, "willing_to_relocate")
    store.qa_update(conn, keyed["id"], "No", False)

    assert store.qa_by_key(conn, "willing_to_relocate")["answer"] == "No"
    unrelated = store.qa_lookup(conn, "Are you authorized to work in India?")
    assert unrelated["answer"] == "Yes"


def test_qa_delete_leaves_an_unrelated_same_job_same_answer_row_untouched(conn):
    _seed_collision(conn)
    keyed = store.qa_by_key(conn, "willing_to_relocate")
    store.qa_delete(conn, keyed["id"])
    assert store.qa_lookup(conn, "Are you authorized to work in India?") is not None


def test_memory_list_still_shows_an_unrelated_same_job_same_answer_row(conn):
    _seed_collision(conn)
    labels = {i["label"] for i in store.memory_list(conn)}
    assert store.qa_normalize("Are you authorized to work in India?") in labels


def test_requestioning_with_the_same_key_from_another_job_keeps_one_twin_link(conn):
    store.qa_remember(conn, "Notice period?", "30 days", memory_key="notice_period",
                      source_job_id=1)
    store.qa_remember(conn, "Notice period?", "45 days", memory_key="notice_period",
                      source_job_id=2)
    n = conn.execute(
        "SELECT COUNT(*) n FROM qa_bank WHERE twin_key = 'notice_period'").fetchone()["n"]
    assert n == 1
    twin = store.qa_lookup(conn, "Notice period?")
    assert twin["twin_key"] == "notice_period"
    assert twin["answer"] == "45 days" and twin["source_job_id"] == 2


def test_qa_remember_without_a_key_clears_a_prior_twin_link(conn):
    store.qa_remember(conn, "Notice period?", "30 days", memory_key="notice_period")
    assert store.qa_lookup(conn, "Notice period?")["twin_key"] == "notice_period"
    store.qa_remember(conn, "Notice period?", "45 days")   # re-answered, no key this time
    assert store.qa_lookup(conn, "Notice period?")["twin_key"] is None


# -- Fix round 1: secrets backstop, independent of the caller's sensitive flag --

def test_qa_remember_refuses_a_secret_shaped_question(conn):
    store.qa_remember(conn, "What is your bank account number?", "12345",
                      memory_key="bank_account")
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 0


def test_qa_remember_refuses_a_secret_shaped_memory_key(conn):
    store.qa_remember(conn, "What should we use to log in?", "hunter2",
                      memory_key="account_password")
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 0


# -- Fix round 2: the secrets regex must not over-match ordinary questions --
# ("pan" inside "Japan", "pin" inside the routine "PIN code" postal-code
# question, unanchored "otp"/"ssn") -- those must still be remembered.

@pytest.mark.parametrize("question", [
    "Password", "Enter your PAN", "SSN", "Bank account number",
])
def test_qa_remember_still_refuses_real_secret_questions(conn, question):
    store.qa_remember(conn, question, "x")
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 0


@pytest.mark.parametrize("question", [
    "Are you authorized to work in Japan?", "What is your PIN code?", "Company name",
])
def test_qa_remember_does_not_over_match_ordinary_questions(conn, question):
    store.qa_remember(conn, question, "x")
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 1


def test_settings_default_apply_model_is_sonnet(conn):
    assert store.get_settings(conn)["apply_model"] == "claude-sonnet-5"


def test_save_settings_leaves_the_apply_model_alone_when_not_given(conn):
    """Nine callers pass two positional args. Omitting the model must keep
    what is stored rather than blanking it."""
    store.save_settings(conn, "claude-sonnet-5", 25, apply_model="claude-opus-5")

    store.save_settings(conn, "claude-haiku-4-5", 30)

    s = store.get_settings(conn)
    assert (s["scoring_model"], s["apply_model"]) == ("claude-haiku-4-5", "claude-opus-5")


def test_save_settings_round_trips_the_apply_model(conn):
    store.save_settings(conn, "claude-sonnet-5", 25, apply_model="claude-opus-5")
    assert store.get_settings(conn)["apply_model"] == "claude-opus-5"


def test_save_settings_rejects_an_unknown_apply_model(conn):
    with pytest.raises(ValueError, match="unknown apply model"):
        store.save_settings(conn, "claude-haiku-4-5", 50, apply_model="gpt-4")
    s = store.get_settings(conn)
    assert (s["scoring_model"], s["apply_model"]) == ("claude-sonnet-5", "claude-sonnet-5")


def _resume(conn, version, bullets, path="x.docx"):
    conn.execute("INSERT INTO resume (version, path, content) VALUES (?, ?, ?)",
                 (version, path, json.dumps({"bullets": bullets})))
    conn.commit()


def test_fact_citations_maps_each_fact_to_the_resumes_that_cite_it(conn):
    """One pass for the whole list: the fact list and the delete guard must
    never disagree about who cites what."""
    _seed_facts(conn, n=3)
    _resume(conn, "tailored-1", [{"text": "a", "fact_ids": [1]},
                                 {"text": "b", "fact_ids": [1, 2]}])   # 1 twice
    _resume(conn, "tailored-2", [{"text": "c", "fact_ids": [1]}])

    assert store.fact_citations(conn) == {1: ["tailored-1", "tailored-2"],
                                          2: ["tailored-1"]}


def test_fact_citations_ignores_unparseable_resume_content(conn):
    _seed_facts(conn, n=1)
    conn.execute("INSERT INTO resume (version, path, content) VALUES ('bad', 'x.docx', 'not json')")
    conn.execute("INSERT INTO resume (version, path, content) VALUES ('untailored', 'x.docx', NULL)")
    conn.commit()
    _resume(conn, "tailored-1", [{"text": "a", "fact_ids": [1]}])

    assert store.fact_citations(conn) == {1: ["tailored-1"]}


def test_fact_cited_by_answers_for_one_fact(conn):
    """The delete guard's authority, now a lookup into the same map."""
    _seed_facts(conn, n=2)
    _resume(conn, "tailored-1", [{"text": "a", "fact_ids": [1]}])
    _resume(conn, "tailored-2", [{"text": "b", "fact_ids": [1]}])

    assert store.fact_cited_by(conn, 1) == ["tailored-1", "tailored-2"]
    assert store.fact_cited_by(conn, 2) == []
