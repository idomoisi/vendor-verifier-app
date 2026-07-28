"""Unit proofs for Coralogix Vendor Verifier similarity / sanitize helpers (P3/P4/P6)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PIPELINE_DIR = Path(__file__).resolve().parent / "pipeline"
sys.path.insert(0, str(_PIPELINE_DIR))

from vendor_verifier_similarity import (  # noqa: E402
    find_similar,
    gate2_should_add_alias,
    normalize_cmp,
    sanitize_vendor_name_raw,
    should_skip_control_ghost,
)

DUPLICATE_THRESHOLD = 0.90


def test_find_similar_hanwha_techwin_america_containment() -> None:
    registry = ["Hanwha Techwin", "Other Corp"]
    results = find_similar(
        "Hanwha Techwin America", registry, duplicate_threshold=DUPLICATE_THRESHOLD
    )
    assert results, "expected at least one match"
    name, score, match_type = results[0]
    assert name == "Hanwha Techwin"
    assert score >= DUPLICATE_THRESHOLD
    assert match_type == "word-overlap"


def test_normalize_cmp_thomas_krenn_punctuation() -> None:
    assert normalize_cmp("Thomas-Krenn.AG") == normalize_cmp("Thomas Krenn")


def test_find_similar_thomas_krenn_strong_match() -> None:
    registry = ["Thomas Krenn", "Other Corp"]
    results = find_similar(
        "Thomas-Krenn.AG", registry, duplicate_threshold=DUPLICATE_THRESHOLD
    )
    assert results
    name, score, match_type = results[0]
    assert name == "Thomas Krenn"
    assert score >= DUPLICATE_THRESHOLD
    assert match_type in {"exact", "word-overlap", "fuzzy"}


@pytest.mark.parametrize(
    "input_name,hijack",
    [
        ("Insyde Software Corp.", "Software"),
        ("Shenzhen RealBom Intelligent Co.", "Intelligent"),
        ("TES Touch Embedded Solutions", "Embedded"),
        ("BCM Advanced Research", "Advanced"),
    ],
)
def test_find_similar_rejects_generic_single_token_hijacks(
    input_name: str, hijack: str
) -> None:
    registry = [hijack, "Unrelated Vendor Name"]
    results = find_similar(
        input_name, registry, duplicate_threshold=DUPLICATE_THRESHOLD
    )
    strong_hijacks = [
        (n, s, t) for n, s, t in results if n == hijack and s >= DUPLICATE_THRESHOLD
    ]
    assert not strong_hijacks, (
        f"{hijack!r} must not be a ≥0.90 duplicate for {input_name!r}"
    )


def test_find_similar_dell_bell_not_strong_duplicate() -> None:
    results = find_similar("Dell", ["Bell"], duplicate_threshold=DUPLICATE_THRESHOLD)
    strong = [r for r in results if r[1] >= DUPLICATE_THRESHOLD]
    assert not strong, f"Dell/Bell must not meet duplicate bar, got {results}"


def test_find_similar_dell_technologies_bell_not_strong_duplicate() -> None:
    results = find_similar(
        "Dell Technologies",
        ["Bell Technologies"],
        duplicate_threshold=DUPLICATE_THRESHOLD,
    )
    strong = [r for r in results if r[1] >= DUPLICATE_THRESHOLD]
    assert not strong, (
        f"Dell Technologies/Bell Technologies must not meet duplicate bar, got {results}"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "\x06ell Inc.",
        "\x07ell Inc.",
        "D\x06ll Inc.",
        "D\x07ll Inc.",
    ],
)
def test_sanitize_control_byte_dell_ghosts(raw: str) -> None:
    sanitized = sanitize_vendor_name_raw(raw)
    assert "\x06" not in sanitized and "\x07" not in sanitized
    assert should_skip_control_ghost(raw, sanitized) is True


def test_sanitize_collapses_control_variants_to_same_key() -> None:
    a = sanitize_vendor_name_raw("\x06ell Inc.")
    b = sanitize_vendor_name_raw("\x07ell Inc.")
    assert a == b
    assert a == "ell Inc."


def test_sanitize_clean_dell_not_skipped() -> None:
    raw = "Dell Inc."
    sanitized = sanitize_vendor_name_raw(raw)
    assert sanitized == "Dell Inc."
    assert should_skip_control_ghost(raw, sanitized) is False


def test_sanitize_strips_zero_width() -> None:
    raw = "Dell\u200b Inc."
    sanitized = sanitize_vendor_name_raw(raw)
    assert "\u200b" not in sanitized
    assert sanitized == "Dell Inc."


def test_sanitize_empty_is_ghost() -> None:
    raw = "\x00\x01\x02"
    sanitized = sanitize_vendor_name_raw(raw)
    assert sanitized == ""
    assert should_skip_control_ghost(raw, sanitized) is True


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("SOFTWARE-ONLY", False),
        ("SUSPICIOUS", False),
        ("LEGIT", True),
        (None, False),
        ("", False),
    ],
)
def test_gate2_should_add_alias(verdict: str | None, expected: bool) -> None:
    assert gate2_should_add_alias(verdict) is expected
