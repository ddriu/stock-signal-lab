"""The SQL fixtures must be outputs of the real pure selector, not self-claims."""
import json
from pathlib import Path

from scripts.arena_universe_guard_fixture import build_fixtures


def test_sql_policy_fixtures_match_the_current_real_selector():
    fixture = Path(__file__).parent / "fixtures" / "arena_universe_policy_guard.json"
    assert json.loads(fixture.read_text()) == build_fixtures()
