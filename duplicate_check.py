"""Duplicate/similarity detection against existing vendor registry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.70
HIGH_SIMILARITY = 0.90
TOP_N = 5


@dataclass
class SimilarVendor:
    name: str
    score: float
    match_type: str


def normalize_for_comparison(name: str) -> str:
    n = name.lower().strip()
    for suffix in [
        " inc.", " inc", " corp.", " corp", " ltd.", " ltd",
        " llc", " gmbh", " co.", " co", " ag", " sa", " plc",
        " international", " intl", " technologies", " technology",
    ]:
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


def find_similar(input_name: str, registry: list[str]) -> list[SimilarVendor]:
    results: list[SimilarVendor] = []
    seen: set[str] = set()
    input_norm = normalize_for_comparison(input_name)
    input_words = set(input_norm.split())

    for candidate in registry:
        cand_norm = normalize_for_comparison(candidate)
        if len(cand_norm) < 3:
            continue

        if input_norm == cand_norm:
            if candidate not in seen:
                seen.add(candidate)
                results.append(SimilarVendor(candidate, 1.0, "exact"))
            continue

        ratio = SequenceMatcher(None, input_norm, cand_norm).ratio()
        if ratio >= SIMILARITY_THRESHOLD and candidate not in seen:
            seen.add(candidate)
            results.append(SimilarVendor(candidate, ratio, "fuzzy"))
            continue

        cand_words = set(cand_norm.split())
        shared = input_words & cand_words
        if len(shared) >= 2 or (len(shared) == 1 and len(input_words) == 1 and len(cand_words) == 1):
            shorter = input_words if len(input_words) <= len(cand_words) else cand_words
            overlap = len(shared) / len(shorter) if shorter else 0
            if overlap >= 0.5 and candidate not in seen:
                seen.add(candidate)
                results.append(SimilarVendor(candidate, 0.65 + overlap * 0.25, "substring"))

    results.sort(key=lambda x: x.score, reverse=True)
    return results[:TOP_N]


def is_duplicate(similar: list[SimilarVendor]) -> bool:
    return bool(similar) and (similar[0].match_type == "exact" or similar[0].score >= HIGH_SIMILARITY)


def find_parent_brand_match(official: str, registry: list[str]) -> SimilarVendor | None:
    """Match compound names like 'Molex - Woodhead Software' to parent vendor Molex."""
    official_stripped = official.strip()
    if not official_stripped:
        return None

    registry_by_norm: dict[str, str] = {}
    for cand in registry:
        cand_norm = normalize_for_comparison(cand)
        if len(cand_norm) >= 3:
            registry_by_norm.setdefault(cand_norm, cand)

    first_segment = re.split(r"\s*[-–|/]\s*", official_stripped, maxsplit=1)[0].strip()
    first_norm = normalize_for_comparison(first_segment)
    if first_norm in registry_by_norm:
        return SimilarVendor(registry_by_norm[first_norm], 0.98, "parent-brand")

    official_norm = normalize_for_comparison(official_stripped)
    for cand_norm, cand in registry_by_norm.items():
        if len(cand_norm) < 4:
            continue
        if official_norm.startswith(cand_norm + " ") and official_norm != cand_norm:
            return SimilarVendor(cand, 0.96, "parent-prefix")

    return None


def detect_existing_vendor_for_normalized_name(
    vendor_name: str,
    official: str,
    registry: list[str],
) -> tuple[bool, str | None, str]:
    """Return (is_alias_case, canonical_vendor_name, match_type) against codebase registry only."""
    if vendor_name.strip().lower() == official.strip().lower():
        return False, None, ""

    parent = find_parent_brand_match(official, registry)
    if parent:
        return True, parent.name, parent.match_type

    similar_norm = find_similar(official, registry)
    if is_duplicate(similar_norm):
        return True, similar_norm[0].name, similar_norm[0].match_type

    return False, None, ""
