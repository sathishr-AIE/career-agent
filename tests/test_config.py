import pydantic
import pytest

from career_agent.config import load_boards, load_brief

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
