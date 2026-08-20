from pathlib import Path

import pydantic
import pytest

from career_agent.config import CareerBrief, load_boards, load_brief, save_brief

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


def test_save_brief_creates_a_file_that_does_not_exist(tmp_path):
    p = tmp_path / "new.toml"
    save_brief(p, CareerBrief(target_titles=["AI Engineer"],
                              search_locations=["Chennai"]))
    assert load_brief(p).target_titles == ["AI Engineer"]
