import pytest

from specforge.context_packs import PackFormatError, load_seed_packs, parse_pack
from specforge.core.enums import SEED_MODULES

PACKS = {p.pack_code: p for p in load_seed_packs()}
INDUSTRY_TERMS = ("taller", "vehículo", "automotriz", "odometro", "kilometraje")


def test_every_seed_module_has_a_base_pack() -> None:
    for module in SEED_MODULES:
        assert PACKS[module].is_base
        assert PACKS[module].industry is None


def test_pack_codes_match_their_module() -> None:
    for code, pack in PACKS.items():
        assert code.split(".", 1)[0] == pack.module


def test_agnostic_packs_contain_no_industry_vocabulary() -> None:
    for pack in PACKS.values():
        if pack.industry is None:
            text = pack.content.lower()
            assert not [t for t in INDUSTRY_TERMS if t in text], pack.pack_code


def test_crm_reminders_cover_time_usage_and_cycle_policies() -> None:
    content = PACKS["CRM.reminders"].content
    for key in (
        "reminders.time.interval",
        "reminders.usage.counter",
        "reminders.usage.threshold",
        "reminders.cycle.rule",
        "reminders.combine",
    ):
        assert key in content
    assert "Consentimiento revocado" in content
    inquiries = PACKS["CRM.inquiries"].content
    assert "inquiries.handoff_after_turns" in inquiries


def test_maintenance_is_only_a_configuration_overlay() -> None:
    overlay = PACKS["CRM.examples.automotive"]
    assert overlay.industry == "automotriz"
    assert "reminders.usage.threshold = 5000" in overlay.content


def test_chunks_follow_sections_and_are_deterministic() -> None:
    pack = PACKS["CRM.reminders"]
    chunks = pack.chunks()
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[1].heading == "Tipos de política"
    assert all(c.content.startswith(f"# {pack.title}") for c in chunks)
    assert chunks == pack.chunks()


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("sin front matter", "front matter"),
        ('+++\npack_code = "crm"\nmodule = "CRM"\ntitle = "x"\n+++\nx', "pack_code inválido"),
        ('+++\npack_code = "CRM.x"\nmodule = "SUNAT"\ntitle = "x"\n+++\nx', "prefijo"),
        ('+++\npack_code = "CRM"\nmodule = "CRM"\ntitle = "x"\nfoo = 1\n+++\nx', "desconocidas"),
        ('+++\npack_code = "CRM"\nmodule = "CRM"\n+++\nx', "falta title"),
    ],
)
def test_invalid_pack_files_are_rejected(text: str, match: str) -> None:
    with pytest.raises(PackFormatError, match=match):
        parse_pack(text, source="test.md")
