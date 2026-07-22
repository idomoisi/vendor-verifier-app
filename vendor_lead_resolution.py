"""Classify raw integration manufacturer strings into vendor lead buckets.

Used by the Databricks notebook ``vendor_leads_discovery.py`` and reusable
offline (e.g. CSV post-processing).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# Mirrors medigator/medetector/detector.py IntegrationDetectorMixin.UNALLOWED_NAMES
UNALLOWED_NAMES = {
    "n/a",
    "to be filled by o.e.m.",
    "to be filled by o.e.m",
    "by o.e.m.",
    "default string",
    "default string default string",
    "system product name",
    "system manufacturer",
    "system_manufacturer",
    "unknown",
    "t. b. d.",
    "none",
    "_",
    "skylake",
    "kabylake",
    "no serial number",
    "enter the serial number",
    "enter the serial num",
    "serial here",
    "storz x3 serial here",
    "#systemserialnum#",
    "manufacturer's serial number",
    "system serial number",
    "invalid",
    "0123456789",
    "1234567890",
    "123456789",
    "oem",
    "o.e.m.",
    "oemyi",
}

# Hypervisor / VM-host strings — not hardware OEM leads
HYPERVISOR_DENYLIST = {
    "xen",
    "qemu",
    "innotek gmbh",
}

# Model-like manufacturer strings (BIOS garbage)
MODEL_LIKE_RE = re.compile(
    r"( series$| optiplex | latitude | precision | thinkcentre | thinkpad | prodesk | elitedesk )",
    re.IGNORECASE,
)

SIMILARITY_THRESHOLD = 0.70
HIGH_SIMILARITY = 0.90


@dataclass(frozen=True)
class Registry:
    """Vendor registry loaded from vendors_master + vendor_aliases Delta tables."""

    display_names: list[str]
    display_to_enum: dict[str, str]
    alias_to_display: dict[str, str]


@dataclass(frozen=True)
class Classification:
    raw_vendor: str
    uid_count: int
    bucket: str
    matched_display_name: str | None
    matched_enum_key: str | None
    fuzzy_score: float | None
    fuzzy_match: str | None
    reason: str


def normalize_vendor_name(name: str) -> str:
    """Lightweight stand-in for xiot_parsing.normalized_vendor_name."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_comparison(name: str) -> str:
    n = name.lower().strip()
    for suffix in (
        " inc.",
        " inc",
        " corp.",
        " corp",
        " ltd.",
        " ltd",
        " llc",
        " gmbh",
        " co.",
        " co",
        " ag",
        " sa",
        " plc",
        " international",
        " intl",
        " technologies",
        " technology",
    ):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


def build_registry(
    vendors_master_rows: list[dict[str, str]],
    vendor_aliases_rows: list[dict[str, str]],
) -> Registry:
    display_to_enum: dict[str, str] = {}
    display_names: list[str] = []
    enum_to_display: dict[str, str] = {}

    for row in vendors_master_rows:
        display = row["display_name"].strip()
        enum_key = row["enum_key"]
        display_names.append(display)
        display_to_enum[display.lower()] = enum_key
        display_to_enum[normalize_vendor_name(display)] = enum_key
        enum_to_display[enum_key] = display

    alias_to_display: dict[str, str] = {}
    for row in vendor_aliases_rows:
        alias = row["alias_name"].strip()
        enum_key = row["enum_key"]
        display = enum_to_display.get(enum_key)
        if not display:
            continue
        alias_to_display[alias.lower()] = display
        alias_to_display[normalize_vendor_name(alias)] = display

    # Canonical display names are also valid aliases
    for display in display_names:
        alias_to_display[display.lower()] = display
        alias_to_display[normalize_vendor_name(display)] = display

    return Registry(
        display_names=display_names,
        display_to_enum=display_to_enum,
        alias_to_display=alias_to_display,
    )


def _display_for_enum(registry: Registry, enum_key: str | None) -> str | None:
    if not enum_key:
        return None
    for display in registry.display_names:
        if registry.display_to_enum.get(display.lower()) == enum_key:
            return display
    return None


def is_hard_noise(raw: str) -> str | None:
    cleaned = raw.strip()
    if not cleaned:
        return "empty"
    lower = cleaned.lower()
    if lower in UNALLOWED_NAMES:
        return "unallowed_name"
    if lower in HYPERVISOR_DENYLIST:
        return "hypervisor_or_vm_host"
    if MODEL_LIKE_RE.search(cleaned):
        return "model_like_string"
    if len(cleaned) < 2:
        return "too_short"
    return None


def resolve_vendor(raw: str, registry: Registry) -> tuple[str | None, str | None, str]:
    """Return (display_name, enum_key, match_type) or (None, None, '')."""
    cleaned = raw.strip()
    lower = cleaned.lower()
    norm = normalize_vendor_name(cleaned)

    if lower in registry.display_to_enum:
        enum_key = registry.display_to_enum[lower]
        return cleaned, enum_key, "exact_display"

    if norm in registry.display_to_enum:
        enum_key = registry.display_to_enum[norm]
        display = _display_for_enum(registry, enum_key) or cleaned
        return display, enum_key, "normalized_display"

    if lower in registry.alias_to_display:
        display = registry.alias_to_display[lower]
        return display, registry.display_to_enum.get(display.lower()), "alias_exact"

    if norm in registry.alias_to_display:
        display = registry.alias_to_display[norm]
        return display, registry.display_to_enum.get(display.lower()), "alias_normalized"

    return None, None, ""


def best_fuzzy_match(raw: str, registry: Registry) -> tuple[str | None, float, str]:
    input_norm = normalize_for_comparison(raw)
    input_words = set(input_norm.split())
    best_name: str | None = None
    best_score = 0.0
    best_type = ""

    for candidate in registry.display_names:
        cand_norm = normalize_for_comparison(candidate)
        if len(cand_norm) < 3:
            continue

        if input_norm == cand_norm:
            return candidate, 1.0, "exact"

        ratio = SequenceMatcher(None, input_norm, cand_norm).ratio()
        if ratio > best_score:
            best_score = ratio
            best_name = candidate
            best_type = "fuzzy"

        cand_words = set(cand_norm.split())
        shared = input_words & cand_words
        if len(shared) >= 2 or (
            len(shared) == 1 and len(input_words) == 1 and len(cand_words) == 1
        ):
            shorter = input_words if len(input_words) <= len(cand_words) else cand_words
            overlap = len(shared) / len(shorter) if shorter else 0.0
            if overlap >= 0.5:
                score = 0.65 + overlap * 0.25
                if score > best_score:
                    best_score = score
                    best_name = candidate
                    best_type = "substring"

    if best_score < SIMILARITY_THRESHOLD:
        return None, best_score, ""
    return best_name, best_score, best_type


def classify_raw_vendor(
    raw_vendor: str,
    uid_count: int,
    registry: Registry,
    *,
    device_vendor_null: bool = True,
) -> Classification:
    noise_reason = is_hard_noise(raw_vendor)
    if noise_reason:
        return Classification(
            raw_vendor=raw_vendor,
            uid_count=uid_count,
            bucket="noise",
            matched_display_name=None,
            matched_enum_key=None,
            fuzzy_score=None,
            fuzzy_match=None,
            reason=noise_reason,
        )

    display, enum_key, match_type = resolve_vendor(raw_vendor, registry)
    if display is not None:
        return Classification(
            raw_vendor=raw_vendor,
            uid_count=uid_count,
            bucket="known_enum",
            matched_display_name=display,
            matched_enum_key=enum_key,
            fuzzy_score=1.0,
            fuzzy_match=display,
            reason=match_type,
        )

    fuzzy_name, fuzzy_score, fuzzy_type = best_fuzzy_match(raw_vendor, registry)
    if fuzzy_name and fuzzy_score >= HIGH_SIMILARITY:
        return Classification(
            raw_vendor=raw_vendor,
            uid_count=uid_count,
            bucket="add_alias",
            matched_display_name=fuzzy_name,
            matched_enum_key=registry.display_to_enum.get(fuzzy_name.lower()),
            fuzzy_score=fuzzy_score,
            fuzzy_match=fuzzy_name,
            reason=fuzzy_type,
        )

    if not device_vendor_null:
        return Classification(
            raw_vendor=raw_vendor,
            uid_count=uid_count,
            bucket="resolved_elsewhere",
            matched_display_name=None,
            matched_enum_key=None,
            fuzzy_score=fuzzy_score if fuzzy_score else None,
            fuzzy_match=fuzzy_name,
            reason="device_already_has_vendor",
        )

    if fuzzy_name and fuzzy_score >= SIMILARITY_THRESHOLD:
        return Classification(
            raw_vendor=raw_vendor,
            uid_count=uid_count,
            bucket="review_fuzzy",
            matched_display_name=None,
            matched_enum_key=None,
            fuzzy_score=fuzzy_score,
            fuzzy_match=fuzzy_name,
            reason=fuzzy_type,
        )

    return Classification(
        raw_vendor=raw_vendor,
        uid_count=uid_count,
        bucket="new_vendor",
        matched_display_name=None,
        matched_enum_key=None,
        fuzzy_score=fuzzy_score if fuzzy_score else None,
        fuzzy_match=fuzzy_name,
        reason="no_registry_match",
    )
