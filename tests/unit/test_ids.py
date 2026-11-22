import pytest

from specforge.core.ids import FeatureId, InvalidIdentifierError, ThreadId


@pytest.mark.parametrize(
    ("raw", "module", "seq"),
    [
        ("CRM-001", "CRM", 1),
        ("SUNAT-014", "SUNAT", 14),
        ("SIRE-003", "SIRE", 3),
        ("CORE-1000", "CORE", 1000),
    ],
)
def test_feature_id_roundtrip(raw: str, module: str, seq: int) -> None:
    fid = FeatureId.parse(raw)
    assert (fid.module, fid.seq) == (module, seq)
    assert str(fid) == raw


@pytest.mark.parametrize("raw", ["crm-001", "CRM-01", "CRM001", "C-001", "CRM-0001", "CRM-000"])
def test_feature_id_rejects_non_canonical(raw: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        FeatureId.parse(raw)


def test_thread_id_format_and_progression() -> None:
    thread = ThreadId(FeatureId("CRM", 1), blueprint_version=1, run_seq=1)
    assert str(thread) == "sf:CRM-001:v1:01"
    assert str(thread.next_run()) == "sf:CRM-001:v1:02"
    assert str(thread.next_run().next_blueprint()) == "sf:CRM-001:v2:01"
    assert ThreadId.parse("sf:SUNAT-014:v3:12") == ThreadId(FeatureId("SUNAT", 14), 3, 12)
    assert str(ThreadId(FeatureId("CRM", 1), 1, 100)) == "sf:CRM-001:v1:100"


@pytest.mark.parametrize(
    "raw", ["sf:CRM-001:v0:01", "sf:CRM-001:v1:1", "CRM-001:v1:01", "sf:CRM-001:v1:001"]
)
def test_thread_id_rejects_invalid(raw: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        ThreadId.parse(raw)
