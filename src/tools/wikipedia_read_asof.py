import os
import re
import json
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs, unquote

import httpx

from src.tools import AsyncTool, ToolResult
from src.registry import TOOL

# ---- Config / defaults -------------------------------------------------------
OLDID_ENFORCER_BASE = os.getenv("OLDID_ENFORCER_BASE", "http://127.0.0.1:8008")
WIKI_API = "https://en.wikipedia.org/w/api.php"

UA = os.getenv(
    "WIKI_USER_AGENT",
    "PATQA-OldidEnforcer/0.1 (academic research; YOUR_EMAIL@university.edu)",
)

MAX_EXTRACT_CHARS = int(os.getenv("WIKI_EXTRACT_MAX_CHARS", "6000"))  # trim huge pages


def _isoify_to_eod(s: str) -> str:
    """Ensure YYYY-MM-DD -> YYYY-MM-DDT23:59:59Z; pass through full ISO with Z/+."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s + "T23:59:59Z"
    if s.endswith("Z") or "+" in s:
        return s
    return s + "Z"


def _title_from_query_or_url(q: str) -> str:
    """
    Accepts a page title OR a full enwiki URL and returns canonical title.
    Handles:
      - https://en.wikipedia.org/wiki/Title_With_Underscores
      - https://en.wikipedia.org/w/index.php?title=Foo&oldid=123
      - plain "Foo bar"
    """
    # If it looks like a URL to enwiki, parse out the title
    try:
        pu = urlparse(q)
        if pu.netloc.endswith("wikipedia.org"):
            if pu.path.startswith("/wiki/"):
                title = unquote(pu.path.split("/wiki/", 1)[1])
                return title.replace("_", " ")
            # /w/index.php?title=...
            qs = parse_qs(pu.query)
            if "title" in qs and len(qs["title"]) > 0:
                return qs["title"][0].replace("_", " ")
    except Exception:
        pass
    # Otherwise treat as a title string
    return q.strip()


async def _oldid_before(client: httpx.AsyncClient, title: str, t_query: str) -> Tuple[int, str, str]:
    """Call your OldidEnforcer to resolve the revision at/before t_query."""
    params = {"title": title, "t_query": t_query}
    r = await client.get(f"{OLDID_ENFORCER_BASE}/wiki/oldid_before", params=params, timeout=30.0)
    if r.status_code == 403:
        raise RuntimeError("OldidEnforcer: 403 Forbidden (set a descriptive User-Agent).")
    if r.status_code == 404:
        # Bubble up a friendly message with the service's detail (if present)
        try:
            msg = r.json().get("detail") or r.text
        except Exception:
            msg = r.text
        raise RuntimeError(f"OldidEnforcer: {msg}")
    r.raise_for_status()
    data = r.json()
    return int(data["rev_id"]), data["rev_time"], data["oldid_url"]


async def _fetch_extract_for_oldid(client: httpx.AsyncClient, oldid: int) -> str:
    """
    Get plaintext extract for a specific oldid.
    Uses `prop=extracts&explaintext=1` for a clean text block.
    """
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": 1,
        "format": "json",
        "formatversion": 2,
        "oldid": oldid,
    }
    r = await client.get(WIKI_API, params=params, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return ""
    return pages[0].get("extract", "") or ""


@TOOL.register_module(name="wikipedia_read_asof", force=True)
class WikipediaAsOfTool(AsyncTool):
    name = "wikipedia_read_asof"
    description = "Reads English Wikipedia content *as of* time t using OldidEnforcer (returns text + oldid_url)."
    parameters = {
        "type": "object",
        "properties": {
            "query_or_url": {"type": "string", "description": "Page title or a wikipedia URL"},
            "t_query": {"type": "string", "description": "As-of date (YYYY-MM-DD or full ISO)", "nullable": True},
        },
        "required": ["query_or_url"],
    }
    output_type = "any"

    async def forward(self, query_or_url: str, t_query: Optional[str] = None) -> ToolResult:
        """
        1) Normalize the title from title/URL
        2) Resolve oldid at/before t_query via OldidEnforcer
        3) Fetch plaintext for that oldid from enwiki API
        4) Return a compact JSON string with title, oldid_url, rev info, and extract (possibly truncated)
        """
        try:
            # Normalize inputs
            title = _title_from_query_or_url(query_or_url)
            if not title:
                return ToolResult(output=None, error="Empty title after parsing query_or_url.")

            # Default t_query = current UTC date end-of-day (latest as-of now)
            if not t_query:
                t_query = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            t_query_iso = _isoify_to_eod(t_query)

            headers = {"User-Agent": UA, "Accept": "application/json"}

            async with httpx.AsyncClient(headers=headers) as client:
                rev_id, rev_time, oldid_url = await _oldid_before(client, title, t_query_iso)
                extract = await _fetch_extract_for_oldid(client, rev_id)

            truncated = False
            if len(extract) > MAX_EXTRACT_CHARS:
                extract = extract[:MAX_EXTRACT_CHARS].rstrip() + "\n… [truncated]"
                truncated = True

            payload = {
                "source": "enwiki",
                "title": title,
                "as_of": t_query,
                "rev_id": rev_id,
                "rev_time": rev_time,
                "oldid_url": oldid_url,
                "extract": extract,
                "extract_truncated": truncated,
                "extract_chars": len(extract),
            }

            # Return JSON string so the agent can easily parse/cite oldid_url
            return ToolResult(output=json.dumps(payload, ensure_ascii=False), error=None)

        except httpx.HTTPError as e:
            return ToolResult(output=None, error=f"HTTP error: {str(e)}")
        except Exception as e:
            return ToolResult(output=None, error=str(e))
