"""Vendor Verifier -- Databricks Streamlit App.

Full pipeline: duplicate check -> Gemini Pro + Google Search verification ->
name normalization -> write to Delta tables -> code preview.
"""

from __future__ import annotations

import os
import re

import pandas as pd
import streamlit as st

from db import (
    check_vendor_has_alias_entry,
    find_manuf_matches,
    get_manuf_enum_name,
    get_recent_verifications,
    get_verified_vendor,
    increment_failure,
    is_manuf_vendor,
    load_vendor_enum_keys,
    load_vendor_registry,
    set_jira_ticket,
    update_raw_vendor_status,
    upsert_raw_vendor,
    upsert_vendor_verify,
)
from duplicate_check import (
    SimilarVendor,
    detect_existing_vendor_for_normalized_name,
    find_similar,
    is_duplicate,
)
from github_pr import create_vendor_pr, find_open_vendor_pr, test_github_connection
from jira import create_vendor_ticket, link_pr_to_jira
from normalization import (
    check_enum_collision,
    clean_official_name,
    generate_alias_snippet,
    generate_code_snippets,
    generate_enum_name,
    generate_promote_snippet,
)
from verification import verify_vendor

st.set_page_config(page_title="Vendor Verifier", page_icon="🏭", layout="wide")


def get_gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    try:
        import base64
        import requests
        from databricks.sdk.core import Config

        cfg = Config()
        host = cfg.host.rstrip("/")
        headers = cfg.authenticate()
        scopes = [
            ("vendor-validation-app", "gemini_api_key"),
            ("gemini_api_key", "api_key"),
        ]
        for scope, secret_key in scopes:
            try:
                resp = requests.get(
                    f"{host}/api/2.0/secrets/get",
                    headers=headers,
                    params={"scope": scope, "key": secret_key},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return base64.b64decode(resp.json()["value"]).decode()
            except Exception:
                continue
    except Exception:
        pass
    return ""


def get_current_user() -> str:
    """Return the viewing user's email from Databricks Apps forwarded headers.

    Do not use Config().authenticate() here — that returns the app service
  principal (deployer), so every visitor would inherit developer access.
    """
    try:
        headers = st.context.headers
        for key in (
            "X-Forwarded-Email",
            "x-forwarded-email",
            "X-Forwarded-Preferred-Username",
            "x-forwarded-preferred-username",
        ):
            value = headers.get(key)
            if value and "@" in value:
                return value.strip().lower()
    except Exception:
        pass
    return "unknown"


_DEVELOPER_EMAILS = {
    "ido.m@claroty.com",
    "17305340-34b9-4473-b189-ff3b86b73ca8",  # Ido Moisi (Databricks user ID)
}


def is_developer(email: str) -> bool:
    return email.strip().lower() in _DEVELOPER_EMAILS


def is_support_mode() -> bool:
    """True when a developer is previewing the simplified Support UI."""
    return bool(st.session_state.get("support_mode", False))


def effective_dev_mode(email: str) -> bool:
    return is_developer(email) and not is_support_mode()


def enum_key_for_display(display: str, enum_keys: list[tuple[str, str]]) -> str | None:
    target = display.strip().lower()
    for key, value in enum_keys:
        if value.strip().lower() == target:
            return key
    return None


def detect_normalized_parent_alias(
    vendor_name: str,
    official: str,
    registry: list[str],
) -> tuple[bool, str | None]:
    """True when AI normalized to a name that already exists — raw input should be an alias."""
    is_alias, match_name, _ = detect_existing_vendor_for_normalized_name(vendor_name, official, registry)
    return is_alias, match_name


@st.cache_resource(ttl=300)
def cached_registry() -> list[str]:
    """Vendor display names from device.py only (not pending app verifications)."""
    return load_vendor_registry()


@st.cache_resource(ttl=300)
def cached_enum_keys() -> list[tuple[str, str]]:
    return load_vendor_enum_keys()


@st.cache_resource(ttl=300)
def cached_manuf_pairs() -> list[tuple[str, str]]:
    """Load all VendorSource.Manuf (enum_key, display_value) pairs from device.py, cached 5 min."""
    from github_pr import DEVICE_PY_PATH, _get_file, _get_github_token
    import re
    token = _get_github_token()
    if not token:
        return []
    try:
        content, _ = _get_file(token, DEVICE_PY_PATH)
        return re.findall(r'^\s+(\w+)\s*=\s*"([^"]+)",\s*VendorSource\.Manuf', content, re.MULTILINE)
    except Exception:
        return []


def render_similar_vendors(similar: list[SimilarVendor]) -> None:
    for s in similar:
        icon = "🟢" if s.score >= 0.90 else "🟡" if s.score >= 0.80 else "🔵"
        st.markdown(f"{icon} **{s.name}** -- {s.score:.0%} ({s.match_type})")


def render_report(result: dict, search_grounded: bool) -> None:
    oui = result.get("mac_oui_check", "Unknown")
    oui_icon = "✅" if oui.lower().startswith("yes") else "❌" if oui.lower().startswith("no") else "❓"

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown(f"**Hardware Evidence**\n\n{result.get('hardware_evidence', 'None')}")
        st.markdown(f"**Networking Proof**\n\n{result.get('networking_proof', 'None')}")
    with col_b:
        protocols = result.get("supported_protocols", [])
        st.markdown(f"**Protocols:** {', '.join(protocols) if protocols else 'None found'}")
        st.markdown(f"**MAC/OUI Check:** {oui_icon} {oui}")
        devices = result.get("device_types", [])
        st.markdown(f"**Device Types:** {', '.join(devices) if devices else 'None found'}")
        industries = result.get("industries", [])
        st.markdown(f"**Industries:** {', '.join(industries) if industries else 'None found'}")

    if result.get("analyst_note"):
        st.info(f"**Analyst Note:** {result['analyst_note']}")

    artifacts = result.get("technical_artifacts", [])
    if artifacts:
        with st.expander(f"Technical Artifacts ({len(artifacts)})"):
            for a in artifacts:
                st.markdown(f"- [{a}]({a})")


def _build_sv_from_prev(
    prev: dict,
    *,
    official: str,
    enum: str,
    registry: list[str],
    similar: list,
) -> dict:
    """Build session-state payload from an existing vendor_verify row."""
    return {
        "dup": False,
        "result": prev,
        "raw": prev.get("raw_output", "{}"),
        "grounded": bool(prev.get("search_grounded")),
        "verdict": prev.get("verdict", "LEGIT"),
        "grounded_label": "🌐 Web-verified" if prev.get("search_grounded") else "⚠️ Not grounded",
        "similar": [(s.name, s.score, s.match_type) for s in similar],
        "official": official,
        "enum": enum,
        "registry": registry,
    }


def _render_result_panel(vendor_name: str, vendor_url: str) -> None:
    """Render the result panel, using session state so button clicks don't wipe results."""
    skip_ai = False

    _DEMO_RESULT = {
        "verdict": "LEGIT",
        "official_name": "Acme Industrial Systems",
        "website": "https://acme-industrial.example.com",
        "hardware_evidence": "Acme X200 PLC: 45mm × 90mm, 24VDC input, -20°C to 60°C operating range. Acme RT-500 RTU: DIN rail mount, 150g, 9-36VDC.",
        "networking_proof": "Default IP 192.168.0.1, subnet 255.255.255.0. Documented in Acme X200 Quick Start Guide p.12.",
        "supported_protocols": ["Modbus TCP", "EtherNet/IP", "PROFINET", "SNMP v2c"],
        "mac_oui_check": "Yes — OUI registered to Acme Industrial GmbH (IEEE registry)",
        "technical_artifacts": ["https://acme-industrial.example.com/docs/x200-datasheet.pdf"],
        "analyst_note": "Original equipment manufacturer. No white-label or reseller indicators.",
        "device_types": ["PLC", "RTU", "HMI", "Industrial Gateway"],
        "industries": ["Industrial", "Manufacturing", "Energy"],
        "confidence_score": "High",
    }

    _DEMO_FAIL_RESULT = {
        "verdict": "SUSPICIOUS",
        "official_name": "Acme Cloud Solutions",
        "website": "https://acme-cloud.example.com",
        "hardware_evidence": "No physical hardware datasheets found. All products appear to be cloud-hosted SaaS platforms.",
        "networking_proof": "No documented default network settings or IP configuration found.",
        "supported_protocols": [],
        "mac_oui_check": "No — no registered OUI found in the IEEE database.",
        "technical_artifacts": [],
        "analyst_note": "This appears to be a software-only company. No evidence of NIC manufacturing or physical device production.",
        "device_types": [],
        "industries": ["Enterprise", "SaaS"],
        "confidence_score": "High",
    }

    # ── Run pipeline when Verify was just clicked ──
    if st.session_state.pop("_run_pipeline", False):
        codebase_registry = cached_registry()
        api_key = get_gemini_api_key()
        user = get_current_user()

        # ── Demo mode: skip full pipeline ──
        _demo_key = vendor_name.strip().lower()
        if _demo_key in ("__demo__", "__demo_fail__"):
            if _demo_key == "__demo_fail__":
                demo_result = _DEMO_FAIL_RESULT
                demo_verdict = "SUSPICIOUS"
                demo_official = "Acme Cloud Solutions"
                demo_raw = '{"verdict": "SUSPICIOUS", "official_name": "Acme Cloud Solutions", "...": "..."}'
            else:
                demo_result = _DEMO_RESULT
                demo_verdict = "LEGIT"
                demo_official = "Acme Industrial Systems"
                demo_raw = '{"verdict": "LEGIT", "official_name": "Acme Industrial Systems", "...": "..."}'
            demo_enum = generate_enum_name(demo_official)
            st.session_state["sv"] = {
                "dup": False,
                "result": demo_result,
                "raw": demo_raw,
                "grounded": True,
                "verdict": demo_verdict,
                "grounded_label": "🌐 Web-verified",
                "similar": [],
                "official": demo_official,
                "enum": demo_enum,
                "registry": codebase_registry,
            }
            st.rerun()

        # Step 1: Duplicate Check (codebase only)
        with st.status("Checking for duplicates...", expanded=True) as dup_status:
            similar = find_similar(vendor_name, codebase_registry)
            dup = is_duplicate(similar)
            prev = get_verified_vendor(vendor_name)

            if dup:
                match = similar[0]
                label = "Exact match" if match.match_type == "exact" else f"High similarity ({match.score:.0%})"
                dup_status.update(label="Duplicate found", state="error")
                st.error(
                    f"**{label}:** `{match.name}`\n\n"
                    f"This vendor already exists in the codebase. No verification needed."
                )
                if len(similar) > 1:
                    st.caption("Other similar vendors:")
                    render_similar_vendors(similar[1:])
                upsert_raw_vendor(vendor_name, vendor_url or None, user, is_existed=True, status="DUPLICATE")
                st.session_state["sv"] = {
                    "dup": True,
                    "dup_message": f"**{label}:** `{match.name}` is already in the system.",
                }
                skip_ai = True

            elif prev and prev.get("verdict") == "LEGIT":
                # Same input verified earlier in this app — resume without re-running AI.
                official = clean_official_name(prev.get("normalized_name") or vendor_name)
                enum = generate_enum_name(official)
                if check_enum_collision(enum, codebase_registry):
                    enum = f"{enum}Corp"
                dup_status.update(
                    label="Previously verified — resuming to ticket creation",
                    state="complete",
                    expanded=False,
                )
                st.info(
                    f"**`{vendor_name}`** was already verified in this app — "
                    f"skipping AI and opening ticket creation."
                )
                if similar:
                    st.caption("Similar vendors in codebase:")
                    render_similar_vendors(similar)
                upsert_raw_vendor(
                    vendor_name, vendor_url or None, user, is_existed=bool(similar), status="COMPLETED"
                )
                st.session_state["sv"] = _build_sv_from_prev(
                    prev, official=official, enum=enum, registry=codebase_registry, similar=similar
                )
                skip_ai = True

            if not skip_ai:
                if similar:
                    dup_status.update(label="Similar found (< 90%)", state="running")
                    st.warning("**Similar vendors found** (below 90% -- proceeding):")
                    render_similar_vendors(similar)
                else:
                    dup_status.update(label="No duplicates", state="complete", expanded=False)

        if not skip_ai:
            # Step 2: AI Verification
            with st.status("Verifying with Gemini Pro + Google Search...", expanded=True) as ai_status:
                if not api_key:
                    ai_status.update(label="No API key", state="error")
                    st.error("Gemini API key not configured.")
                    return

                upsert_raw_vendor(vendor_name, vendor_url or None, user, is_existed=bool(similar), status="PROCESSING")
                result, raw, grounded = verify_vendor(vendor_name, vendor_url or None, api_key)

                if result is None:
                    ai_status.update(label="Verification failed", state="error")
                    st.error("All verification attempts failed. Try again later.")
                    increment_failure(vendor_name)
                    return

                verdict = result.get("verdict", "SUSPICIOUS")
                grounded_label = "🌐 Web-verified" if grounded else "⚠️ Not grounded"

                if verdict == "LEGIT":
                    ai_status.update(label=f"LEGIT ({grounded_label})", state="complete")
                    st.success(f"**Verdict: LEGIT** -- Confirmed hardware manufacturer ({grounded_label})")
                elif verdict == "SOFTWARE-ONLY":
                    ai_status.update(label=f"SOFTWARE-ONLY ({grounded_label})", state="error")
                    st.error(f"**Verdict: SOFTWARE-ONLY** ({grounded_label})")
                else:
                    ai_status.update(label=f"SUSPICIOUS ({grounded_label})", state="running")
                    st.warning(f"**Verdict: SUSPICIOUS** -- Manual review needed ({grounded_label})")

            official = clean_official_name(result.get("official_name", vendor_name))

            # If official name has no usable Latin characters (e.g. Chinese/Arabic script),
            # fall back to the user's original input and flag it for manual correction.
            _latin_chars = re.sub(r"[^a-zA-Z0-9]", "", official)
            _non_latin_name = len(_latin_chars) < 3
            if _non_latin_name:
                official = vendor_name  # fall back to what the user typed

            enum = generate_enum_name(official)
            if not enum:
                enum = "UnknownVendor"
            if check_enum_collision(enum, codebase_registry):
                enum = f"{enum}Corp"

            # Step 3: Gate 2 — re-check duplicate on AI-normalized name (codebase only)
            gate2_duplicate = False
            gate2_match_name: str | None = None
            gate2_match_type = ""
            similar_norm: list[SimilarVendor] = []
            if official.lower() != vendor_name.lower():
                with st.status(f"Re-checking normalized name: {official}...", expanded=True) as norm_status:
                    gate2_duplicate, gate2_match_name, gate2_match_type = (
                        detect_existing_vendor_for_normalized_name(vendor_name, official, codebase_registry)
                    )

                    if gate2_duplicate and gate2_match_name:
                        label = {
                            "exact": "Exact match",
                            "parent-brand": "Parent brand",
                            "parent-prefix": "Parent brand",
                        }.get(gate2_match_type, "High similarity")
                        norm_status.update(label=f"Existing vendor identified ({label})", state="complete")
                        st.info(
                            f"**AI normalized** `{vendor_name}` → **`{official}`**, which maps to "
                            f"existing vendor **`{gate2_match_name}`** ({label}).\n\n"
                            f"**`{vendor_name}`** will be added as an alias for **`{gate2_match_name}`**."
                        )
                    else:
                        similar_norm = find_similar(official, codebase_registry)
                        if similar_norm:
                            norm_status.update(
                                label="Similar normalized name (< 90%) -- proceeding",
                                state="running",
                            )
                            st.warning(f"**Normalized name `{official}` has similar vendors** (below 90%):")
                            render_similar_vendors(similar_norm)
                        else:
                            norm_status.update(label="Normalized name is unique", state="complete", expanded=False)

            upsert_vendor_verify(vendor_name, result, grounded, raw)
            update_raw_vendor_status(vendor_name, "COMPLETED")

            st.session_state["sv"] = {
                "dup": False,
                "result": result,
                "raw": raw,
                "grounded": grounded,
                "verdict": verdict,
                "non_latin_name": _non_latin_name,
                "grounded_label": grounded_label,
                "similar": [(s.name, s.score, s.match_type) for s in similar],
                "similar_norm": [(s.name, s.score, s.match_type) for s in similar_norm],
                "gate2_duplicate": gate2_duplicate,
                "gate2_match_name": gate2_match_name,
                "official": official,
                "enum": enum,
                "registry": codebase_registry,
            }

    sv = st.session_state.get("sv", {})

    # ── Nothing stored yet ──
    if not sv:
        st.info("Enter a vendor name and click **Verify** to start the pipeline.")
        return

    if sv.get("dup"):
        st.error(sv.get("dup_message", "This vendor already exists in the system. No verification needed."))
        return

    # ── Re-render results from session state ──
    result = sv["result"]
    raw = sv["raw"]
    grounded = sv["grounded"]
    verdict = sv["verdict"]
    grounded_label = sv["grounded_label"]
    official = sv["official"]
    enum = sv["enum"]
    dev_mode = effective_dev_mode(get_current_user())

    verdict_icon = {"LEGIT": "✅", "SOFTWARE-ONLY": "❌", "SUSPICIOUS": "⚠️"}.get(verdict, "❓")
    confidence = result.get("confidence_score", "")
    confidence_str = f" &nbsp; 🎯 `{confidence}`" if confidence else ""
    st.markdown(f"**Verdict:** {verdict_icon} `{verdict}` &nbsp; {grounded_label}{confidence_str}")

    if sv.get("non_latin_name"):
        st.warning(
            "⚠️ **The AI returned a non-Latin vendor name** (e.g. Chinese/Arabic characters). "
            "The display value has been reset to your original input. "
            "Please manually verify and correct the **Display Value** and **Enum Name** below before submitting."
        )

    render_report(result, grounded)

    if dev_mode:
        with st.expander("Raw AI response"):
            st.code(raw, language="json")

    if verdict != "LEGIT":
        st.divider()
        st.info(
            f"This vendor was not verified as a hardware manufacturer. "
            f"If you believe this requires further investigation, you can track it on the DF board:\n\n"
            f"[Open DF Board ↗](https://team82.atlassian.net/jira/software/c/projects/DF/boards/317)"
        )

    if verdict == "LEGIT":
        st.divider()

        # ── Auto-detect PR case (always) ──
        registry = sv.get("registry") or cached_registry()
        gate2_duplicate, gate2_match_name = detect_normalized_parent_alias(
            vendor_name, official, registry
        )
        if not gate2_duplicate and sv.get("gate2_duplicate"):
            gate2_duplicate = True
            gate2_match_name = sv.get("gate2_match_name")

        final_enum = enum
        final_display = official
        alias_string = vendor_name
        alias_target_enum: str | None = None
        manuf_enum: str | None = None
        enum_keys = cached_enum_keys()

        manuf_matches: list[tuple[str, str, float]] = []
        manuf_original_display: str | None = None

        if gate2_duplicate and gate2_match_name:
            pr_case = "alias"
            alias_target_enum = enum_key_for_display(gate2_match_name, enum_keys)
            if not alias_target_enum:
                gate2_duplicate = False
                gate2_match_name = None
                pr_case = "new"
        elif not gate2_duplicate:
            manuf_matches = find_manuf_matches(final_display, preloaded_pairs=cached_manuf_pairs())
            if manuf_matches:
                # Show fuzzy manuf matches and let user decide
                options_display = [f"{disp} ({score:.0%} match)" for _, disp, score in manuf_matches]
                options_display.append("None of these — add as new vendor")
                selected_manuf = st.radio(
                    "⬆️ Similar entries found in the manuf-file section. Is this a promotion?",
                    options=options_display,
                    index=len(options_display) - 1,  # default to "None"
                    key="manuf_match_select",
                )
                if selected_manuf == options_display[-1]:
                    pr_case = "new"
                    manuf_enum = None
                else:
                    pr_case = "promote"
                    idx = options_display.index(selected_manuf)
                    manuf_enum = manuf_matches[idx][0]
                    manuf_original_display = manuf_matches[idx][1]
            elif sv.get("similar") and any(s[1] >= 0.85 for s in sv.get("similar", [])):
                pr_case = "alias"
                best = sv["similar"][0][0]
                alias_target_enum = enum_key_for_display(best, enum_keys)
                if not alias_target_enum:
                    pr_case = "new"
            else:
                pr_case = "new"
        else:
            pr_case = "alias"

        # Allow developer to override the auto-detected case
        if dev_mode:
            pr_case = st.selectbox(
                "Override detected case",
                options=["new", "alias", "promote"],
                index=["new", "alias", "promote"].index(pr_case),
                format_func=lambda x: {"new": "🆕 New vendor", "alias": "🔗 Alias", "promote": "⬆️ Promote from manuf"}[x],
                key="demo_case_override",
            )

        case_labels = {
            "new": ("🆕", "New vendor", f"**{official}** will be added as a new vendor to the system."),
            "alias": (
                "🔗",
                "Alias detected",
                (
                    f"**`{alias_string}`** will be added as an alias for **`{gate2_match_name}`**."
                    if gate2_duplicate and gate2_match_name
                    else f"**{alias_string}** is another name for an existing vendor."
                ),
            ),
            "promote": ("⬆️", "Existing manuf-file vendor", f"**{official}** exists as a low-priority entry and will be promoted to a first-class vendor."),
        }
        icon, case_title, case_desc = case_labels[pr_case]

        if dev_mode:
            # ── Developer mode: full controls ──
            st.subheader("Name Normalization")
            if official.lower() != vendor_name.lower():
                st.info(f"**AI corrected:** `{vendor_name}` → **{official}**")
            if pr_case == "alias":
                st.caption(f"Alias string to add: **`{alias_string}`**")

            col_e, col_d = st.columns(2)
            with col_e:
                final_enum = st.text_input(
                    "Enum Name",
                    value=enum,
                    key="input_enum",
                    disabled=pr_case == "alias",
                )
            with col_d:
                final_display = st.text_input(
                    "Display Value",
                    value=official,
                    key="input_display",
                    disabled=pr_case == "alias",
                )

            st.divider()
            st.subheader("Code Changes")
            st.info(f"**{icon} Detected: {case_title}** — {case_desc}")

            if pr_case == "alias":
                options = [f"{k} ({v})" for k, v in enum_keys]
                default_idx = 0
                target_name = gate2_match_name or ""
                if not target_name and sv.get("similar"):
                    target_name = sv["similar"][0][0]
                for i, opt in enumerate(options):
                    if target_name and target_name.lower() in opt.lower():
                        default_idx = i
                        break
                selected = st.selectbox("Alias target vendor:", options=options, index=default_idx, key="alias_target")
                alias_target_enum = selected.split(" (")[0] if selected else None
                has_existing = check_vendor_has_alias_entry(alias_target_enum) if alias_target_enum else False
                snippets = generate_alias_snippet(alias_target_enum or "", alias_string, has_existing_entry=has_existing)
            elif pr_case == "promote":
                snippets = generate_promote_snippet(
                    manuf_enum or final_enum,
                    manuf_original_display or final_display,
                    final_enum,
                    final_display,
                )
            else:
                snippets = generate_code_snippets(final_enum, final_display)

            tab_diff, tab_dev, tab_types = st.tabs(["Unified Diff", "device.py", "types.py"])
            with tab_diff:
                st.code(snippets["diff"], language="diff")
            with tab_dev:
                st.caption("`medigator/common/domain_model/xiot/device.py`")
                st.code(snippets["device_py"], language="python")
            with tab_types:
                if pr_case == "alias":
                    st.caption("No changes to `types.py` for aliases.")
                else:
                    st.caption("`medigator/common/domain_model/intels/vulnerabilities/types.py`")
                    st.code(snippets["types_enum"], language="python")
                    st.divider()
                    st.code(snippets["types_set"], language="python")

        else:
            # ── Support mode: clear summary only ──
            if pr_case == "alias":
                target_name = gate2_match_name or (sv["similar"][0][0] if sv.get("similar") else official)
                if not alias_target_enum:
                    options_raw = [f"{k} ({v})" for k, v in enum_keys]
                    default_idx = 0
                    for i, opt in enumerate(options_raw):
                        if target_name and target_name.lower() in opt.lower():
                            default_idx = i
                            break
                    alias_target_enum = options_raw[default_idx].split(" (")[0] if options_raw else None
                enum_label = f"`Vendor.{alias_target_enum}`" if alias_target_enum else "an existing vendor"
                match_display = target_name or official
                st.success(
                    f"✅ **Already in the system** — **{vendor_name}** matches existing vendor "
                    f"**{match_display}** ({enum_label}).\n\n"
                    f"No action needed."
                )
            else:
                st.info(f"{icon} **{case_title}:** {case_desc}")

        # Support cannot create alias PRs — hard stop before submit/Jira/PR.
        if not dev_mode and pr_case == "alias":
            return

        # ── Jira Ticket + GitHub PR ──
        st.divider()

        def _do_submit(submit_enum: str, submit_display: str, submit_case: str,
                       submit_alias: str | None, submit_manuf: str | None,
                       submit_manuf_original_display: str | None = None) -> None:
            submit_display_for_pr = alias_string if submit_case == "alias" else submit_display
            with st.spinner("Creating Jira ticket and GitHub PR..."):
                try:
                    ticket_key, _ = create_vendor_ticket(
                        vendor_name,
                        submit_enum,
                        submit_display_for_pr,
                        result,
                        case=submit_case,
                        alias_target_display=submit_display if submit_case == "alias" else None,
                    )
                    set_jira_ticket(vendor_name, ticket_key)
                    st.session_state[f"jira_{vendor_name}"] = ticket_key
                except RuntimeError as e:
                    st.error(f"Jira failed: {e}")
                    return
                try:
                    pr_link, _ = create_vendor_pr(
                        submit_enum, submit_display_for_pr, ticket_key, result,
                        case=submit_case,
                        alias_target_enum=submit_alias,
                        manuf_enum_name=submit_manuf,
                        manuf_original_display=submit_manuf_original_display,
                        input_vendor=vendor_name,
                    )
                    st.session_state[f"pr_{vendor_name}"] = pr_link
                    link_pr_to_jira(ticket_key, pr_link)
                except RuntimeError as e:
                    st.session_state[f"pr_error_{vendor_name}"] = str(e)
                st.session_state.pop(f"confirm_{vendor_name}", None)
                st.rerun()

        jira_key = st.session_state.get(f"jira_{vendor_name}")
        pr_url = st.session_state.get(f"pr_{vendor_name}")

        open_pr = None
        if not pr_url:
            pr_lookup_display = alias_string if pr_case == "alias" else final_display
            open_pr_cache_key = f"open_pr_{vendor_name}_{final_enum}_{pr_case}"
            if open_pr_cache_key not in st.session_state:
                st.session_state[open_pr_cache_key] = find_open_vendor_pr(
                    enum_name=final_enum,
                    display_name=pr_lookup_display,
                    input_vendor=vendor_name,
                    case=pr_case,
                    alias_target_enum=alias_target_enum,
                )
            open_pr = st.session_state[open_pr_cache_key]

        if open_pr and not pr_url:
            st.warning(
                f"An open PR already exists for this vendor — nothing new will be created.\n\n"
                f"**[#{open_pr.number} {open_pr.title}]({open_pr.url})**"
            )
        elif jira_key:
            jira_url = f"https://team82.atlassian.net/browse/{jira_key}"
            st.success(f"Ticket created: **[{jira_key}]({jira_url})**")
            if pr_url:
                st.success(f"PR opened: **[View on GitHub ↗]({pr_url})**")
            else:
                pr_error = st.session_state.get(f"pr_error_{vendor_name}", "Unknown error")
                st.warning(f"Jira ticket created but GitHub PR failed: `{pr_error}`")

        elif dev_mode:
            # Developer: detailed confirm dialog
            st.subheader("Create Jira Ticket & GitHub PR")
            st.caption("Opens a NET task and a GitHub PR on `staging` with the exact code changes.")
            if not st.session_state.get(f"confirm_{vendor_name}"):
                case_label = {"new": "new vendor", "alias": "alias", "promote": "vendor promotion"}.get(pr_case, pr_case)
                ticket_subject = (
                    f"`{alias_string}` → `{gate2_match_name}`"
                    if pr_case == "alias" and gate2_match_name
                    else final_display
                )
                st.info(
                    f"This will immediately:\n"
                    f"- Open a **NET Jira ticket** titled `[Vendor Verifier] Add {case_label}: {ticket_subject}`\n"
                    f"- Create a **GitHub PR** on a branch off `staging` with the exact code changes shown above\n\n"
                    f"Make sure everything above looks correct before proceeding."
                )
                if st.button("🎫 Create NET Ticket & PR", type="primary"):
                    st.session_state[f"confirm_{vendor_name}"] = {
                        "case": pr_case, "alias_target": alias_target_enum,
                        "manuf_enum": manuf_enum, "manuf_original_display": manuf_original_display,
                        "enum": final_enum, "display": final_display,
                    }
                    st.rerun()
            else:
                col_confirm, col_cancel = st.columns(2)
                with col_confirm:
                    confirmed = st.button("✅ Yes, create ticket & PR", type="primary", use_container_width=True)
                with col_cancel:
                    if st.button("✖ Cancel", use_container_width=True):
                        st.session_state.pop(f"confirm_{vendor_name}", None)
                        st.rerun()
                if confirmed:
                    cd = st.session_state.get(f"confirm_{vendor_name}", {})
                    _do_submit(cd.get("enum", final_enum), cd.get("display", final_display),
                               cd.get("case", "new"), cd.get("alias_target"),
                               cd.get("manuf_enum"), cd.get("manuf_original_display"))

        else:
            # Support: simple submit
            st.subheader("Submit")
            if not st.session_state.get(f"confirm_{vendor_name}"):
                if st.button("✅ Yes, add this vendor", type="primary", use_container_width=True):
                    st.session_state[f"confirm_{vendor_name}"] = True
                    st.rerun()
            else:
                col_yes, col_no = st.columns(2)
                with col_yes:
                    confirmed = st.button("✅ Confirm", type="primary", use_container_width=True)
                with col_no:
                    if st.button("✖ Cancel", use_container_width=True):
                        st.session_state.pop(f"confirm_{vendor_name}", None)
                        st.rerun()
                if confirmed:
                    _do_submit(final_enum, final_display, pr_case, alias_target_enum,
                               manuf_enum, manuf_original_display)


def main() -> None:
    st.title("🏭 Vendor Verifier")
    st.caption("Verify vendors with AI + Google Search, check duplicates, and preview code changes.")

    with st.sidebar:
        _current_user = get_current_user()
        _is_dev = is_developer(_current_user)
        if _is_dev:
            st.toggle(
                "Support mode",
                value=is_support_mode(),
                key="support_mode",
                help="Preview the simplified UI that Support users see.",
            )
        if _is_dev and is_support_mode():
            _role = "Developer · Support preview"
        elif _is_dev:
            _role = "Developer"
        else:
            _role = "Support"
        st.caption(f"Logged in as: `{_current_user}` ({_role})")
        st.subheader("Integrations")
        if st.button("Test GitHub Connection", use_container_width=True):
            ok, msg = test_github_connection()
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    tab_verify, tab_history = st.tabs(["Verify Vendor", "History"])

    with tab_verify:
        col_form, col_result = st.columns([1, 2])

        with col_form:
            st.subheader("New Vendor")
            vendor_name = st.text_input("Vendor Name", placeholder="e.g., Newhaven Display")
            vendor_url = st.text_input("Website (optional)", placeholder="e.g., https://newhavendisplay.com")
            run = st.button("Verify", type="primary", use_container_width=True)

        if run and vendor_name:
            st.session_state["_run_pipeline"] = True

        # Clear stored results only when the vendor name actually changed (not mid-verify).
        if st.session_state.get("sv_vendor") != vendor_name:
            if not st.session_state.get("_run_pipeline"):
                st.session_state.pop("sv", None)
            st.session_state["sv_vendor"] = vendor_name

        with col_result:
            if vendor_name:
                _render_result_panel(vendor_name, vendor_url)
            else:
                st.info("Enter a vendor name and click **Verify** to start the pipeline.")

    with tab_history:
        st.subheader("Recent Verifications")
        try:
            rows = get_recent_verifications(30)
            if rows:
                df = pd.DataFrame(rows)
                verdict_colors = {"LEGIT": "✅", "SOFTWARE-ONLY": "❌", "SUSPICIOUS": "⚠️"}
                df["verdict"] = df["verdict"].apply(lambda v: f"{verdict_colors.get(v, '')} {v}")
                base = "https://team82.atlassian.net/browse/"
                df["jira_ticket"] = df["jira_ticket"].apply(
                    lambda k: f"[{k}]({base}{k})" if k else ""
                )
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.caption("No verifications yet.")
        except Exception as e:
            st.error(f"Could not load history: {e}")


if __name__ == "__main__":
    main()
