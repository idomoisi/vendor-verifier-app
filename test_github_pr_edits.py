"""Regression tests for Vendor Verifier device.py edits."""

from __future__ import annotations

import re

from github_pr import _edit_device_py, _edit_device_py_promote_manuf


FIXTURE = """\
    Dynamox = "Dynamox"
    SpectoTecnologia = "Specto Tecnologia"
    Lincata = "Lincata"

    # Vendors from manuf file #

    BpLubricantsUsa = "BP Lubricants USA", VendorSource.Manuf
    Khomp = "Khomp", VendorSource.Manuf
    Technexion = "TechNexion", VendorSource.Manuf
"""


def _first_class_tail(content: str) -> str:
    """Return text from last few first-class lines through the manuf comment."""
    m = re.search(
        r"(?s)(    \w+ = \"[^\"]+\"\n(?:    \w+ = \"[^\"]+\"\n)*\n    # Vendors from manuf file #)",
        content,
    )
    assert m, content
    return m.group(1)


def test_promote_khomp_no_extra_blank_line() -> None:
    """PR #50843 regression: promote must sit flush under the previous vendor."""
    out = _edit_device_py_promote_manuf(FIXTURE, "Khomp", "Khomp", "Khomp")
    assert (
        '    Lincata = "Lincata"\n'
        '    Khomp = "Khomp"\n'
        "\n"
        "    # Vendors from manuf file #\n"
    ) in out
    assert 'Khomp = "Khomp", VendorSource.Manuf' not in out
    assert "\n\n    Khomp = \"Khomp\"\n" not in out


def test_new_and_promote_share_first_class_spacing() -> None:
    base = """\
    Lincata = "Lincata"

    # Vendors from manuf file #

    BpLubricantsUsa = "BP Lubricants USA", VendorSource.Manuf
    Other = "Other", VendorSource.Manuf
"""
    new_out = _edit_device_py(base, "Acme", "Acme")
    promote_fixture = base.replace(
        '    Other = "Other", VendorSource.Manuf\n',
        '    Acme = "Acme", VendorSource.Manuf\n'
        '    Other = "Other", VendorSource.Manuf\n',
    )
    promote_out = _edit_device_py_promote_manuf(promote_fixture, "Acme", "Acme", "Acme")
    assert _first_class_tail(new_out) == _first_class_tail(promote_out)
    assert _first_class_tail(new_out) == (
        '    Lincata = "Lincata"\n'
        '    Acme = "Acme"\n'
        "\n"
        "    # Vendors from manuf file #"
    )
