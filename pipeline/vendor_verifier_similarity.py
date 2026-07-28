"""Similarity + EXTRACT sanitize helpers for Coralogix Vendor Verifier pipeline.

Repo source of truth: pipeline/vendor_verifier_similarity.py
  (https://github.com/idomoisi/vendor-verifier-app)

Deploy copy to: /Workspace/Users/ido.m@claroty.com/vendor-verifier-pipeline-lib/
(same pattern as vendor_verifier_normalization.py).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Generic single-token registry hijacks (finding 5) — lowercased.
OVERLAP_STOPWORDS: frozenset[str] = frozenset(
    {
        "software",
        "intelligent",
        "embedded",
        "advanced",
        "automatic",
        "array",
        "solutions",
        "technology",
        "technologies",
        "systems",
        "company",
        "inc",
        "corp",
        "ltd",
        "group",
        "international",
        "intl",
        "research",
        "electronics",
        "networks",
        "network",
        "digital",
        "global",
        "industrial",
        "medical",
        "security",
        "services",
        "service",
        "industries",
        "industry",
        "holding",
        "holdings",
        "limited",
        "corporation",
        "enterprises",
        "enterprise",
    }
)

_LEGAL_CMP_SUFFIXES = (
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
)

# Zero-width / BOM codepoints stripped at EXTRACT.
_ZERO_WIDTH_CHARS: frozenset[str] = frozenset(
    {
        "\u200b",  # ZERO WIDTH SPACE
        "\u200c",  # ZERO WIDTH NON-JOINER
        "\u200d",  # ZERO WIDTH JOINER
        "\u2060",  # WORD JOINER
        "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
        "\u00ad",  # SOFT HYPHEN
    }
)

_SHORT_FUZZY_LEN = 6
_SHORT_FUZZY_CAP = 0.89  # below DUPLICATE_THRESHOLD (0.90)
_MIN_REAL_NAME_FIRST_TOKEN = 4


def normalize_cmp(name: str) -> str:
    """Lowercase, fold punctuation to spaces, strip common legal suffixes."""
    n = name.lower().strip()
    n = re.sub(r"[^a-z0-9]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    for sfx in _LEGAL_CMP_SUFFIXES:
        if n.endswith(sfx):
            n = n[: -len(sfx)].strip()
            break
    return n


def _length_aware_fuzzy(inp: str, cn: str) -> float:
    """SequenceMatcher ratio with a raised bar for short near-misses (Dell/Bell).

    Caps both bare short strings and longer names that differ only in a short
    first token (``Dell Technologies`` vs ``Bell Technologies`` ~94%).
    """
    ratio = SequenceMatcher(None, inp, cn).ratio()
    if ratio >= 1.0:
        return ratio

    min_len = min(len(inp), len(cn))
    if min_len < _SHORT_FUZZY_LEN and abs(len(inp) - len(cn)) <= 1:
        return min(ratio, _SHORT_FUZZY_CAP)

    # Shared suffix tokens + short first-token 1-char near-miss.
    iw, cw = inp.split(), cn.split()
    if len(iw) >= 2 and len(cw) >= 2 and iw[1:] == cw[1:]:
        a, b = iw[0], cw[0]
        if (
            a != b
            and min(len(a), len(b)) < _SHORT_FUZZY_LEN
            and abs(len(a) - len(b)) <= 1
        ):
            return min(ratio, _SHORT_FUZZY_CAP)

    return ratio


def _word_overlap_score(inp_words: set[str], cand_words: set[str]) -> float | None:
    """Return word-overlap score or None if rules reject the match."""
    shared = inp_words & cand_words
    if not shared:
        return None
    shorter = inp_words if len(inp_words) <= len(cand_words) else cand_words
    longer = cand_words if shorter is inp_words else inp_words
    if len(shared) >= 2:
        allow = True
    elif len(shorter) == 1:
        token = next(iter(shorter))
        allow = token not in OVERLAP_STOPWORDS and shorter <= longer
    else:
        allow = False
    if not allow:
        return None
    overlap = len(shared) / len(shorter) if shorter else 0.0
    if overlap < 0.5:
        return None
    return 0.65 + overlap * 0.25


def find_similar(
    input_name: str,
    registry: list,
    threshold: float = 0.70,
    top_n: int = 5,
    duplicate_threshold: float = 0.90,
) -> list[tuple[str, float, str]]:
    """Rank registry candidates by max(fuzzy, word-overlap); no fuzzy short-circuit.

    ``duplicate_threshold`` is accepted for call-site clarity; callers still compare
    returned scores against their own duplicate bar (typically 0.90).
    """
    del duplicate_threshold  # documented for AC / call sites; scoring is absolute
    results: list[tuple[str, float, str]] = []
    seen: set[str] = set()
    inp = normalize_cmp(input_name)
    inp_words = set(inp.split()) if inp else set()
    for cand in registry:
        cn = normalize_cmp(cand)
        if len(cn) < 3:
            continue
        if inp == cn:
            if cand not in seen:
                seen.add(cand)
                results.append((cand, 1.0, "exact"))
            continue

        best_score = 0.0
        best_type: str | None = None

        ratio = _length_aware_fuzzy(inp, cn)
        if ratio >= threshold:
            best_score = ratio
            best_type = "fuzzy"

        wo = _word_overlap_score(inp_words, set(cn.split()))
        if wo is not None and wo >= threshold and wo > best_score:
            best_score = wo
            best_type = "word-overlap"

        if best_type is not None and cand not in seen:
            seen.add(cand)
            results.append((cand, best_score, best_type))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_n]


def sanitize_vendor_name_raw(raw: str) -> str:
    """Strip control characters (ord < 32) and zero-width / BOM codepoints."""
    out: list[str] = []
    for ch in raw:
        o = ord(ch)
        if o < 32:
            continue
        if ch in _ZERO_WIDTH_CHARS:
            continue
        out.append(ch)
    return "".join(out).strip()


def should_skip_control_ghost(raw: str, sanitized: str) -> bool:
    """True when sanitized is empty or control-byte contamination left a non-name.

    Control-only deltas that leave a short / corrupt first token (e.g. ``\\x06ell`` →
    ``ell``, ``D\\x06ll`` → ``Dll``) must not become VERIFY candidates.
    """
    s = (sanitized or "").strip()
    if not s:
        return True
    if raw == s:
        return False
    # Only differed by control / zero-width removal.
    if sanitize_vendor_name_raw(raw) != s:
        return False
    tokens = s.split()
    if not tokens:
        return True
    first_alnum = "".join(c for c in tokens[0] if c.isalnum())
    if len(first_alnum) < _MIN_REAL_NAME_FIRST_TOKEN:
        return True
    alnum_count = sum(1 for c in s if c.isalnum())
    if alnum_count < _MIN_REAL_NAME_FIRST_TOKEN:
        return True
    return False


def gate2_should_add_alias(verdict: str | None) -> bool:
    """Gate 2 alias PR flag — only LEGIT duplicates may queue aliases."""
    return verdict == "LEGIT"
