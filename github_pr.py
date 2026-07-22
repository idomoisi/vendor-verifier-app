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

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
REPO = "medigateio/medigator"
BASE_BRANCH = "staging"

DEVICE_PY_PATH = "medigator/common/domain_model/xiot/device.py"
TYPES_PY_PATH = "medigator/common/domain_model/intels/vulnerabilities/types.py"


def _get_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        import requests as req
        from databricks.sdk.core import Config

        cfg = Config()
        host = cfg.host.rstrip("/")
        headers = cfg.authenticate()
        resp = req.get(
            f"{host}/api/2.0/secrets/get",
            headers=headers,
            params={"scope": "vendor-validation-app", "key": "github_token"},
            timeout=10,
        )
        if resp.status_code == 200:
            return base64.b64decode(resp.json()["value"]).decode()
    except Exception:
        pass
    return ""


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
    - Inserts a clean enum entry before '# Vendors from manuf file #'
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

    # Insert clean entry before the manuf comment
    anchor = "    # Vendors from manuf file #"
    new_line = f'    {new_enum_name} = "{new_display_name}"\n'
    if anchor not in content:
        raise ValueError("Could not find insertion anchor in device.py")
    return content.replace(anchor, new_line + "\n" + anchor)


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
    return content.replace(anchor, f"\n    {new_line}\n{anchor}", 1)


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
    token = _get_github_token()
    if not token:
        return False, "GitHub token not found in secrets scope `vendor-validation-app` key `github_token`."
    try:
        sha = _get_staging_sha(token)
        return True, f"Connected. `staging` branch SHA: `{sha[:10]}...`"
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
) -> tuple[str, str]:
    """Create a GitHub PR with device.py and types.py changes.

    Args:
        case: "new" | "alias" | "manuf"
        alias_target_enum: required when case == "alias", the existing Vendor enum key

    Returns (pr_url, branch_name).
    Raises RuntimeError on failure.
    """
    token = _get_github_token()
    if not token:
        raise RuntimeError(
            "GitHub token not configured. Add `github_token` to the "
            "`vendor-validation-app` Databricks secret scope."
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
        return pr_url, branch_name

    except Exception as e:
        raise RuntimeError(f"GitHub PR creation failed: {e}") from e
