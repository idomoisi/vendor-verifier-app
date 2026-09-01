"""Batch GitHub PR creation for the Coralogix vendor pipeline.

Mirrors vendor-verifier-app/github_pr.py but applies many vendors/aliases in one branch.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import requests

from vendor_verifier_jira import link_pr_to_jira

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
REPO = "medigateio/medigator"
BASE_BRANCH = "staging"
DEVICE_PY_PATH = "medigator/common/domain_model/xiot/device.py"
TYPES_PY_PATH = "medigator/common/domain_model/intels/vulnerabilities/types.py"
OUI_INFO_PY_PATH = "medigator/common/domain_model/oui_info.py"
IEEE_MANUF_PATH = "medigator/resources/manuf"
SECRET_SCOPE = "vendor-validation-app"


@dataclass(frozen=True)
class BatchPrRow:
    vendor_name_raw: str
    jira_ticket: str
    status: str
    verdict: str | None
    confidence_score: str | None
    official_name: str | None
    enum_name: str | None
    duplicate_of: str | None
    should_add_alias: bool
    website: str | None
    hardware_evidence: str | None
    supported_protocols: str | None
    pr_case: str = "new"
    manuf_enum: str | None = None
    manuf_original_display: str | None = None
    manuf_source: str | None = None
    manuf_ieee_short: str | None = None


def _get_secret(get_secret: Callable[[str, str], str], key: str) -> str:
    return get_secret(SECRET_SCOPE, key)


def _resolve_github_auth(get_secret: Callable[[str, str], str]) -> tuple[str, str]:
    """Return (token, mode)."""
    try:
        import jwt as pyjwt

        app_id = _get_secret(get_secret, "github_app_id").strip()
        private_key = _get_secret(get_secret, "github_app_private_key")
        installation_id = _get_secret(get_secret, "github_app_installation_id").strip()
        if app_id and private_key and installation_id:
            now = int(time.time())
            app_jwt = pyjwt.encode(
                {"iat": now - 60, "exp": now + 600, "iss": str(app_id)},
                private_key,
                algorithm="RS256",
            )
            resp = requests.post(
                f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15,
            )
            if resp.status_code == 201:
                token = resp.json().get("token", "")
                if token:
                    return token, "github-app"
    except Exception:
        logger.exception("GitHub App auth failed")

    pat = _get_secret(get_secret, "github_token")
    if pat:
        return pat, "secret-pat"
    return "", "none"


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_staging_sha(token: str) -> str:
    resp = requests.get(
        f"{GITHUB_API}/repos/{REPO}/git/ref/heads/{BASE_BRANCH}",
        headers=_gh_headers(token),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


def _create_branch(token: str, branch_name: str, sha: str) -> None:
    resp = requests.post(
        f"{GITHUB_API}/repos/{REPO}/git/refs",
        headers=_gh_headers(token),
        json={"ref": f"refs/heads/{branch_name}", "sha": sha},
        timeout=15,
    )
    if resp.status_code not in (201, 422):
        resp.raise_for_status()


def _get_file(token: str, path: str) -> tuple[str, str]:
    resp = requests.get(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers=_gh_headers(token),
        params={"ref": BASE_BRANCH},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("encoding") != "base64":
        raise ValueError(
            f"{path} came back with encoding {data.get('encoding')!r} "
            f"({data.get('size')} bytes); use _get_file_raw for files over 1MB"
        )
    content = base64.b64decode(data["content"]).decode()
    return content, data["sha"]


def _get_file_raw(token: str, path: str) -> str:
    """Read a file too large for the base64 Contents response.

    The Contents API only base64-encodes blobs up to 1MB and returns an empty
    body with encoding "none" above that. `resources/manuf` is ~2.5MB.
    """
    headers = dict(_gh_headers(token))
    headers["Accept"] = "application/vnd.github.raw"
    resp = requests.get(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers=headers,
        params={"ref": BASE_BRANCH},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


def _push_file(
    token: str, path: str, content: str, sha: str, branch: str, message: str
) -> None:
    resp = requests.put(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers=_gh_headers(token),
        json={
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "sha": sha,
            "branch": branch,
        },
        timeout=30,
    )
    resp.raise_for_status()


def _open_pr(token: str, title: str, body: str, head: str) -> str:
    resp = requests.post(
        f"{GITHUB_API}/repos/{REPO}/pulls",
        headers=_gh_headers(token),
        json={"title": title, "body": body, "head": head, "base": BASE_BRANCH},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["html_url"]


def _edit_device_py(content: str, enum_name: str, display_name: str) -> str:
    anchor = "\n\n    # Vendors from manuf file #"
    new_line = f'    {enum_name} = "{display_name}"'
    if f'{enum_name} = "{display_name}"' in content:
        raise ValueError(f"{enum_name} already exists in device.py")
    if anchor not in content:
        raise ValueError("Could not find insertion anchor in device.py")
    return content.replace(anchor, f"\n{new_line}{anchor}", 1)


def _edit_types_py(content: str, enum_name: str, display_name: str) -> str:
    enum_line = f'    {enum_name} = "{display_name}"\n'
    set_line = f"    VulnerabilityRelevanceSource.{enum_name},\n"
    if f"    {enum_name} = " in content:
        raise ValueError(f"{enum_name} already exists in types.py")
    enum_anchor = "\n\n    def get_description("
    if enum_anchor not in content:
        raise ValueError("Could not find get_description anchor in types.py")
    content = content.replace(enum_anchor, "\n" + enum_line + "\n    def get_description(", 1)
    set_anchor = "}\n\n\nVULNERABILITY_SOURCE_STR_TO_TYPE"
    if set_anchor not in content:
        raise ValueError("Could not find manufacturer_sources closing brace in types.py")
    return content.replace(set_anchor, set_line + set_anchor, 1)


def _edit_device_py_alias(content: str, existing_enum: str, alias_name: str) -> str:
    existing_entry = f"Vendor.{existing_enum}: ("
    new_alias_line = f'        "{alias_name}",\n'
    if existing_entry in content:
        entry_start = content.index(existing_entry)
        close_paren_idx = content.index("\n    ),", entry_start)
        if f'"{alias_name}"' in content[entry_start:close_paren_idx]:
            return content
        if f'"{alias_name}"' in content:
            raise ValueError(
                f'Alias "{alias_name}" already belongs to another Vendor entry'
            )
        return (
            content[:close_paren_idx]
            + "\n"
            + new_alias_line
            + "    ),"
            + content[close_paren_idx + len("\n    ),") :]
        )
    if f'"{alias_name}"' in content:
        raise ValueError(f'Alias "{alias_name}" already exists elsewhere in device.py')
    aliases_close = "}\n\nassert all(\n    isinstance(value, tuple)"
    if aliases_close not in content:
        raise ValueError("Could not find end of VENDOR_ALIASES in device.py")
    new_entry = f'    Vendor.{existing_enum}: (\n        "{alias_name}",\n    ),\n'
    return content.replace(aliases_close, new_entry + aliases_close)


def _edit_device_py_promote(
    content: str,
    manuf_enum: str,
    new_enum: str,
    new_display: str,
) -> str:
    match = re.search(
        rf'^\s+{re.escape(manuf_enum)}\s*=\s*"[^"]+",\s*VendorSource\.Manuf\n',
        content,
        re.MULTILINE,
    )
    if not match:
        raise ValueError(f"Could not find Manuf entry for {manuf_enum}")
    content = content.replace(match.group(0), "", 1)
    return _edit_device_py(content, new_enum, new_display)


def _edit_oui_info(
    content: str,
    old_enum: str,
    new_enum: str,
) -> tuple[str, int]:
    if old_enum == new_enum:
        return content, 0
    pattern = re.compile(rf"(?<!\w)Vendor\.{re.escape(old_enum)}(?!\w)")
    return pattern.subn(f"Vendor.{new_enum}", content)


def _edit_oui_info_ieee_promote(
    content: str,
    ieee_long_name: str,
    ieee_short_name: str,
    new_enum_name: str,
) -> tuple[str, int]:
    from vendor_verifier_ieee_manuf import parse_oui_vendor_mappings

    mappings = parse_oui_vendor_mappings(content)
    lines: list[str] = []
    for key in dict.fromkeys((ieee_long_name, ieee_short_name.upper())):
        existing = mappings.get(key)
        if existing and existing != new_enum_name:
            raise ValueError(f"IEEE OUI key {key!r} already maps to Vendor.{existing}")
        if existing is None:
            lines.append(f"    {json.dumps(key, ensure_ascii=False)}: Vendor.{new_enum_name},")
    if not lines:
        return content, 0
    start = content.find("OUI_TO_VENDOR")
    if start < 0:
        raise ValueError("Could not find OUI_TO_VENDOR")
    close = content.find("\n}", start)
    if close < 0:
        raise ValueError("Could not find end of OUI_TO_VENDOR")
    return content[:close] + "\n" + "\n".join(lines) + content[close:], len(lines)


def load_vendor_source(
    get_secret: Callable[[str, str], str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Load first-class names and the closed Manuf registry from medigator staging."""
    token, _ = _resolve_github_auth(get_secret)
    if not token:
        raise RuntimeError("GitHub auth failed while loading Vendor registry")
    device_content, _ = _get_file(token, DEVICE_PY_PATH)
    vendor_start = device_content.find("class Vendor(")
    manuf_anchor = device_content.find("# Vendors from manuf file #", vendor_start)
    if vendor_start < 0 or manuf_anchor < 0:
        raise ValueError("Could not locate Vendor/Manuf sections in device.py")
    first_class = [
        display
        for _enum, display in re.findall(
            r'^\s+(\w+)\s*=\s*"([^"]+)"\s*$',
            device_content[vendor_start:manuf_anchor],
            re.MULTILINE,
        )
    ]
    manuf_pairs = re.findall(
        r'^\s+(\w+)\s*=\s*"([^"]+)",\s*VendorSource\.Manuf\s*$',
        device_content,
        re.MULTILINE,
    )
    return first_class, manuf_pairs


def load_vendor_sources(
    get_secret: Callable[[str, str], str],
):
    """Load enum-backed and IEEE-backed Manuf identities plus OUI mappings."""
    from vendor_verifier_ieee_manuf import (
        parse_ieee_manuf_pairs,
        parse_oui_vendor_mappings,
    )

    token, _ = _resolve_github_auth(get_secret)
    if not token:
        raise RuntimeError("GitHub auth failed while loading Vendor registries")
    device_content, _ = _get_file(token, DEVICE_PY_PATH)
    vendor_start = device_content.find("class Vendor(")
    manuf_anchor = device_content.find("# Vendors from manuf file #", vendor_start)
    if vendor_start < 0 or manuf_anchor < 0:
        raise ValueError("Could not locate Vendor/Manuf sections in device.py")
    first_class_pairs = re.findall(
        r'^\s+(\w+)\s*=\s*"([^"]+)"\s*$',
        device_content[vendor_start:manuf_anchor],
        re.MULTILINE,
    )
    first_class = [display for _enum, display in first_class_pairs]
    manuf_pairs = re.findall(
        r'^\s+(\w+)\s*=\s*"([^"]+)",\s*VendorSource\.Manuf\s*$',
        device_content,
        re.MULTILINE,
    )
    manuf_content = _get_file_raw(token, IEEE_MANUF_PATH)
    oui_content, _ = _get_file(token, OUI_INFO_PY_PATH)
    ieee_identities = parse_ieee_manuf_pairs(manuf_content)
    if not ieee_identities:
        # Failing closed keeps an unattended batch from silently downgrading
        # every IEEE-backed promote to `new`.
        raise ValueError(f"IEEE manuf registry parsed empty from {IEEE_MANUF_PATH}")
    return (
        first_class,
        manuf_pairs,
        ieee_identities,
        parse_oui_vendor_mappings(oui_content),
        dict(first_class_pairs),
    )


def load_manuf_pairs(get_secret: Callable[[str, str], str]) -> list[tuple[str, str]]:
    """Compatibility wrapper for callers that only need Manuf rows."""
    return load_vendor_source(get_secret)[1]


def resolve_vendor_enum_key(device_py: str, canonical_display: str) -> str | None:
    pattern = re.compile(
        rf'^\s+(\w+)\s*=\s*"{re.escape(canonical_display)}"',
        re.MULTILINE,
    )
    match = pattern.search(device_py)
    return match.group(1) if match else None


def _build_batch_pr_body(rows: list[BatchPrRow], pr_mode: str, run_id: str) -> str:
    lines = [
        f"## Vendor Verifier batch PR (`{pr_mode}`)",
        "",
        f"**Run ID:** `{run_id}`",
        f"**Changes:** {len(rows)} vendor lead(s)",
        "",
        "| Jira | Case | Raw log string | Official / target |",
        "|------|------|----------------|-------------------|",
    ]
    for row in rows:
        if row.status == "DUPLICATE":
            case = "alias"
            target = row.duplicate_of or "?"
        else:
            case = f"`{row.pr_case}`"
            if row.should_add_alias:
                case += " + raw alias"
            target = row.official_name or row.vendor_name_raw
        lines.append(
            f"| [{row.jira_ticket}](https://team82.atlassian.net/browse/{row.jira_ticket}) "
            f"| {case} | `{row.vendor_name_raw}` | {target} |"
        )
    lines += ["", "*Created automatically by Vendor Verifier Coralogix pipeline*"]
    return "\n".join(lines)


def apply_batch_edits(
    device_content: str,
    types_content: str,
    oui_content: str,
    rows: list[BatchPrRow],
) -> tuple[str, str, str, list[BatchPrRow]]:
    new_rows = [r for r in rows if r.status == "COMPLETED"]
    alias_rows = [r for r in rows if r.status == "DUPLICATE" and r.should_add_alias]
    applied: list[BatchPrRow] = []

    for row in new_rows:
        before = device_content, types_content, oui_content
        enum_name = row.enum_name or ""
        official = row.official_name or row.vendor_name_raw
        try:
            if not enum_name:
                raise ValueError(f"Missing enum_name for new vendor row {row.vendor_name_raw!r}")
            if row.pr_case in ("promote", "promote_rename"):
                if not row.manuf_original_display:
                    raise ValueError("Manuf promotion is missing its source display")
                if row.manuf_source == "ieee":
                    if not row.manuf_ieee_short:
                        raise ValueError("IEEE promotion is missing its short name")
                    device_content = _edit_device_py(
                        device_content, enum_name, official
                    )
                    oui_content, _ = _edit_oui_info_ieee_promote(
                        oui_content,
                        row.manuf_original_display,
                        row.manuf_ieee_short,
                        enum_name,
                    )
                else:
                    if not row.manuf_enum:
                        raise ValueError("Enum promotion is missing its source enum")
                    device_content = _edit_device_py_promote(
                        device_content, row.manuf_enum, enum_name, official
                    )
                # An IEEE long name can differ from the new display even in a
                # plain promote, because the case is decided on cleaned names.
                if row.manuf_original_display != official:
                    device_content = _edit_device_py_alias(
                        device_content, enum_name, row.manuf_original_display
                    )
                if row.pr_case == "promote_rename" and row.manuf_source != "ieee":
                    oui_content, _ = _edit_oui_info(
                        oui_content, row.manuf_enum, enum_name
                    )
            else:
                device_content = _edit_device_py(device_content, enum_name, official)
            types_content = _edit_types_py(types_content, enum_name, official)
            if row.should_add_alias and row.vendor_name_raw.strip() != official.strip():
                device_content = _edit_device_py_alias(
                    device_content, enum_name, row.vendor_name_raw
                )
            applied.append(row)
        except Exception:
            device_content, types_content, oui_content = before
            logger.exception("Omitting failed batch vendor %r", row.vendor_name_raw)

    for row in alias_rows:
        before = device_content
        try:
            canonical = row.duplicate_of or row.official_name or ""
            enum_key = resolve_vendor_enum_key(device_content, canonical)
            if not enum_key:
                raise ValueError(
                    f"Could not resolve enum key for alias target {canonical!r} "
                    f"(raw={row.vendor_name_raw!r})"
                )
            device_content = _edit_device_py_alias(
                device_content, enum_key, row.vendor_name_raw
            )
            applied.append(row)
        except Exception:
            device_content = before
            logger.exception("Omitting failed batch alias %r", row.vendor_name_raw)

    return device_content, types_content, oui_content, applied


def create_batch_pr(
    rows: list[BatchPrRow],
    *,
    pr_mode: str,
    run_id: str,
    get_secret: Callable[[str, str], str],
    link_jira: bool = True,
) -> tuple[str, list[BatchPrRow]] | None:
    if not rows:
        return None

    token, auth_mode = _resolve_github_auth(get_secret)
    if not token:
        raise RuntimeError("GitHub auth failed — no token available")

    date_suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    lead_ticket = rows[0].jira_ticket
    branch_name = f"{lead_ticket}-wip/vendor-verifier/batch-{date_suffix}"

    staging_sha = _get_staging_sha(token)
    device_content, device_sha = _get_file(token, DEVICE_PY_PATH)
    types_content, types_sha = _get_file(token, TYPES_PY_PATH)
    oui_content, oui_sha = _get_file(token, OUI_INFO_PY_PATH)
    original_oui = oui_content
    device_content, types_content, oui_content, applied = apply_batch_edits(
        device_content, types_content, oui_content, rows
    )
    if not applied:
        logger.error("All batch rows failed edit validation; PR not opened")
        return None

    change_count = len(applied)
    commit_message = (
        f"{lead_ticket}: [Vendor Verifier] Batch {pr_mode} — {change_count} changes"
    )
    pr_title = (
        f"{lead_ticket}: [Vendor Verifier] Batch PR ({pr_mode}) — "
        f"{change_count} changes"
    )
    _create_branch(token, branch_name, staging_sha)
    _push_file(token, DEVICE_PY_PATH, device_content, device_sha, branch_name, commit_message)
    _push_file(token, TYPES_PY_PATH, types_content, types_sha, branch_name, commit_message)
    if oui_content != original_oui:
        _push_file(token, OUI_INFO_PY_PATH, oui_content, oui_sha, branch_name, commit_message)

    pr_url = _open_pr(
        token,
        pr_title,
        _build_batch_pr_body(applied, pr_mode, run_id),
        branch_name,
    )
    if auth_mode != "github-app":
        logger.warning("Batch PR created with auth mode=%s", auth_mode)

    if link_jira:
        email = get_secret(SECRET_SCOPE, "jira_email")
        jira_token = get_secret(SECRET_SCOPE, "jira_api_token")
        if email and jira_token:
            for row in applied:
                try:
                    link_pr_to_jira(row.jira_ticket, pr_url, email=email, token=jira_token)
                except Exception:
                    logger.exception("Failed linking PR to %s", row.jira_ticket)

    return pr_url, applied


def _esc_sql(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def create_batch_pr_from_spark(
    spark,
    candidates_table: str,
    run_id: str,
    pr_mode: str,
    *,
    auto_pr_confidences: set[str],
    get_secret: Callable[[str, str], str],
) -> str | None:
    """Query qualifying Delta rows, open one batch PR, write pr_url + pr_batch_id."""
    conf_list = ", ".join(f"'{c}'" for c in sorted(auto_pr_confidences))
    pending = spark.sql(
        f"""
        SELECT
            vendor_name_raw, jira_ticket, status, verdict, confidence_score,
            official_name, enum_name, duplicate_of, should_add_alias,
            website, hardware_evidence, supported_protocols,
            pr_case, manuf_enum, manuf_original_display,
            manuf_source, manuf_ieee_short
        FROM {candidates_table}
        WHERE jira_ticket IS NOT NULL
          AND (pr_url IS NULL OR TRIM(pr_url) = '')
          AND (
                (status = 'COMPLETED' AND verdict = 'LEGIT'
                 AND confidence_score IN ({conf_list})
                 AND COALESCE(distinct_companies_found, 1) = 1
                 AND (is_original_manufacturer IS NULL OR is_original_manufacturer = true))
             OR (status = 'DUPLICATE' AND should_add_alias = true)
          )
        ORDER BY orgs_count DESC NULLS LAST, vendor_name_raw
        """
    ).collect()

    if not pending:
        print("  No rows qualify for batch PR.")
        return None

    rows = [
        BatchPrRow(
            vendor_name_raw=r.vendor_name_raw,
            jira_ticket=r.jira_ticket,
            status=r.status,
            verdict=r.verdict,
            confidence_score=r.confidence_score,
            official_name=r.official_name,
            enum_name=r.enum_name,
            duplicate_of=r.duplicate_of,
            should_add_alias=bool(r.should_add_alias),
            website=r.website,
            hardware_evidence=r.hardware_evidence,
            supported_protocols=r.supported_protocols,
            pr_case=r.pr_case or "new",
            manuf_enum=r.manuf_enum,
            manuf_original_display=r.manuf_original_display,
            manuf_source=r.manuf_source,
            manuf_ieee_short=r.manuf_ieee_short,
        )
        for r in pending
    ]
    print(f"  Opening batch PR for {len(rows)} row(s)...")
    result = create_batch_pr(rows, pr_mode=pr_mode, run_id=run_id, get_secret=get_secret)
    if not result:
        return None
    pr_url, applied = result

    tickets_sql = ", ".join(f"'{_esc_sql(r.vendor_name_raw)}'" for r in applied)
    spark.sql(
        f"""
        UPDATE {candidates_table}
        SET pr_url = '{_esc_sql(pr_url)}',
            pr_batch_id = '{_esc_sql(run_id)}',
            last_seen = current_timestamp()
        WHERE vendor_name_raw IN ({tickets_sql})
        """
    )
    print(f"  Batch PR: {pr_url}")
    return pr_url
