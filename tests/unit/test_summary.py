from specforge.context import compact_summary, summary_header
from specforge.context.summary import parse_summary
from specforge.core.enums import Phase
from specforge.schemas import FeatureDeliveryState

STATE = FeatureDeliveryState(
    feature_id="CRM-001", tenant_id="acme", module="CRM", phase=Phase.STORY_REFINEMENT
)
HEADER = summary_header(STATE)


def test_header_reflects_state() -> None:
    assert HEADER == "■ CRM-001 · STORY_REFINEMENT · bp v1 · run 01 · reintentos 0/3"


def test_appends_events_and_regenerates_header() -> None:
    first = compact_summary("", ["intake listo"], header=HEADER)
    second = compact_summary(first, ["US-001 refinada"], header="■ nuevo")
    assert second.splitlines() == ["■ nuevo", "intake listo", "US-001 refinada"]


def test_folds_oldest_events_within_15_lines() -> None:
    summary = ""
    for i in range(40):
        summary = compact_summary(summary, [f"evento {i}"], header=HEADER)
    lines = summary.splitlines()
    assert len(lines) == 15
    assert lines[1] == "… +27 eventos previos (ver feature_phase_history)"
    assert lines[-1] == "evento 39"
    folded, events = parse_summary(summary)
    assert folded + len(events) == 40


def test_respects_char_budget_and_clips_long_lines() -> None:
    entries = [f"{i} " + "x" * 500 for i in range(10)]
    summary = compact_summary("", entries, header=HEADER, max_chars=800)
    assert len(summary) <= 800
    assert all(len(line) <= 200 for line in summary.splitlines())
    assert summary.splitlines()[1].startswith("… +")


def test_skips_consecutive_duplicates_and_blank_entries() -> None:
    summary = compact_summary("", ["QA falló", "QA falló", "  ", "QA ok"], header=HEADER)
    assert summary.splitlines()[1:] == ["QA falló", "QA ok"]


def test_result_is_accepted_by_the_state_contract() -> None:
    summary = ""
    for i in range(30):
        summary = compact_summary(summary, [f"evento {i} " + "y" * 150], header=HEADER)
    STATE.model_copy(update={}).model_validate({**STATE.model_dump(), "executive_summary": summary})
