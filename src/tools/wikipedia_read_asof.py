# src/tools/wikipedia_asof_tool.py
import os
import json
import re
from urllib.parse import urlparse, parse_qs, unquote

import httpx

from src.tools import AsyncTool, ToolResult
from src.registry import TOOL

WIKI_API = "https://en.wikipedia.org/w/api.php"

def _default_user_agent() -> str:
    # Good UA format per Wikimedia policy; override via env if you want.
    # Example: PATQA-OldidClient/0.1 (academic research; you@uni.edu)
    return os.getenv(
        "WIKI_USER_AGENT",
        "PATQA-OldidClient/0.1 (academic research; YOUR_EMAIL@university.edu)"
    )

def _oldid_enforcer_base() -> str:
    # Where your FastAPI OldidEnforcer is running
    return os.getenv("OLDID_ENFORCER_BASE", "http://127.0.0.1:8008")

def _extract_title_or_oldid(query_or_url: str):
    """
    Accepts a page title OR a full enwiki URL.
    Returns a tuple: (title: str|None, oldid: int|None)
    """
    if re.match(r"^https?://", query_or_url):
        u = urlparse(query_or_url)
        qs = parse_qs(u.query or "")
        # Try oldid in query first
        if "oldid" in qs and qs["oldid"]:
            try:
                return None, int(qs["oldid"][0])
            except ValueError:
                pass
        # Else pull title from /wiki/Title path
        if "/wiki/" in u.path:
            raw = u.path.split("/wiki/", 1)[1]
            return unquote(raw.replace("_", " ")), None
        # Or from ?title=... query
        if "title" in qs and qs["title"]:
            return unquote(qs["title"][0]).replace("_", " "), None
        # Fallback: nothing parseable
        return None, None
    else:
        # Looks like a plain title
        return query_or_url.strip(), None

async def _resolve_oldid_with_enforcer(title: str, t_query: str, ua: str) -> dict:
    """
    Call OldidEnforcer to get {title, rev_id, rev_time, oldid_url}
    """
    base = _oldid_enforcer_base()
    url = f"{base}/wiki/oldid_before"
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": ua, "Accept": "application/json"}) as client:
        r = await client.get(url, params={"title": title, "t_query": t_query})
        # Let non-2xx raise so we capture details below
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Surface OldidEnforcer message if available
            msg = None
            try:
                msg = r.json()
            except Exception:
                pass
            raise RuntimeError(f"OldidEnforcer error {r.status_code}: {msg or r.text}") from e
        return r.json()

async def _fetch_html_for_oldid(oldid: int, ua: str) -> dict:
    """
    Use the Wikipedia API to fetch HTML for the given oldid.
    Returns a dict with { "html": str, "sections": [...]} where sections may be empty.
    """
    params = {
        "action": "parse",
        "oldid": oldid,
        "prop": "text|sections",
        "format": "json",
        "formatversion": 2
    }
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": ua, "Accept": "application/json"}) as client:
        r = await client.get(WIKI_API, params=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Wikipedia API error: {data['error']}")
        parsed = data.get("parse", {})
        return {
            "html": (parsed.get("text") or ""),
            "sections": (parsed.get("sections") or []),
            "title": parsed.get("title")
        }

@TOOL.register_module(name="wikipedia_read_asof", force=True)
class WikipediaAsOfTool(AsyncTool):
    name = "wikipedia_read_asof"
    description = "Reads English Wikipedia content *as of* time t using OldidEnforcer (no browser)."
    parameters = {
        "type": "object",
        "properties": {
            "query_or_url": {"type": "string", "description": "Page title or a wikipedia URL"},
            "t_query": {"type":"string", "description":"As-of date/time, e.g. 2024-04-15 (YYYY-MM-DD or ISO8601)", "nullable": True},
        },
        "required": ["query_or_url"]
    }
    output_type = "any"

    def __init__(self,
                 model_id: str = "langchain-Qwen", # "gpt-4.1",
                 ):

        super(WikipediaAsOfTool, self).__init__()
        self.ua = _default_user_agent()

    async def forward(self, query_or_url: str, t_query: str = None) -> ToolResult:
        """
        - If query_or_url contains oldid=..., fetch that exact revision (t_query ignored).
        - Else, resolve the oldid for (title, t_query) via OldidEnforcer, then fetch HTML.
        Returns JSON as a string in ToolResult.output.
        """
        try:
            title, oldid = _extract_title_or_oldid(query_or_url)

            # If we already have an oldid from the URL, just fetch it.
            oldid_meta = None
            if oldid is None:
                if not title:
                    return ToolResult(output=None, error="Could not parse a title or oldid from query_or_url.")
                if not t_query:
                    # If you really want to allow 'latest', you could call action=parse&page=title here,
                    # but this tool is meant to be *as of* a time. Be explicit:
                    return ToolResult(output=None, error="t_query is required when no oldid is provided.")
                oldid_meta = await _resolve_oldid_with_enforcer(title, t_query, self.ua)
                oldid = int(oldid_meta["rev_id"])

            html_pkg = await _fetch_html_for_oldid(oldid, self.ua)

            result = {
                "input": {"query_or_url": query_or_url, "t_query": t_query},
                "resolved": {
                    "title": html_pkg.get("title") or (oldid_meta.get("title") if oldid_meta else title),
                    "rev_id": oldid,
                    "rev_time": (oldid_meta.get("rev_time") if oldid_meta else None),
                    "oldid_url": (oldid_meta.get("oldid_url") if oldid_meta else f"https://en.wikipedia.org/w/index.php?oldid={oldid}")
                },
                "content": {
                    "html": html_pkg["html"],
                    "sections": html_pkg["sections"],
                }
            }
            return ToolResult(output=json.dumps(result), error=None)

        except Exception as e:
            # Bubble a concise error for the agent
            return ToolResult(output=None, error=f"wikipedia_asof_tool error: {e}")
