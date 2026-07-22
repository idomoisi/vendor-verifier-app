"""Name normalization -- generate enum names from AI-corrected vendor names."""

from __future__ import annotations

import re

_LEGAL_SUFFIXES = (
    ", Inc.", ", Inc", ", Corp.", ", Corp", ", Ltd.", ", Ltd",
    ", LLC", ", GmbH", ", Co.", ", Co", ", AG", ", SA", ", PLC",
    " Inc.", " Inc", " Corp.", " Corp", " Ltd.", " Ltd", " GmbH", " AG",
)


def clean_official_name(name: str) -> str:
    """Strip AI-appended brand/subsidiary notes from the official name.

    Handles patterns like:
      "Datex-Ohmeda, Inc., a General Electric Company, d.b.a. GE HealthCare"
        → "Datex-Ohmeda"
      "Turck (a division of Hans Turck GmbH)"
        → "Turck"
      "Acme - A Siemens Company"
        → "Acme"
    """
    # Cut at the first relationship marker: ", a Brand/Division/Subsidiary..."
    name = re.split(r",\s+a\s+", name, maxsplit=1)[0]
    # Cut at "doing business as"
    name = re.split(r"\s+d\.b\.a\.", name, flags=re.IGNORECASE, maxsplit=1)[0]
    # Cut at " - A ... Company"
    name = re.split(r"\s+-\s+[Aa]\s+", name, maxsplit=1)[0]
    # Cut at opening parenthesis (extra info in parens is never part of the name)
    name = re.split(r"\s+\(", name, maxsplit=1)[0]
    # Strip trailing legal suffixes (may be revealed after the above cuts)
    for suffix in _LEGAL_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip(" ,;-")


def generate_enum_name(display_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", display_name)
    parts = cleaned.split()
    enum_name = "".join(word.capitalize() for word in parts)
    if enum_name and enum_name[0].isdigit():
        enum_name = f"_{enum_name}"
    return enum_name


def check_enum_collision(enum_name: str, registry: list[str]) -> str | None:
    """Returns the colliding vendor name, or None if no collision."""
    enum_lower = enum_name.lower()
    for existing in registry:
        existing_enum = re.sub(r"[^a-zA-Z0-9]", "", existing)
        if existing_enum.lower() == enum_lower:
            return existing
    return None


def generate_alias_snippet(existing_enum: str, alias_name: str, has_existing_entry: bool = True) -> dict[str, str]:
    """Generate code snippet for adding an alias to an existing vendor.

    Args:
        has_existing_entry: True if Vendor.{existing_enum} already has an entry in VENDOR_ALIASES.
                            False if a brand new entry needs to be created.
    """
    if has_existing_entry:
        device_py = (
            f'    Vendor.{existing_enum}: (\n'
            f'        ...,          # existing aliases\n'
            f'        "{alias_name}",  # ← new\n'
            f'    ),'
        )
        diff = (
            f'--- a/medigator/common/domain_model/xiot/device.py\n'
            f'+++ b/medigator/common/domain_model/xiot/device.py\n'
            f'@@ VENDOR_ALIASES @@\n'
            f'     Vendor.{existing_enum}: (\n'
            f'         ...,\n'
            f'+        "{alias_name}",\n'
            f'     ),'
        )
    else:
        device_py = (
            f'    Vendor.{existing_enum}: (\n'
            f'        "{alias_name}",\n'
            f'    ),  # ← new entry'
        )
        diff = (
            f'--- a/medigator/common/domain_model/xiot/device.py\n'
            f'+++ b/medigator/common/domain_model/xiot/device.py\n'
            f'@@ VENDOR_ALIASES @@\n'
            f'+    Vendor.{existing_enum}: (\n'
            f'+        "{alias_name}",\n'
            f'+    ),'
        )
    return {"device_py": device_py, "diff": diff}


def generate_promote_snippet(
    manuf_enum_name: str,
    manuf_display_value: str,
    new_enum_name: str,
    new_display_value: str,
) -> dict[str, str]:
    """Generate code snippet for promoting a VendorSource.Manuf entry to system vendor.

    Args:
        manuf_enum_name: existing enum key in the manuf section (e.g. AvidaSystems)
        manuf_display_value: existing display value in the manuf section (e.g. "AVIDIA Systems")
        new_enum_name: new clean enum key (e.g. AcmeIndustrialSystems)
        new_display_value: new AI-corrected display value (e.g. "Acme Industrial Systems")
    """
    device_py = (
        f'    # Remove from manuf section:\n'
        f'    # {manuf_enum_name} = "{manuf_display_value}", VendorSource.Manuf\n\n'
        f'    # Add before # Vendors from manuf file #:\n'
        f'    {new_enum_name} = "{new_display_value}"'
    )
    types_enum = f'    {new_enum_name} = "{new_display_value}"'
    types_set = f"    VulnerabilityRelevanceSource.{new_enum_name},"
    diff = (
        f'--- a/medigator/common/domain_model/xiot/device.py\n'
        f'+++ b/medigator/common/domain_model/xiot/device.py\n'
        f'@@ VendorSource.Manuf section @@\n'
        f'-    {manuf_enum_name} = "{manuf_display_value}", VendorSource.Manuf\n'
        f'\n'
        f'@@ class Vendor(SerializableStrEnum): @@\n'
        f'+    {new_enum_name} = "{new_display_value}"\n'
        f'     # Vendors from manuf file #\n'
        f'     ...\n\n'
        f'--- a/medigator/common/domain_model/intels/vulnerabilities/types.py\n'
        f'+++ b/medigator/common/domain_model/intels/vulnerabilities/types.py\n'
        f'@@ class VulnerabilityRelevanceSource @@\n'
        f'+    {new_enum_name} = "{new_display_value}"\n'
        f'\n'
        f'@@ manufacturer_sources @@\n'
        f'+    VulnerabilityRelevanceSource.{new_enum_name},'
    )
    return {"device_py": device_py, "types_enum": types_enum, "types_set": types_set, "diff": diff}


def generate_manuf_snippet(enum_name: str, display_value: str) -> dict[str, str]:
    """Generate code snippet for a manuf-file vendor (VendorSource.Manuf)."""
    device_py = f'    {enum_name} = "{display_value}", VendorSource.Manuf'
    types_enum = f'    {enum_name} = "{display_value}"'
    types_set = f"    VulnerabilityRelevanceSource.{enum_name},"
    diff = f"""--- a/medigator/common/domain_model/xiot/device.py
+++ b/medigator/common/domain_model/xiot/device.py
@@ class Vendor(SerializableStrEnum): @@
     # Vendors from manuf file #
+    {enum_name} = "{display_value}", VendorSource.Manuf
     ...

--- a/medigator/common/domain_model/intels/vulnerabilities/types.py
+++ b/medigator/common/domain_model/intels/vulnerabilities/types.py
@@ class VulnerabilityRelevanceSource(SerializableStrEnum): @@
     # manufacturer sources.
     ...
+    {enum_name} = "{display_value}"
     ...

@@ VulnerabilityRelevanceSource.manufacturer_sources = {{ @@
     ...
+    VulnerabilityRelevanceSource.{enum_name},
 }}"""
    return {"device_py": device_py, "types_enum": types_enum, "types_set": types_set, "diff": diff}


def generate_code_snippets(enum_name: str, display_value: str) -> dict[str, str]:
    device_py = f'    {enum_name} = "{display_value}"'
    types_enum = f'    {enum_name} = "{display_value}"'
    types_set = f"    VulnerabilityRelevanceSource.{enum_name},"
    diff = f"""--- a/medigator/common/domain_model/xiot/device.py
+++ b/medigator/common/domain_model/xiot/device.py
@@ class Vendor(SerializableStrEnum): @@
     ...
+    {enum_name} = "{display_value}"
     # Vendors from manuf file #
     ...

--- a/medigator/common/domain_model/intels/vulnerabilities/types.py
+++ b/medigator/common/domain_model/intels/vulnerabilities/types.py
@@ class VulnerabilityRelevanceSource(SerializableStrEnum): @@
     # manufacturer sources.
     ...
+    {enum_name} = "{display_value}"
     ...

@@ VulnerabilityRelevanceSource.manufacturer_sources = {{ @@
     ...
+    VulnerabilityRelevanceSource.{enum_name},
 }}"""
    return {"device_py": device_py, "types_enum": types_enum, "types_set": types_set, "diff": diff}
