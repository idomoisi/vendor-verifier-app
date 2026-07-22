"""Duplicate/similarity detection against existing vendor registry."""

from __future__ import annotations

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
