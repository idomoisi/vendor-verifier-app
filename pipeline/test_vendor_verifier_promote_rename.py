"""Regression coverage for promote_rename in Coralogix batch PRs."""

from vendor_verifier_batch_pr import (
    BatchPrRow,
    _edit_oui_info_ieee_promote,
    apply_batch_edits,
)
from vendor_verifier_ieee_manuf import (
    IeeeManufIdentity,
    find_exact_ieee_manuf,
    ieee_promote_pr_case,
    parse_ieee_manuf_pairs,
    resolve_ieee_manuf,
)
from vendor_verifier_similarity import (
    find_manuf_matches,
    pick_promote_manuf,
    promote_pr_case,
)


DEVICE_PY = '''class Vendor:
    Existing = "Existing"

    # Vendors from manuf file #
    AdvancedRfTechnologies = "Advanced Rf Technologies", VendorSource.Manuf

VENDOR_ALIASES = {
}

assert all(
    isinstance(value, tuple)
)
'''

TYPES_PY = '''class VulnerabilityRelevanceSource:
    Existing = "Existing"

    def get_description(self):
        pass

manufacturer_sources = {
}


VULNERABILITY_SOURCE_STR_TO_TYPE = {}
'''

OUI_INFO = '''OUI_INFO = {
    "Advanced Rf Technologies Inc": Vendor.AdvancedRfTechnologies,
    "ADRF": Vendor.AdvancedRfTechnologies,
    "Legacy": Vendor.AdvancedRfTechnologiesLegacy,
}
'''


def _row(**overrides) -> BatchPrRow:
    values = {
        "vendor_name_raw": "Advanced Rf Technologies",
        "jira_ticket": "NET-14837",
        "status": "COMPLETED",
        "verdict": "LEGIT",
        "confidence_score": "HIGH",
        "official_name": "Advanced RF Technologies, Inc.",
        "enum_name": "AdvancedRFTechnologies",
        "duplicate_of": None,
        "should_add_alias": False,
        "website": "https://www.adrftech.com",
        "hardware_evidence": "Hardware",
        "supported_protocols": "[]",
        "pr_case": "promote_rename",
        "manuf_enum": "AdvancedRfTechnologies",
        "manuf_original_display": "Advanced Rf Technologies",
    }
    values.update(overrides)
    return BatchPrRow(**values)


def test_typed_manuf_hit_wins_and_classifies_rename():
    pairs = [
        ("AdvancedRfTechnologies", "Advanced Rf Technologies"),
        ("Other", "Other"),
    ]
    typed = find_manuf_matches("Advanced Rf Technologies", pairs)
    official = find_manuf_matches("Advanced RF Technologies, Inc.", pairs)
    picked = pick_promote_manuf(typed, official)

    assert picked is not None
    assert picked[0] == "AdvancedRfTechnologies"
    assert (
        promote_pr_case(
            "AdvancedRFTechnologies",
            "Advanced RF Technologies, Inc.",
            picked[0],
            picked[1],
        )
        == "promote_rename"
    )


def test_batch_promote_rename_updates_device_types_and_all_oui_references():
    device, types, oui, applied = apply_batch_edits(
        DEVICE_PY, TYPES_PY, OUI_INFO, [_row()]
    )

    assert len(applied) == 1
    assert "VendorSource.Manuf" not in device
    assert 'AdvancedRFTechnologies = "Advanced RF Technologies, Inc."' in device
    assert '"Advanced Rf Technologies"' in device
    assert "AdvancedRFTechnologies" in types
    assert "Vendor.AdvancedRfTechnologies," not in oui
    assert oui.count("Vendor.AdvancedRFTechnologies") == 2
    assert "Vendor.AdvancedRfTechnologiesLegacy" in oui


def test_invalid_promote_is_omitted_without_poisoning_valid_batch_rows():
    invalid = _row(
        vendor_name_raw="Broken",
        enum_name="",
        manuf_enum=None,
        manuf_original_display=None,
    )
    device, _types, oui, applied = apply_batch_edits(
        DEVICE_PY, TYPES_PY, OUI_INFO, [invalid, _row()]
    )

    assert [row.vendor_name_raw for row in applied] == ["Advanced Rf Technologies"]
    assert "AdvancedRFTechnologies" in device
    assert "Vendor.AdvancedRfTechnologies," not in oui


def test_ieee_exact_official_match_strips_same_legal_suffixes():
    identities = parse_ieee_manuf_pairs(
        "00:08:87\tmaschinenfab\tMaschinenfabrik Reinhausen GmbH\n"
    )

    identity = find_exact_ieee_manuf("Maschinenfabrik Reinhausen", identities)

    assert identity == IeeeManufIdentity(
        "maschinenfab", "Maschinenfabrik Reinhausen GmbH"
    )
    assert (
        ieee_promote_pr_case("Reinhausen", "Reinhausen", identity)
        == "promote_rename"
    )


def test_batch_ieee_promote_rename_inserts_oui_keys_without_deleting_manuf_row():
    row = _row(
        vendor_name_raw="Reinhausen",
        official_name="Reinhausen",
        enum_name="Reinhausen",
        manuf_enum=None,
        manuf_original_display="Maschinenfabrik Reinhausen GmbH",
        manuf_source="ieee",
        manuf_ieee_short="maschinenfab",
    )

    device, types, oui, applied = apply_batch_edits(
        DEVICE_PY, TYPES_PY, "OUI_TO_VENDOR = {\n}\n", [row]
    )

    assert applied == [row]
    assert (
        'AdvancedRfTechnologies = "Advanced Rf Technologies", VendorSource.Manuf'
        in device
    )
    assert 'Reinhausen = "Reinhausen"' in device
    assert '"Maschinenfabrik Reinhausen GmbH"' in device
    assert "Reinhausen" in types
    assert '"MASCHINENFAB": Vendor.Reinhausen' in oui


def test_ieee_insert_rejects_mapping_collision():
    try:
        _edit_oui_info_ieee_promote(
            'OUI_TO_VENDOR = {\n'
            '    "MASCHINENFAB": Vendor.Other,\n'
            "}\n",
            "Maschinenfabrik Reinhausen GmbH",
            "maschinenfab",
            "Reinhausen",
        )
    except ValueError as exc:
        assert "Vendor.Other" in str(exc)
    else:
        raise AssertionError("Expected collision")


def test_existing_ieee_mapping_resolves_as_alias():
    identity = IeeeManufIdentity(
        "maschinenfab", "Maschinenfabrik Reinhausen GmbH"
    )

    resolution = resolve_ieee_manuf(
        "Maschinenfabrik Reinhausen",
        "MaschinenfabrikReinhausen",
        "Maschinenfabrik Reinhausen",
        [identity],
        {identity.long_name: "Existing"},
        {"Existing": "Existing Vendor"},
    )

    assert resolution is not None
    assert resolution.pr_case == "alias"
    assert resolution.alias_target_enum == "Existing"
