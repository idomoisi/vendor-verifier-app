"""PascalCase enum generation — keep in sync with vendor-verifier-app/normalization.py."""

from __future__ import annotations

import re

_LEGAL_SUFFIXES = (
    ", Inc.",
    ", Inc",
    ", Corp.",
    ", Corp",
    ", Ltd.",
    ", Ltd",
    ", LLC",
    ", GmbH",
    ", Co.",
    ", Co",
    " Inc.",
    " Inc",
    " Corp.",
    " Corp",
    " Ltd.",
    " Ltd",
    " GmbH",
    " AG",
    " SA",
    " PLC",
)


def clean_official_name(name: str) -> str:
    name = re.split(r",\s+a\s+", name, maxsplit=1)[0]
    name = re.split(r"\s+d\.b\.a\.", name, flags=re.IGNORECASE, maxsplit=1)[0]
    name = re.split(r"\s+-\s+[Aa]\s+", name, maxsplit=1)[0]
    name = re.split(r"\s+\(", name, maxsplit=1)[0]
    for suffix in _LEGAL_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip(" ,;-")


def _to_pascal_word(word: str) -> str:
    if not word:
        return ""
    if any(c.isupper() for c in word[1:]):
        return word[0].upper() + word[1:]
    return word[0].upper() + word[1:].lower()


def generate_enum_name(display_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s-]", "", display_name)
    parts = [part for part in re.split(r"[\s-]+", cleaned) if part]
    enum_name = "".join(_to_pascal_word(word) for word in parts)
    if enum_name and enum_name[0].isdigit():
        enum_name = f"_{enum_name}"
    return enum_name
