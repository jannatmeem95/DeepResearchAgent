#!/usr/bin/env python3
import re, html, json, time
from typing import Dict
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

APP = FastAPI(title="OldidEnforcer", version="0.1")
WIKI_API = "https://en.wikipedia.org/w/api.php"

UA = "PATQA-OldidEnforcer/0.1 (academic research; YOUR_EMAIL@university.edu)"
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "application/json"})

def isoify_to_eod(s: str) -> str:
    return s + "T23:59:59Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else (s if s.endswith("Z") or "+" in s else s+"Z")

@APP.get("/wiki/oldid_before")
def oldid_before(title: str, t_query: str):
    """Return latest revision ≤ t_query for 'title'."""
    params = {
        "action":"query","prop":"revisions","titles":title,
        "rvlimit":1,"rvdir":"older","rvstart":isoify_to_eod(t_query),
        "rvend":"2001-01-01T00:00:00Z","rvprop":"ids|timestamp",
        "redirects":1,"format":"json","formatversion":2,"maxlag":5
    }
    r = S.get(WIKI_API, params=params, timeout=30)
    if r.status_code == 403:
        raise HTTPException(403, "Forbidden. Use a descriptive User-Agent.")
    r.raise_for_status()
    data = r.json()
    pages = data.get("query",{}).get("pages",[])
    if not pages or "revisions" not in pages[0]:
        raise HTTPException(404, f"No revision for '{title}' ≤ {t_query}")
    page = pages[0]
    rev = page["revisions"][0]
    rev_id = rev["revid"]
    rev_time = rev["timestamp"]
    resolved = page.get("title", title)
    return JSONResponse({
        "title": resolved,
        "rev_id": rev_id,
        "rev_time": rev_time,
        "oldid_url": f"https://en.wikipedia.org/w/index.php?oldid={rev_id}"
    })
#225932
if __name__ == "__main__":
    uvicorn.run(APP, host="0.0.0.0", port=8008)
