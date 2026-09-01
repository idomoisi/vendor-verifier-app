"""IEEE manuf-file identity parsing and exact official-name attribution."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from vendor_verifier_normalization import clean_official_name, generate_enum_name


@dataclass(frozen=True)
class IeeeManufIdentity:
    short_name: str
    long_name: str


@dataclass(frozen=True)
class IeeeManufResolution:
    identity: IeeeManufIdentity
    pr_case: str
    alias_target_enum: str | None = None
    alias_target_display: str | None = None
    # Populated with (oui_key, current Vendor enum) when the identity's keys
    # cannot be resolved: several targets, or one that is not first-class.
    # The case stays `new`, but the conflict has to be visible in the run log.
    oui_conflicts: tuple[tuple[str, str], ...] = ()


def parse_ieee_manuf_pairs(content: str) -> list[IeeeManufIdentity]:
    identities: list[IeeeManufIdentity] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\t+", line, maxsplit=2)
        if len(parts) < 3:
            continue
        short_name = parts[1].strip()
        long_name = parts[2].split("\t#", 1)[0].strip()
        if not short_name or not long_name:
            continue
        key = (short_name.casefold(), long_name.casefold())
        if key not in seen:
            seen.add(key)
            identities.append(IeeeManufIdentity(short_name, long_name))
    return identities


def parse_oui_vendor_mappings(content: str) -> dict[str, str]:
    mappings: dict[str, str] = {}
    pattern = re.compile(
        r'^\s*("(?:[^"\\]|\\.)*")\s*:\s*Vendor\.(\w+)\s*,',
        re.MULTILINE,
    )
    for key_literal, enum_name in pattern.findall(content):
        try:
            key = ast.literal_eval(key_literal)
        except (SyntaxError, ValueError):
            continue
        if isinstance(key, str):
            mappings[key] = enum_name
    return mappings


def _match_key(name: str) -> str:
    return clean_official_name(name).casefold()


def find_exact_ieee_manuf(
    official_name: str,
    identities: list[IeeeManufIdentity],
) -> IeeeManufIdentity | None:
    target = _match_key(official_name)
    if not target:
        return None
    matches = {
        identity
        for identity in identities
        if target in {_match_key(identity.long_name), _match_key(identity.short_name)}
    }
    return next(iter(matches)) if len(matches) == 1 else None


def ieee_mapping_entries(
    identity: IeeeManufIdentity,
    mappings: dict[str, str],
) -> dict[str, str]:
    """Return existing oui_info keys for this identity mapped to their Vendor."""
    keys = (
        identity.long_name,
        identity.short_name,
        identity.short_name.upper(),
    )
    return {key: mappings[key] for key in dict.fromkeys(keys) if key in mappings}


def ieee_mapping_targets(
    identity: IeeeManufIdentity,
    mappings: dict[str, str],
) -> set[str]:
    return set(ieee_mapping_entries(identity, mappings).values())


def ieee_promote_pr_case(
    enum_name: str,
    display_name: str,
    identity: IeeeManufIdentity,
) -> str:
    source_display = clean_official_name(identity.long_name)
    source_enum = generate_enum_name(source_display)
    if enum_name != source_enum or display_name != source_display:
        return "promote_rename"
    return "promote"


def resolve_ieee_manuf(
    official_name: str,
    enum_name: str,
    display_name: str,
    identities: list[IeeeManufIdentity],
    mappings: dict[str, str],
    enum_displays: dict[str, str],
) -> IeeeManufResolution | None:
    identity = find_exact_ieee_manuf(official_name, identities)
    if identity is None:
        return None
    entries = ieee_mapping_entries(identity, mappings)
    targets = set(entries.values())
    if len(targets) == 1:
        target_enum = next(iter(targets))
        target_display = enum_displays.get(target_enum)
        if target_display:
            return IeeeManufResolution(
                identity,
                "alias",
                alias_target_enum=target_enum,
                alias_target_display=target_display,
            )
    if targets:
        # Several targets, or a single one that is not a first-class Vendor.
        # Promoting would fight the existing keys, so stay `new` and report.
        return IeeeManufResolution(
            identity,
            "new",
            oui_conflicts=tuple(sorted(entries.items())),
        )
    return IeeeManufResolution(
        identity,
        ieee_promote_pr_case(enum_name, display_name, identity),
    )
