"""Batch GitHub PR creation for the Coralogix vendor pipeline.

Mirrors vendor-verifier-app/github_pr.py but applies many vendors/aliases in one branch.

Batch-only policy: the pipeline never opens a PR for a single vendor. A batch needs at
least ``MIN_BATCH_SIZE`` rows (>= 2); smaller batches stay pending for a later run.
Single-vendor PRs remain the interactive Device Labeler path only.
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
SECRET_SCOPE = "vendor-validation-app"

# Smallest batch the pipeline is allowed to open a PR for. A batch of one is a
# single-vendor PR, which this pipeline must never produce.
MIN_BATCH_SIZE = 2


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
    content = base64.b64decode(data["content"]).decode()
    return content, data["sha"]


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
    if f'"{alias_name}"' in content:
        return content
    existing_entry = f"Vendor.{existing_enum}: ("
    new_alias_line = f'        "{alias_name}",\n'
    if existing_entry in content:
        close_paren_idx = content.index("\n    ),", content.index(existing_entry))
        return (
            content[:close_paren_idx]
            + "\n"
            + new_alias_line
            + "    ),"
            + content[close_paren_idx + len("\n    ),") :]
        )
    aliases_close = "}\n\nassert all(\n    isinstance(value, tuple)"
    if aliases_close not in content:
        raise ValueError("Could not find end of VENDOR_ALIASES in device.py")
    new_entry = f'    Vendor.{existing_enum}: (\n        "{alias_name}",\n    ),\n'
    return content.replace(aliases_close, new_entry + aliases_close)


def resolve_vendor_enum_key(device_py: str, canonical_display: str) -> str | None:
    pattern = re.compile(
        rf'^\s+(\w+)\s*=\s*"{re.escape(canonical_display)}"',
        re.MULTILINE,
    )
    match = pattern.search(device_py)
    return match.group(1) if match else None


def _distinct_tickets(rows: list[BatchPrRow]) -> list[str]:
    """Batch tickets in row order, de-duplicated (all rows of one run share a ticket)."""
    seen: list[str] = []
    for row in rows:
        if row.jira_ticket and row.jira_ticket not in seen:
            seen.append(row.jira_ticket)
    return seen


def _build_batch_pr_body(rows: list[BatchPrRow], pr_mode: str, run_id: str) -> str:
    tickets = _distinct_tickets(rows)
    ticket_links = ", ".join(
        f"[{key}](https://team82.atlassian.net/browse/{key})" for key in tickets
    )
    lines = [
        f"## Vendor Verifier batch PR (`{pr_mode}`)",
        "",
        f"**Run ID:** `{run_id}`",
        f"**Batch ticket(s):** {ticket_links or 'N/A'}",
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
            case = "new vendor"
            if row.should_add_alias:
                case = "new vendor + alias"
            target = row.official_name or row.vendor_name_raw
        lines.append(
            f"| [{row.jira_ticket}](https://team82.atlassian.net/browse/{row.jira_ticket}) "
            f"| {case} | `{row.vendor_name_raw}` | {target} |"
        )
    lines += ["", "*Created automatically by Vendor Verifier Coralogix pipeline*"]
    return "\n".join(lines)


def apply_batch_edits(device_content: str, types_content: str, rows: list[BatchPrRow]) -> tuple[str, str]:
    new_rows = [r for r in rows if r.status == "COMPLETED"]
    alias_rows = [r for r in rows if r.status == "DUPLICATE" and r.should_add_alias]

    for row in new_rows:
        enum_name = row.enum_name or ""
        official = row.official_name or row.vendor_name_raw
        if not enum_name:
            raise ValueError(f"Missing enum_name for new vendor row {row.vendor_name_raw!r}")
        device_content = _edit_device_py(device_content, enum_name, official)
        types_content = _edit_types_py(types_content, enum_name, official)
        if row.should_add_alias and row.vendor_name_raw.strip() != official.strip():
            device_content = _edit_device_py_alias(device_content, enum_name, row.vendor_name_raw)

    for row in alias_rows:
        canonical = row.duplicate_of or row.official_name or ""
        enum_key = resolve_vendor_enum_key(device_content, canonical)
        if not enum_key:
            raise ValueError(
                f"Could not resolve enum key for alias target {canonical!r} "
                f"(raw={row.vendor_name_raw!r})"
            )
        device_content = _edit_device_py_alias(device_content, enum_key, row.vendor_name_raw)

    return device_content, types_content


def create_batch_pr(
    rows: list[BatchPrRow],
    *,
    pr_mode: str,
    run_id: str,
    get_secret: Callable[[str, str], str],
    link_jira: bool = True,
    min_batch_size: int = MIN_BATCH_SIZE,
) -> str | None:
    if not rows:
        return None
    if min_batch_size < MIN_BATCH_SIZE:
        raise ValueError(
            f"min_batch_size={min_batch_size} is below the batch-only floor {MIN_BATCH_SIZE}"
        )
    if len(rows) < min_batch_size:
        raise ValueError(
            f"Refusing to open a PR for {len(rows)} row(s): the pipeline is batch-only "
            f"(min_batch_size={min_batch_size}). Single-vendor PRs go through Device Labeler."
        )

    token, auth_mode = _resolve_github_auth(get_secret)
    if not token:
        raise RuntimeError("GitHub auth failed — no token available")

    date_suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    tickets = _distinct_tickets(rows)
    if not tickets:
        raise ValueError("Batch rows carry no Jira ticket — run CREATE_TICKETS first")
    lead_ticket = tickets[0]
    branch_name = f"{lead_ticket}-wip/vendor-verifier/batch-{date_suffix}"
    commit_message = f"{lead_ticket}: [Vendor Verifier] Batch {pr_mode} — {len(rows)} changes"
    pr_title = f"{lead_ticket}: [Vendor Verifier] Batch PR ({pr_mode}) — {len(rows)} changes"

    staging_sha = _get_staging_sha(token)
    _create_branch(token, branch_name, staging_sha)

    device_content, device_sha = _get_file(token, DEVICE_PY_PATH)
    types_content, types_sha = _get_file(token, TYPES_PY_PATH)
    device_content, types_content = apply_batch_edits(device_content, types_content, rows)

    _push_file(token, DEVICE_PY_PATH, device_content, device_sha, branch_name, commit_message)
    _push_file(token, TYPES_PY_PATH, types_content, types_sha, branch_name, commit_message)

    pr_url = _open_pr(
        token,
        pr_title,
        _build_batch_pr_body(rows, pr_mode, run_id),
        branch_name,
    )
    if auth_mode != "github-app":
        logger.warning("Batch PR created with auth mode=%s", auth_mode)

    if link_jira:
        email = get_secret(SECRET_SCOPE, "jira_email")
        jira_token = get_secret(SECRET_SCOPE, "jira_api_token")
        if email and jira_token:
            for ticket in tickets:
                try:
                    link_pr_to_jira(ticket, pr_url, email=email, token=jira_token)
                except Exception:
                    logger.exception("Failed linking PR to %s", ticket)

    return pr_url


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
    min_batch_size: int = MIN_BATCH_SIZE,
) -> str | None:
    """Query qualifying Delta rows, open one batch PR, write pr_url + pr_batch_id.

    Returns None when fewer than ``min_batch_size`` rows qualify — those rows keep an
    empty ``pr_url`` and roll into the next run's batch.
    """
    conf_list = ", ".join(f"'{c}'" for c in sorted(auto_pr_confidences))
    pending = spark.sql(
        f"""
        SELECT
            vendor_name_raw, jira_ticket, status, verdict, confidence_score,
            official_name, enum_name, duplicate_of, should_add_alias,
            website, hardware_evidence, supported_protocols
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

    if len(pending) < min_batch_size:
        print(
            f"  Held back: {len(pending)} row(s) qualify but the pipeline is batch-only "
            f"(min_batch_size={min_batch_size}). They stay pending for the next run."
        )
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
        )
        for r in pending
    ]
    print(f"  Opening batch PR for {len(rows)} row(s)...")
    pr_url = create_batch_pr(
        rows,
        pr_mode=pr_mode,
        run_id=run_id,
        get_secret=get_secret,
        min_batch_size=min_batch_size,
    )
    if not pr_url:
        return None

    tickets_sql = ", ".join(f"'{_esc_sql(r.vendor_name_raw)}'" for r in rows)
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
