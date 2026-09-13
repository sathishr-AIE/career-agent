import os
import tomllib
from pathlib import Path

import pydantic
import pytest

from career_agent.config import (CandidateProfile, CareerBrief, load_boards,
                                 load_brief, load_candidate_profile,
                                 save_brief, save_candidate_profile)

BRIEF = """
target_titles = ["AI Engineer", "ML Engineer"]
title_families = ["ai engineer", "ml engineer"]
search_locations = ["Chennai"]
locations = ["Chennai", "Remote"]
remote_ok = true
salary_floor_inr = 1200000
daily_cap = 5
gate_threshold = 72
staleness_days = 30
excluded_companies = ["BadCo"]
"""

COMMENTED_BRIEF = """\
# Single source of truth for search and filter settings.
target_titles = ["AI Engineer"]

# What we ASK each source for. Adding a city multiplies daily Actor runs.
search_locations = ["Chennai"]
locations = ["Chennai", "Remote"]
salary_floor_inr = 1200000
daily_cap = 5
"""


def test_brief_exposes_search_locations(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text(BRIEF, encoding="utf-8")
    brief = load_brief(p)
    assert brief.search_locations == ["Chennai"]
    assert brief.remote_ok is True
    assert brief.gate_threshold == 72


def test_brief_requires_at_least_one_search_location(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('target_titles = ["X"]\nsearch_locations = []\n', encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        load_brief(p)


def test_brief_rejects_zero_daily_cap(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('target_titles = ["X"]\nsearch_locations = ["Chennai"]\n'
                 'daily_cap = 0\n', encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        load_brief(p)


def test_load_boards(tmp_path):
    p = tmp_path / "boards.toml"
    p.write_text('[[board]]\nprovider = "greenhouse"\ntoken = "anthropic"\n'
                 'company = "Anthropic"\ntier = 1\n', encoding="utf-8")
    boards = load_boards(p)
    assert boards[0].provider == "greenhouse"
    assert boards[0].token == "anthropic"


def test_save_brief_preserves_comments(tmp_path):
    p = tmp_path / "brief.toml"
    p.write_text(COMMENTED_BRIEF, encoding="utf-8")
    brief = load_brief(p)
    brief.daily_cap = 9
    save_brief(p, brief)
    text = p.read_text(encoding="utf-8")
    assert "# Single source of truth for search and filter settings." in text
    assert "# What we ASK each source for." in text
    assert load_brief(p).daily_cap == 9


def test_save_brief_round_trips_list_fields(tmp_path):
    p = tmp_path / "brief.toml"
    p.write_text(COMMENTED_BRIEF, encoding="utf-8")
    brief = load_brief(p)
    brief.target_titles = ["AI Engineer", "ML Engineer"]
    brief.excluded_companies = ["BadCo"]
    save_brief(p, brief)
    reloaded = load_brief(p)
    assert reloaded.target_titles == ["AI Engineer", "ML Engineer"]
    assert reloaded.excluded_companies == ["BadCo"]


def test_save_brief_omits_an_unset_salary_floor(tmp_path):
    """TOML has no null. An unset floor must be ABSENT, not 0 -- 0 would
    mean 'reject nothing', which is a different statement."""
    p = tmp_path / "brief.toml"
    p.write_text(COMMENTED_BRIEF, encoding="utf-8")
    brief = load_brief(p)
    brief.salary_floor_inr = None
    save_brief(p, brief)
    assert "salary_floor_inr" not in p.read_text(encoding="utf-8")
    assert load_brief(p).salary_floor_inr is None


def test_save_brief_preserves_multiline_array_formatting_when_untouched(tmp_path):
    """Assigning to a tomlkit item wholesale replaces its formatting, even
    when the new value equals the old one. A save that only changes
    daily_cap must not collapse an unrelated hand-wrapped array."""
    wrapped = (
        'target_titles = ["AI Engineer"]\n'
        'title_families = ["ai engineer", "machine learning engineer",\n'
        '                  "applied ai engineer", "ml engineer", "data scientist"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 5\n'
    )
    p = tmp_path / "brief.toml"
    p.write_text(wrapped, encoding="utf-8")
    brief = load_brief(p)
    brief.daily_cap = 9
    save_brief(p, brief)
    text = p.read_text(encoding="utf-8")
    assert (
        'title_families = ["ai engineer", "machine learning engineer",\n'
        '                  "applied ai engineer", "ml engineer", "data scientist"]'
    ) in text
    assert load_brief(p).daily_cap == 9
    assert load_brief(p).title_families == [
        "ai engineer", "machine learning engineer",
        "applied ai engineer", "ml engineer", "data scientist",
    ]


def test_save_brief_creates_a_file_that_does_not_exist(tmp_path):
    p = tmp_path / "new.toml"
    save_brief(p, CareerBrief(target_titles=["AI Engineer"],
                              search_locations=["Chennai"]))
    assert load_brief(p).target_titles == ["AI Engineer"]


def test_save_brief_never_exposes_a_truncated_file(tmp_path, monkeypatch):
    """save_brief must not truncate the live file: the worker's guard() and
    the dashboard routes call load_brief on every tick and every request, so
    a reader landing mid-write would get an unparseable TOML and fail a run
    or 500 a page. The write goes to a sibling temp file and is swapped in
    with os.replace, so a reader sees either the old brief or the new one."""
    p = tmp_path / "brief.toml"
    p.write_text(BRIEF, encoding="utf-8")
    brief = load_brief(p)
    brief.daily_cap = 9

    seen = []
    real_replace = os.replace

    def spy(src, dst):
        # what a concurrent reader would see at the last possible instant
        # before the swap -- under write_text this was empty or partial
        seen.append(Path(dst).read_bytes())
        return real_replace(src, dst)

    monkeypatch.setattr("career_agent.config.os.replace", spy)
    save_brief(p, brief)

    assert seen, "save_brief no longer goes through os.replace"
    still_readable = CareerBrief(**tomllib.loads(seen[0].decode("utf-8")))
    assert still_readable.daily_cap == 5, \
        "the old brief must stay whole and parseable right up to the swap"
    assert load_brief(p).daily_cap == 9
    assert list(tmp_path.iterdir()) == [p], "left a stray temp file behind"


def test_save_brief_leaves_no_temp_file_when_the_write_fails(tmp_path,
                                                             monkeypatch):
    p = tmp_path / "brief.toml"
    p.write_text(BRIEF, encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("career_agent.config.os.replace", boom)
    with pytest.raises(OSError):
        save_brief(p, load_brief(p))

    assert list(tmp_path.iterdir()) == [p], "left a stray temp file behind"
    assert p.read_text(encoding="utf-8") == BRIEF


CANDIDATE = """
candidate_name = "Jane Doe"
candidate_email = "jane@example.com"
candidate_phone = "+91-90000-00000"
"""


def test_load_candidate_profile(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    p.write_text(CANDIDATE, encoding="utf-8")
    profile = load_candidate_profile(p)
    assert profile.candidate_name == "Jane Doe"
    assert profile.candidate_email == "jane@example.com"
    assert profile.linkedin_url is None


def test_load_candidate_profile_missing_file_raises_clearly(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    with pytest.raises(FileNotFoundError, match="candidate_profile.toml.example"):
        load_candidate_profile(p)


def test_candidate_profile_rejects_blank_name(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    p.write_text('candidate_name = ""\ncandidate_email = "a@b.com"\n'
                 'candidate_phone = "123"\n', encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        load_candidate_profile(p)


def test_save_candidate_profile_round_trips(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    save_candidate_profile(p, CandidateProfile(
        candidate_name="Jane Doe", candidate_email="jane@example.com",
        candidate_phone="+91-90000-00000", linkedin_url="https://linkedin.com/in/jane"))
    reloaded = load_candidate_profile(p)
    assert reloaded.candidate_name == "Jane Doe"
    assert reloaded.linkedin_url == "https://linkedin.com/in/jane"


def test_save_candidate_profile_creates_a_file_that_does_not_exist(tmp_path):
    p = tmp_path / "new.toml"
    save_candidate_profile(p, CandidateProfile(
        candidate_name="X", candidate_email="x@y.com", candidate_phone="1"))
    assert load_candidate_profile(p).candidate_name == "X"


def _full_profile(**kw):
    from career_agent.config import Address, EduEntry, WorkEntry
    d = dict(candidate_name="Jane Doe", candidate_email="jane@example.com",
             candidate_phone="+91-1", gender="female",
             address=Address(line1="1 Main St", city="Chennai", state="TN",
                             postal_code="600001", country="India"),
             work_history=[WorkEntry(company="Acme", title="Engineer",
                                     start="2022-01", current=True,
                                     description="Built things"),
                           WorkEntry(company="Old Co", title="Intern",
                                     start="2020-06", end="2021-12")],
             education=[EduEntry(institution="IIT", degree="B.Tech",
                                 field="CS", start="2016", end="2020")])
    d.update(kw)
    return CandidateProfile(**d)


def test_full_profile_round_trips(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    profile = _full_profile()
    save_candidate_profile(p, profile)
    assert load_candidate_profile(p) == profile


def test_flat_profile_loads_defaults_and_save_adds_sections_keeping_comments(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    p.write_text("# keep me\n" + CANDIDATE, encoding="utf-8")
    loaded = load_candidate_profile(p)
    assert loaded.gender == "decline"
    assert loaded.address.city == ""
    assert loaded.work_history == [] and loaded.education == []

    save_candidate_profile(p, _full_profile())
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# keep me\n")
    assert "[address]" in text and "[[work_history]]" in text
    assert load_candidate_profile(p) == _full_profile()


def test_replacing_work_history_writes_exactly_one_entry(tmp_path):
    from career_agent.config import WorkEntry
    p = tmp_path / "candidate_profile.toml"
    three = [WorkEntry(company=f"C{i}", title="T") for i in range(3)]
    save_candidate_profile(p, _full_profile(work_history=three))
    assert p.read_text(encoding="utf-8").count("[[work_history]]") == 3
    save_candidate_profile(p, _full_profile(work_history=[WorkEntry(company="Z", title="T")]))
    assert p.read_text(encoding="utf-8").count("[[work_history]]") == 1
    assert [w.company for w in load_candidate_profile(p).work_history] == ["Z"]


def test_unchanged_sections_keep_their_formatting(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    save_candidate_profile(p, _full_profile())
    text = p.read_text(encoding="utf-8").replace(
        '[address]\n', '[address]  # home\n')
    p.write_text(text, encoding="utf-8")
    save_candidate_profile(p, _full_profile(candidate_phone="+91-2"))
    assert "[address]  # home" in p.read_text(encoding="utf-8")
