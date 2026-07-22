"""Jira integration -- creates NET tickets for verified vendors."""

from __future__ import annotations

import base64
import os
import requests


JIRA_BASE_URL = "https://team82.atlassian.net"
JIRA_PROJECT_KEY = "NET"
JIRA_ISSUE_TYPE = "Task"
JIRA_DEFAULT_ASSIGNEE = "712020:af3f3d43-c255-456b-8230-4d2bcec470ee"  # Ido Moisi


def _get_jira_credentials() -> tuple[str, str]:
    """Return (email, api_token) from env vars or Databricks secrets."""
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if email and token:
        return email, token

    try:
        import requests as req
        from databricks.sdk.core import Config

        cfg = Config()
        host = cfg.host.rstrip("/")
        headers = cfg.authenticate()

        def _get_secret(scope: str, key: str) -> str:
            resp = req.get(
                f"{host}/api/2.0/secrets/get",
                headers=headers,
                params={"scope": scope, "key": key},
                timeout=10,
            )
            if resp.status_code == 200:
                return base64.b64decode(resp.json()["value"]).decode()
            return ""

        email = email or _get_secret("vendor-validation-app", "jira_email")
        token = token or _get_secret("vendor-validation-app", "jira_api_token")
    except Exception:
        pass

    return email, token


def _auth_header(email: str, token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _build_description(
    vendor_name: str,
    enum_name: str,
    display_name: str,
    result: dict,
) -> dict:
    """Build Jira ADF description body."""
    website = result.get("website", "N/A")
    verdict = result.get("verdict", "LEGIT")
    hardware = result.get("hardware_evidence", "N/A")
    protocols = ", ".join(result.get("supported_protocols", [])) or "N/A"
    industries = ", ".join(result.get("industries", [])) or "N/A"
    analyst_note = result.get("analyst_note", "")

    lines = [
        f"Adding **{display_name}** vendor to the medigator codebase.",
        "",
        f"**AI Verdict:** {verdict}",
        f"**Website:** {website}",
        f"**Hardware Evidence:** {hardware}",
        f"**Protocols:** {protocols}",
        f"**Industries:** {industries}",
    ]
    if analyst_note:
        lines += ["", f"**Analyst Note:** {analyst_note}"]

    lines += [
        "",
        "---",
        "**Files to modify:**",
        "",
        f"- `medigator/common/domain_model/xiot/device.py` — add `{enum_name} = \"{display_name}\"` to `Vendor` enum",
        f"- `medigator/common/domain_model/intels/vulnerabilities/types.py` — add `{enum_name} = \"{display_name}\"` to `VulnerabilityRelevanceSource` enum and `manufacturer_sources` set",
    ]

    content = []
    for line in lines:
        if line == "---":
            content.append({"type": "rule"})
        elif line == "":
            content.append({"type": "paragraph", "content": [{"type": "text", "text": " "}]})
        else:
            paragraph_content: list[dict] = []
            # Bold markers (**text**) — simplified: strip them and use strong marks
            import re
            parts = re.split(r"\*\*(.+?)\*\*", line)
            for i, part in enumerate(parts):
                if not part:
                    continue
                if i % 2 == 1:
                    paragraph_content.append({"type": "text", "text": part, "marks": [{"type": "strong"}]})
                else:
                    paragraph_content.append({"type": "text", "text": part})
            content.append({"type": "paragraph", "content": paragraph_content})

    return {
        "type": "doc",
        "version": 1,
        "content": content,
    }


def create_vendor_ticket(
    vendor_name: str,
    enum_name: str,
    display_name: str,
    result: dict,
) -> tuple[str, str]:
    """Create a NET Jira ticket and add the maestro label.

    Returns (ticket_key, ticket_url) on success.
    Raises RuntimeError on failure.
    """
    email, token = _get_jira_credentials()
    if not email or not token:
        raise RuntimeError(
            "Jira credentials not configured. Add `jira_email` and `jira_api_token` "
            "to the `vendor-validation-app` Databricks secret scope."
        )

    headers = _auth_header(email, token)

    # 1. Create issue
    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": f"[Vendor Verifier] Add new vendor: {display_name}",
            "issuetype": {"name": JIRA_ISSUE_TYPE},
            "description": _build_description(vendor_name, enum_name, display_name, result),
            "labels": ["maestro"],
            "assignee": {"id": JIRA_DEFAULT_ASSIGNEE},
        }
    }

    resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json=payload,
        headers=headers,
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Jira create failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    ticket_key = data["key"]
    ticket_url = f"{JIRA_BASE_URL}/browse/{ticket_key}"
    return ticket_key, ticket_url
