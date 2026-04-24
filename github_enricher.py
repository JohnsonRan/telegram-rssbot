"""
github_enricher.py — Full Changelog link for GitHub Release RSS entries.

Strategy: parse the compare URL from the entry HTML body first (no API call);
fall back to the GitHub Releases API when the URL is absent (e.g. manual notes).
"""

import logging
import os
import re
from html import escape

import httpx

logger = logging.getLogger(__name__)

_FEED_RE = re.compile(r"^https?://github\.com/([^/]+/[^/]+)/releases\.atom$", re.IGNORECASE)
_TAG_RE  = re.compile(r"/releases/tag/([^\"'\s<>]+)$", re.IGNORECASE)
_CMP_RE  = re.compile(r"https://github\.com/[^/]+/[^/]+/compare/[^\s\"'<>]+\.\.\.[^\s\"'<>]+", re.IGNORECASE)

_GH_API = "https://api.github.com"
_GH_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def is_github_release_feed(feed_url: str) -> bool:
    return bool(_FEED_RE.match(feed_url or ""))


def _entry_html(entry) -> str:
    for item in entry.get("content") or []:
        if isinstance(item, dict) and item.get("value"):
            return item["value"]
    return entry.get("summary") or ""


def _fmt(compare_url: str) -> str:
    label = compare_url.split("/compare/", 1)[-1]
    return f'🔗 <a href="{escape(compare_url, quote=True)}">Full Changelog: {escape(label)}</a>'


def _from_content(entry) -> str | None:
    """Extract compare URL from the entry's rendered HTML body."""
    m = _CMP_RE.search(_entry_html(entry))
    return _fmt(m.group(0).rstrip(".,;)")) if m else None


async def _from_api(http_client: httpx.AsyncClient, feed_url: str, entry) -> str | None:
    """Fall back to GitHub Releases API to determine the previous tag."""
    repo_m = _FEED_RE.match(feed_url or "")
    tag_m  = _TAG_RE.search(entry.get("link") or "")
    if not repo_m or not tag_m:
        return None

    repo, current_tag = repo_m.group(1), tag_m.group(1)

    headers = dict(_GH_HEADERS)
    if token := os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = await http_client.get(
            f"{_GH_API}/repos/{repo}/releases",
            params={"per_page": 10},
            headers=headers,
            timeout=8.0,
        )
        if resp.status_code != 200:
            logger.warning("GitHub API %s for %s", resp.status_code, repo)
            return None
        tags = [r["tag_name"] for r in resp.json()]
    except Exception as exc:
        logger.warning("GitHub API error for %s: %s", repo, exc)
        return None

    try:
        idx = tags.index(current_tag)
    except ValueError:
        return None

    if idx + 1 >= len(tags):
        return None  # first-ever release, no previous tag

    return _fmt(f"https://github.com/{repo}/compare/{tags[idx + 1]}...{current_tag}")


async def get_changelog_link(http_client: httpx.AsyncClient, feed_url: str, entry) -> str | None:
    """Return a formatted HTML changelog link, or None if unavailable."""
    return _from_content(entry) or await _from_api(http_client, feed_url, entry)
