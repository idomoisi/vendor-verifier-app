"""Jira helpers for the Coralogix vendor pipeline — keep in sync with vendor-verifier-app/jira.py."""

from __future__ import annotations

import base64
import logging
import re

import requests

logger = logging.getLogger(__name__)

JIRA_BASE_URL = "https://team82.atlassian.net"


def _pr_label_from_url(pr_url: str) -> str:
    match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if match:
        return f"{match.group(1)}#{match.group(2)}"
    return pr_url


def _comment_with_pr_link(pr_url: str, pr_label: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "GitHub batch PR opened by Vendor Verifier pipeline: "},
                    {
                        "type": "text",
                        "text": pr_label,
                        "marks": [{"type": "link", "attrs": {"href": pr_url}}],
                    },
                ],
            }
        ],
    }


def link_pr_to_jira(ticket_key: str, pr_url: str, *, email: str, token: str) -> None:
    """Best-effort Jira comment + remote link after batch PR creation."""
    headers = {
        "Authorization": f"Basic {base64.b64encode(f'{email}:{token}'.encode()).decode()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    pr_label = _pr_label_from_url(pr_url)

    comment_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_key}/comment",
        json={"body": _comment_with_pr_link(pr_url, pr_label)},
        headers=headers,
        timeout=15,
    )
    if comment_resp.status_code not in (200, 201):
        logger.warning(
            "Jira comment failed for %s (%s): %s",
            ticket_key,
            comment_resp.status_code,
            comment_resp.text,
        )

    pr_number = re.search(r"/pull/(\d+)", pr_url)
    global_id = f"vendor-verifier-pipeline:github:{pr_number.group(1) if pr_number else pr_url}"
    link_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_key}/remotelink",
        json={
            "globalId": global_id,
            "application": {"type": "com.github", "name": "GitHub"},
            "relationship": "is implemented by",
            "object": {
                "url": pr_url,
                "title": pr_label,
                "summary": "Vendor Verifier pipeline batch PR",
                "icon": {"url16x16": "https://github.githubassets.com/favicons/favicon.png"},
            },
        },
        headers=headers,
        timeout=15,
    )
    if link_resp.status_code not in (200, 201):
        logger.warning(
            "Jira remote link failed for %s (%s): %s",
            ticket_key,
            link_resp.status_code,
            link_resp.text,
        )
