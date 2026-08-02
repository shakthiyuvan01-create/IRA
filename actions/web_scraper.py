"""web_scraper.py — Scrape any URL for readable content.

Adapted from agentic-os-personal-main's server/collectors/firecrawl.js.
Uses requests and BeautifulSoup (both already in IRA).

Usage:
    result = scrape_url("https://example.com")
    result = search_web("AI news today")
"""

import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from data.database import get_db

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _extract_text(html):
    """Extract readable text from HTML, preferring main content areas."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove unwanted elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    # Try main content selectors
    for selector in ["article", "[role=main]", "main", ".content", "#content", ".post", ".article"]:
        el = soup.select_one(selector)
        if el:
            return el.get_text(separator="\n", strip=True)

    # Fallback to body
    body = soup.find("body")
    return body.get_text(separator="\n", strip=True) if body else ""


def scrape_url(url, save=False):
    """Scrape a URL and return its title, summary, and text content."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e)}

    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    text = _extract_text(resp.text)
    summary = text[:600] if text else ""

    result = {
        "success": True,
        "title": title,
        "url": url,
        "summary": summary,
        "text_length": len(text),
    }

    if save:
        db = get_db()
        db.add_article(
            title=title, url=url, summary=summary,
            raw_markdown=text[:12000], source="scrape",
        )
        result["saved"] = True

    return result


def search_web(query, limit=5):
    """Simple web search using DuckDuckGo's HTML interface (no API key needed)."""
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query}
    try:
        resp = requests.post(url, data=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e)}

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for i, link in enumerate(soup.select(".result__a")):
        if i >= limit:
            break
        title = link.get_text(strip=True)
        href = link.get("href", "")
        # Extract the actual URL from DuckDuckGo's redirect
        match = re.search(r"uddg=(https?://[^&]+)", href)
        actual_url = match.group(1) if match else href
        snippet_el = link.find_next(".result__snippet")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        results.append({"title": title, "url": actual_url, "snippet": snippet})

    return {"success": True, "results": results, "count": len(results)}


def web_scraper(parameters=None, response=None, player=None, session_memory=None, speak=None):
    """Tool entry point — scrape a URL or search the web."""
    params = parameters or {}
    action = params.get("action", "scrape").strip().lower()
    url = params.get("url", "").strip()
    query = params.get("query", "").strip()
    save = params.get("save", False)

    if action == "search":
        if not query:
            return "What would you like me to search for, Yuvan?"
        if player:
            player.write_log("[Scraper] Searching: " + query)
        if speak:
            speak("Searching the web. One moment, Yuvan.")
        result = search_web(query)
        if not result.get("success"):
            return "Search failed: " + result.get("error", "Unknown error")
        if not result["results"]:
            return "No results found for: " + query
        lines = ["Search results for '" + query + "':"]
        for r in result["results"]:
            lines.append(r["title"] + " - " + r["url"])
        return "\n".join(lines)

    if not url:
        return "Please provide a URL to scrape, Yuvan."
    if player:
        player.write_log("[Scraper] Scraping: " + url)
    if speak:
        speak("Scraping the page. One moment, Yuvan.")
    result = scrape_url(url, save=save)
    if not result.get("success"):
        return "Scraping failed: " + result.get("error", "Unknown error")
    return (
        'Scraped: "' + result["title"] + '" (' + str(result["text_length"])
        + " chars). Summary: " + result["summary"][:300]
    )
