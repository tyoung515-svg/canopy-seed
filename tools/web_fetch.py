"""
Web fetch tool with domain-level safety gating.

Three tiers:
  SAFE   — fetches automatically, no confirmation
  CONFIRM — prompts user before fetching
  BLOCKED — always rejected

Caller must check the tier before calling fetch().
"""

import logging
from typing import Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 15
MAX_RESPONSE_BYTES = 64 * 1024

DOMAIN_POLICY: dict[str, str] = {
    "api.frankfurter.app": "safe",
    "open.er-api.com": "safe",
    "api.coinbase.com": "safe",
    "api.open-meteo.com": "safe",
    "wttr.in": "safe",
    "api.github.com": "safe",
    "ipinfo.io": "safe",
    "httpbin.org": "safe",
    "jsonplaceholder.typicode.com": "safe",
    "wikipedia.org": "confirm",
    "en.wikipedia.org": "confirm",
    "news.ycombinator.com": "confirm",
    "reddit.com": "confirm",
    "www.reddit.com": "confirm",
}

BLOCKED_PATTERNS = [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "192.168.",
    "10.0.",
    "10.1.",
    "172.16.",
    "169.254.",
    ".local",
    ".internal",
    "metadata.google",
    "169.254.169.254",
]


def get_domain_policy(url: str) -> Tuple[str, str]:
    """
    Returns (policy, domain) for a URL.
    policy: "safe" | "confirm" | "blocked"
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "blocked", ""

    if parsed.scheme not in ("http", "https"):
        return "blocked", ""

    hostname = parsed.hostname or ""
    hostname_l = hostname.lower()

    for pattern in BLOCKED_PATTERNS:
        if pattern in hostname_l:
            return "blocked", hostname

    if hostname_l in DOMAIN_POLICY:
        return DOMAIN_POLICY[hostname_l], hostname

    for domain, policy in DOMAIN_POLICY.items():
        if hostname_l.endswith("." + domain) or hostname_l == domain:
            return policy, hostname

    return "confirm", hostname


async def fetch(url: str, headers: dict | None = None, timeout: int = TIMEOUT) -> str:
    """
    Perform the HTTP GET. Caller must have already checked get_domain_policy().
    Returns response text (truncated to MAX_RESPONSE_BYTES).
    Raises httpx.HTTPError on failure.
    """
    if url.startswith("http://"):
        domain = (urlparse(url).hostname or "").lower()
        if domain not in ("localhost", "127.0.0.1"):
            url = "https://" + url[7:]

    default_headers = {
        "User-Agent": "CanopySeed/1.0 (personal assistant)",
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        default_headers.update(headers)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, max_redirects=3) as client:
        response = await client.get(url, headers=default_headers)
        response.raise_for_status()

    content = response.text
    if len(content) > MAX_RESPONSE_BYTES:
        content = content[:MAX_RESPONSE_BYTES] + "\n...[truncated]"
    return content
