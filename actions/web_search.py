"""
Web Search — Returns real search results to Gemini.

v3.1: Uses duckduckgo_search for actual results with snippets.
Falls back to browser if everything fails.
"""

import logging
import os
import subprocess
import urllib.parse
import webbrowser

log = logging.getLogger(__name__)


def web_search(query: str) -> str:
    """Search the web and return text results to Gemini."""
    if not query.strip():
        return "No query provided."

    # ── Primary: duckduckgo_search (real results) ─────────────
    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))

        if results:
            lines = []
            for i, r in enumerate(results[:5], 1):
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                lines.append(f"{i}. {title}\n   {body}\n   ({href})")

            answer = "\n\n".join(lines)
            log.info("🔍 DDG search for '%s': %d results", query, len(results))
            return answer[:1500]  # truncate for Gemini context

    except ImportError:
        log.warning("duckduckgo_search not installed, falling back to API")
    except Exception as e:
        log.warning("DDG search failed: %s", e)

    # ── Fallback: DuckDuckGo Instant Answer API ───────────────
    try:
        import json
        import urllib.request

        params = urllib.parse.urlencode({
            "q": query, "format": "json",
            "no_html": "1", "skip_disambig": "1",
        })
        url = f"https://api.duckduckgo.com/?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/3.1"})

        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        abstract = data.get("AbstractText", "").strip()
        if abstract:
            source = data.get("AbstractSource", "")
            return f"{abstract}\n(Source: {source})"

        answer = data.get("Answer", "").strip()
        if answer:
            return answer

    except Exception as e:
        log.warning("DDG API fallback failed: %s", e)

    # ── Last resort: open browser ─────────────────────────────
    try:
        search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
        webbrowser.open(search_url)
        log.info("🌐 Opened browser for: %s", query)
        return f"No instant answer found. Opened browser search for: {query}"
    except Exception as e:
        return f"Search failed: {e}"
