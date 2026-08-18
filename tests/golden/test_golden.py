import json
import os
from pathlib import Path

import pytest

from career_agent import db, store
from career_agent.config import load_brief
from career_agent.gate import score
from career_agent.models import Job

CASES = json.loads((Path(__file__).parent / "listings.json").read_text())


@pytest.mark.skipif(not os.environ.get("RUN_GOLDEN"),
                    reason="set RUN_GOLDEN=1 to run against the live model")
async def test_gate_agrees_with_hand_labels():
    from career_agent.run import _ask

    brief = load_brief(Path("career_brief.toml"))
    conn = db.connect(Path("data/career.db"))
    db.init_schema(conn)
    facts = store.facts(conn)

    disagreements = []
    for case in CASES:
        verdict = await score(Job(**case["job"]), brief, facts, _ask)
        if verdict.verdict != case["expected"]:
            disagreements.append(
                f"{case['job']['title']} at {case['job']['company']}: "
                f"expected {case['expected']}, got {verdict.verdict} "
                f"({verdict.rationale})")

    if disagreements:
        pytest.fail(f"{len(disagreements)}/{len(CASES)} disagreed:\n"
                    + "\n".join(disagreements))
