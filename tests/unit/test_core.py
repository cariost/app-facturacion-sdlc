import itertools

import pytest
from pydantic import ValidationError

from specforge.core.config import SCHEMA_EMBEDDING_DIM, Settings
from specforge.core.enums import TERMINAL_PHASES, Phase
from specforge.core.phases import ALLOWED_TRANSITIONS, is_allowed, transition_edges
from specforge.db.migrate import discover_migrations
from specforge.db.repositories import content_hash


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestSettings:
    def test_dsn_normalization(self) -> None:
        s = _settings(database_url="postgresql+psycopg://u:p@h:5432/db")
        assert s.libpq_url == "postgresql://u:p@h:5432/db"
        assert s.sqlalchemy_url == "postgresql+psycopg://u:p@h:5432/db"

    def test_rejects_unknown_scheme(self) -> None:
        with pytest.raises(ValidationError):
            _settings(database_url="mysql://u:p@h/db")

    def test_embedding_dim_must_match_schema(self) -> None:
        assert _settings().embedding_dim == SCHEMA_EMBEDDING_DIM
        with pytest.raises(ValidationError):
            _settings(embedding_dim=768)

    def test_retry_budget_capped_at_three(self) -> None:
        with pytest.raises(ValidationError):
            _settings(max_dev_qa_retries=4)


class TestPhaseGraph:
    def test_every_phase_has_entry(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == set(Phase)

    def test_terminal_phases_have_no_exits(self) -> None:
        for phase in TERMINAL_PHASES:
            assert ALLOWED_TRANSITIONS[phase] == frozenset()

    def test_no_self_loops(self) -> None:
        assert all(src != dst for src, dst in transition_edges())

    def test_happy_path_and_retry(self) -> None:
        path = [
            Phase.INTAKE_PO,
            Phase.STORY_REFINEMENT,
            Phase.BLUEPRINT_DESIGN,
            Phase.HUMAN_GATE,
            Phase.IMPLEMENTATION,
            Phase.QA_VERIFICATION,
            Phase.IMPLEMENTATION,
            Phase.QA_VERIFICATION,
            Phase.COMPLETED,
        ]
        assert all(is_allowed(a, b) for a, b in itertools.pairwise(path))
        assert not is_allowed(Phase.INTAKE_PO, Phase.IMPLEMENTATION)
        assert not is_allowed(Phase.BLUEPRINT_DESIGN, Phase.IMPLEMENTATION)

    def test_completed_reachable_only_from_qa(self) -> None:
        sources = {src for src, dst in transition_edges() if dst is Phase.COMPLETED}
        assert sources == {Phase.QA_VERIFICATION}


def test_content_hash_is_canonical() -> None:
    assert content_hash({"b": 1, "a": [1, 2]}) == content_hash({"a": [1, 2], "b": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_migrations_are_discovered_in_order() -> None:
    versions = [m.version for m in discover_migrations()]
    assert versions == sorted(versions)
    assert versions[0] == "0001"
