"""GitHub PR creation for verified vendors.

Uses the GitHub REST API to:
  1. Create a branch off staging
  2. Edit device.py and types.py in-memory
  3. Push the changes
  4. Open a PR targeting staging
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
REPO = "medigateio/medigator"
BASE_BRANCH = "staging"

DEVICE_PY_PATH = "medigator/common/domain_model/xiot/device.py"
TYPES_PY_PATH = "medigator/common/domain_model/intels/vulnerabilities/types.py"
VENDOR_VERIFIER_BRANCH_MARKER = "vendor-verifier/adding_"


@dataclass(frozen=True)
class OpenVendorPr:
    url: str
    title: str
    number: int
    branch: str


@dataclass(frozen=True)
class GitHubAuth:
    token: str
    mode: str  # "github-app" | "env-pat" | "secret-pat" | "none"
    reason: str = ""


def _get_databricks_secret(scope: str, key: str) -> str:
    try:
        from databricks.sdk.core import Config

        cfg = Config()
        host = cfg.host.rstrip("/")
        headers = cfg.authenticate()
        resp = requests.get(
            f"{host}/api/2.0/secrets/get",
            headers=headers,
            params={"scope": scope, "key": key},
            timeout=10,
        )
        if resp.status_code == 200:
            return base64.b64decode(resp.json()["value"]).decode()
    except Exception:
        logger.exception("Failed reading Databricks secret %s/%s", scope, key)
    return ""


def _get_github_app_token() -> tuple[str, str]:
    """Return (installation token, failure reason)."""
    app_id = _get_databricks_secret("vendor-validation-app", "github_app_id").strip()
    private_key = _get_databricks_secret("vendor-validation-app", "github_app_private_key")
    installation_id = _get_databricks_secret(
        "vendor-validation-app", "github_app_installation_id"
    ).strip()

    if not app_id or not private_key or not installation_id:
        return "", "missing github_app_id/private_key/installation_id"

    try:
        import jwt as pyjwt

        now = int(time.time())
        app_jwt = pyjwt.encode(
            {
                "iat": now - 60,
                "exp": now + 600,
                "iss": str(app_id),
            },
            private_key,
            algorithm="RS256",
        )

        token_resp = requests.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        if token_resp.status_code != 201:
            logger.warning(
                "GitHub App token exchange failed: HTTP %s %s",
                token_resp.status_code,
                token_resp.text[:300],
            )
            return "", f"token exchange HTTP {token_resp.status_code}"
        token = token_resp.json().get("token", "")
        if not token:
            return "", "token exchange returned empty token"
        return token, ""
    except Exception:
        logger.exception("GitHub App auth failed")
        return "", "exception during GitHub App auth"


def _is_vendor_verifier_pr(pr: dict) -> bool:
    head = pr.get("head", {}).get("ref", "").lower()
    title = pr.get("title", "").lower()
    return VENDOR_VERIFIER_BRANCH_MARKER in head or "[vendor verifier]" in title


def _pr_matches_vendor(
    pr: dict,
    *,
    enum_name: str,
    display_name: str,
    input_vendor: str | None,
    case: str,
    alias_target_enum: str | None,
) -> bool:
    head = pr.get("head", {}).get("ref", "").lower()
    title = pr.get("title", "").lower()
    body = (pr.get("body") or "").lower()
    text_blob = f"{title} {body}"

    enum_key = enum_name.strip().lower()
    if enum_key and f"{VENDOR_VERIFIER_BRANCH_MARKER}{enum_key}" in head:
        return True

    for needle in (input_vendor, display_name):
        if needle and needle.strip().lower() in text_blob:
            if case == "alias" and "alias" in text_blob:
                return True
            if case == "promote" and "promote" in text_blob:
                return True
            if case == "new" and "add new vendor" in text_blob:
                return True

    if case == "alias" and alias_target_enum:
        target = alias_target_enum.strip().lower()
        if target and target in text_blob and "alias" in text_blob:
            return True

    return False


def find_open_vendor_pr(
    *,
    enum_name: str,
    display_name: str,
    input_vendor: str | None = None,
    case: str = "new",
    alias_target_enum: str | None = None,
) -> OpenVendorPr | None:
    """Return an open Vendor Verifier PR for this vendor, if one already exists."""
    auth = _resolve_github_auth()
    if not auth.token:
        return None

    try:
        resp = requests.get(
            f"{GITHUB_API}/repos/{REPO}/pulls",
            headers=_gh_headers(auth.token),
            params={"state": "open", "base": BASE_BRANCH, "per_page": 100},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to list open pull requests")
        return None

    for pr in resp.json():
        if not _is_vendor_verifier_pr(pr):
            continue
        if _pr_matches_vendor(
            pr,
            enum_name=enum_name,
            display_name=display_name,
            input_vendor=input_vendor,
            case=case,
            alias_target_enum=alias_target_enum,
        ):
            return OpenVendorPr(
                url=pr["html_url"],
                title=pr["title"],
                number=pr["number"],
                branch=pr["head"]["ref"],
            )
    return None


def _resolve_github_auth() -> GitHubAuth:
    app_token, app_reason = _get_github_app_token()
    if app_token:
        return GitHubAuth(token=app_token, mode="github-app")

    env_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_token:
        return GitHubAuth(
            token=env_token,
            mode="env-pat",
            reason=f"fallback because GitHub App failed: {app_reason or 'unknown'}",
        )

    secret_pat = _get_databricks_secret("vendor-validation-app", "github_token").strip()
    if secret_pat:
        return GitHubAuth(
            token=secret_pat,
            mode="secret-pat",
            reason=f"fallback because GitHub App failed: {app_reason or 'unknown'}",
        )

    return GitHubAuth(
        token="",
        mode="none",
        reason=f"GitHub App failed ({app_reason or 'unknown'}) and no PAT fallback found",
    )


def _get_github_token() -> str:
    """Backward-compatible token accessor used by app/db helpers."""
    return _resolve_github_auth().token


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
    if resp.status_code == 422 and "already exists" in resp.text:
        logger.warning("Branch %s already exists, reusing it", branch_name)
        return
    resp.raise_for_status()


def _get_file(token: str, path: str) -> tuple[str, str]:
    """Returns (decoded_content, blob_sha)."""
    resp = requests.get(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers=_gh_headers(token),
        params={"ref": BASE_BRANCH},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def _push_file(token: str, path: str, content: str, blob_sha: str, branch: str, message: str) -> None:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    resp = requests.put(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers=_gh_headers(token),
        json={
            "message": message,
            "content": encoded,
            "sha": blob_sha,
            "branch": branch,
        },
        timeout=30,
    )
    resp.raise_for_status()


def _open_pr(token: str, title: str, body: str, head: str) -> str:
    """Returns the PR HTML URL."""
    resp = requests.post(
        f"{GITHUB_API}/repos/{REPO}/pulls",
        headers=_gh_headers(token),
        json={
            "title": title,
            "body": body,
            "head": head,
            "base": BASE_BRANCH,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["html_url"]


def _edit_device_py_alias(content: str, existing_enum: str, alias_name: str) -> str:
    """Add alias_name to VENDOR_ALIASES entry for existing_enum in device.py."""
    existing_entry = f"Vendor.{existing_enum}: ("
    new_alias_line = f'        "{alias_name}",\n'

    if f'"{alias_name}"' in content:
        raise ValueError(f'Alias "{alias_name}" already exists in VENDOR_ALIASES')

    if existing_entry in content:
        # Append to existing tuple: insert before the closing ),
        close = content.index(existing_entry)
        # Find the closing paren of this tuple entry
        close_paren_idx = content.index("\n    ),", close)
        content = content[:close_paren_idx] + "\n" + new_alias_line + "    )," + content[close_paren_idx + len("\n    ),"):]
    else:
        # No existing entry — create a new one at the end of VENDOR_ALIASES
        aliases_close = "}\n\nassert all(\n    isinstance(value, tuple)"
        if aliases_close not in content:
            raise ValueError("Could not find end of VENDOR_ALIASES in device.py")
        new_entry = f'    Vendor.{existing_enum}: (\n        "{alias_name}",\n    ),\n'
        content = content.replace(aliases_close, new_entry + aliases_close)

    return content


def _edit_device_py_promote_manuf(
    content: str,
    manuf_enum_name: str,
    new_enum_name: str,
    new_display_name: str,
    manuf_original_display: str | None = None,
) -> str:
    """Promote a VendorSource.Manuf entry to a first-class system vendor.

    - Removes the Manuf line (matched by enum key, not display value)
    - Inserts a clean enum entry immediately before the blank line + manuf comment
      (same spacing as _edit_device_py — no extra blank line)
    """
    import re

    # Remove the Manuf line — match by enum key regardless of display value
    match = re.search(
        rf'^\s+{re.escape(manuf_enum_name)}\s*=\s*"[^"]+",\s*VendorSource\.Manuf\n',
        content, re.MULTILINE
    )
    if not match:
        raise ValueError(f"Could not find Manuf entry for {manuf_enum_name} in device.py")
    content = content.replace(match.group(0), "", 1)

    # Same anchor/spacing as _edit_device_py so we get:
    #   LastVendor = "..."
    #   NewVendor = "..."
    #
    #   # Vendors from manuf file #
    # not an extra blank line before the new entry.
    anchor = "\n\n    # Vendors from manuf file #"
    new_line = f'    {new_enum_name} = "{new_display_name}"'
    if anchor not in content:
        raise ValueError("Could not find insertion anchor in device.py")
    return content.replace(anchor, f"\n{new_line}{anchor}", 1)


def _edit_device_py_manuf(content: str, enum_name: str, display_name: str) -> str:
    """Insert new manuf-file Vendor entry after '# Vendors from manuf file #'."""
    anchor = "    # Vendors from manuf file #\n"
    new_line = f'\n    {enum_name} = "{display_name}", VendorSource.Manuf\n'
    if f'{enum_name} = ' in content:
        raise ValueError(f"{enum_name} already exists in device.py")
    if anchor not in content:
        raise ValueError("Could not find manuf file anchor in device.py")
    return content.replace(anchor, anchor + new_line, 1)


def _edit_device_py(content: str, enum_name: str, display_name: str) -> str:
    """Insert new Vendor enum entry before '# Vendors from manuf file #'.

    Original structure:
        LastVendor = "Last Vendor"
                                    ← blank line
        # Vendors from manuf file #

    Desired result:
        LastVendor = "Last Vendor"
        NewVendor = "New Vendor"
                                    ← blank line preserved
        # Vendors from manuf file #
    """
    # Anchor includes the blank line before the comment so we consume and rewrite it correctly
    anchor = "\n\n    # Vendors from manuf file #"
    new_line = f'    {enum_name} = "{display_name}"'
    if f'{enum_name} = "{display_name}"' in content:
        raise ValueError(f"{enum_name} already exists in device.py")
    if anchor not in content:
        raise ValueError("Could not find insertion anchor in device.py")
    return content.replace(anchor, f"\n{new_line}{anchor}", 1)


def _edit_types_py(content: str, enum_name: str, display_name: str) -> str:
    """Insert enum entry + manufacturer_sources entry into types.py."""
    enum_line = f'    {enum_name} = "{display_name}"\n'
    set_line = f"    VulnerabilityRelevanceSource.{enum_name},\n"

    if f'    {enum_name} = ' in content:
        raise ValueError(f"{enum_name} already exists in types.py")

    # Insert enum entry before get_description method.
    # Anchor includes both newlines (\n\n) so the blank line separator is preserved.
    enum_anchor = "\n\n    def get_description("
    if enum_anchor not in content:
        raise ValueError("Could not find get_description anchor in types.py")
    content = content.replace(enum_anchor, "\n" + enum_line + "\n    def get_description(", 1)

    # Insert into manufacturer_sources set before its closing brace
    # The set ends with }\n\n\nVULNERABILITY_SOURCE_STR_TO_TYPE
    set_anchor = "}\n\n\nVULNERABILITY_SOURCE_STR_TO_TYPE"
    if set_anchor not in content:
        raise ValueError("Could not find manufacturer_sources closing brace in types.py")
    content = content.replace(set_anchor, set_line + set_anchor, 1)

    return content


def _build_pr_body(display_name: str, jira_key: str, result: dict) -> str:
    website = result.get("website", "N/A")
    hardware = result.get("hardware_evidence", "N/A")
    protocols = ", ".join(result.get("supported_protocols", [])) or "N/A"
    confidence = result.get("confidence_score", "N/A")

    return f"""## Add new vendor: {display_name}

**Jira:** [{jira_key}](https://team82.atlassian.net/browse/{jira_key})
**Website:** {website}
**Confidence:** {confidence}

### Evidence
**Hardware:** {hardware}
**Protocols:** {protocols}

### Files changed
- `{DEVICE_PY_PATH}` — added `{display_name}` to `Vendor` enum
- `{TYPES_PY_PATH}` — added `{display_name}` to `VulnerabilityRelevanceSource` enum and `manufacturer_sources` set

*Created automatically by Vendor Verifier*"""


def test_github_connection() -> tuple[bool, str]:
    """Check token validity and staging branch access without creating anything.

    Returns (ok, message).
    """
    auth = _resolve_github_auth()
    if not auth.token:
        return False, auth.reason
    try:
        sha = _get_staging_sha(auth.token)
        mode_label = {
            "github-app": "GitHub App",
            "env-pat": "PAT (env fallback)",
            "secret-pat": "PAT (secret fallback)",
        }.get(auth.mode, auth.mode)
        note = f" — {auth.reason}" if auth.reason else ""
        return True, f"Connected via {mode_label}. `staging` branch SHA: `{sha[:10]}...`{note}"
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status == 401:
            return False, "Token is invalid or expired (401 Unauthorized)."
        if status == 403:
            return False, "Token lacks required permissions (403 Forbidden). Ensure Contents + Pull Requests read/write."
        if status == 404:
            return False, f"Repo `{REPO}` not found or token has no access (404)."
        return False, f"HTTP {status}: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def create_vendor_pr(
    enum_name: str,
    display_name: str,
    jira_key: str,
    result: dict,
    case: str = "new",
    alias_target_enum: str | None = None,
    manuf_enum_name: str | None = None,
    manuf_original_display: str | None = None,
    input_vendor: str | None = None,
) -> tuple[str, str]:
    """Create a GitHub PR with device.py and types.py changes.

    Args:
        case: "new" | "alias" | "manuf"
        alias_target_enum: required when case == "alias", the existing Vendor enum key

    Returns (pr_url, branch_name).
    Raises RuntimeError on failure.
    """
    auth = _resolve_github_auth()
    token = auth.token
    if not token:
        raise RuntimeError(f"GitHub auth failed: {auth.reason}")

    existing_pr = find_open_vendor_pr(
        enum_name=enum_name,
        display_name=display_name,
        input_vendor=input_vendor,
        case=case,
        alias_target_enum=alias_target_enum,
    )
    if existing_pr:
        raise RuntimeError(
            f"An open PR already exists for this vendor: #{existing_pr.number} "
            f"({existing_pr.title}) — {existing_pr.url}"
        )

    branch_name = f"{jira_key}-wip/vendor-verifier/adding_{enum_name}"
    if case == "alias":
        commit_message = f"{jira_key}: Add alias for {alias_target_enum}: {display_name}"
        pr_title = f"{jira_key}: [Vendor Verifier] Add alias: {display_name} → {alias_target_enum}"
    elif case == "promote":
        commit_message = f"{jira_key}: Promote manuf vendor to system: {display_name}"
        pr_title = f"{jira_key}: [Vendor Verifier] Promote to system vendor: {display_name}"
    else:
        commit_message = f"{jira_key}: Add new vendor: {display_name}"
        pr_title = f"{jira_key}: [Vendor Verifier] Add new vendor: {display_name}"

    try:
        staging_sha = _get_staging_sha(token)
        _create_branch(token, branch_name, staging_sha)

        device_content, device_sha = _get_file(token, DEVICE_PY_PATH)

        if case == "alias":
            if not alias_target_enum:
                raise ValueError("alias_target_enum is required for alias case")
            device_content = _edit_device_py_alias(device_content, alias_target_enum, display_name)
            _push_file(token, DEVICE_PY_PATH, device_content, device_sha, branch_name, commit_message)
        elif case == "promote":
            if not manuf_enum_name:
                raise ValueError("manuf_enum_name is required for promote case")
            types_content, types_sha = _get_file(token, TYPES_PY_PATH)
            device_content = _edit_device_py_promote_manuf(
                device_content, manuf_enum_name, enum_name, display_name, manuf_original_display
            )
            types_content = _edit_types_py(types_content, enum_name, display_name)
            _push_file(token, DEVICE_PY_PATH, device_content, device_sha, branch_name, commit_message)
            _push_file(token, TYPES_PY_PATH, types_content, types_sha, branch_name, commit_message)
        else:
            types_content, types_sha = _get_file(token, TYPES_PY_PATH)
            device_content = _edit_device_py(device_content, enum_name, display_name)
            types_content = _edit_types_py(types_content, enum_name, display_name)
            _push_file(token, DEVICE_PY_PATH, device_content, device_sha, branch_name, commit_message)
            _push_file(token, TYPES_PY_PATH, types_content, types_sha, branch_name, commit_message)

        pr_url = _open_pr(
            token,
            pr_title,
            _build_pr_body(display_name, jira_key, result),
            branch_name,
        )
        if auth.mode != "github-app":
            logger.warning("PR created with fallback auth mode=%s reason=%s", auth.mode, auth.reason)
        return pr_url, branch_name

    except Exception as e:
        raise RuntimeError(f"GitHub PR creation failed: {e}") from e
