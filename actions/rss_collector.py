"""rss_collector.py — RSS/Atom feed parsing and article collection.

Adapted from agentic-os-personal-main's server/collectors/rss.js.
Requires: feedparser (pip install feedparser)
"""

import re
from datetime import datetime
from core.data.database import get_db


def _import_feedparser():
    try:
        import feedparser
        return feedparser
    except ImportError:
        return None


def collect_rss(feed_url, topic_tags=None, max_items=50):
    fp = _import_feedparser()
    if fp is None:
        return {"success": False, "error": "feedparser not installed"}
    try:
        feed = fp.parse(feed_url)
    except Exception as e:
        return {"success": False, "error": str(e)}
    if feed.bozo and not feed.entries:
        return {"success": False, "error": "Invalid RSS feed"}
    feed_title = feed.feed.get("title", feed_url)
    tags = topic_tags or []
    db = get_db()
    added = 0
    found = 0
    for entry in feed.entries[:max_items]:
        found += 1
        title = entry.get("title", "(untitled)").strip()
        link = entry.get("link") or entry.get("guid") or feed_url
        summary_raw = (entry.get("summary") or entry.get("description") or "")
        summary = re.sub(r"<[^>]+>", " ", summary_raw).strip()[:600]
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        pub_str = datetime(*published[:6]).isoformat() if published else None
        result = db.add_article(
            title=title, url=link, summary=summary,
            raw_markdown=summary_raw[:8000], source="rss:" + feed_url,
            topic_tags=tags, published_at=pub_str,
        )
        if result:
            added += 1
    return {"success": True, "feed_title": feed_title, "found": found, "added": added}


def rss_collector(parameters=None, response=None, player=None, session_memory=None, speak=None):
    params = parameters or {}
    feed_url = params.get("url", "").strip()
    tags = params.get("tags", "").strip()
    if not feed_url:
        return "Please provide an RSS feed URL, Yuvan."
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if player:
        player.write_log("[RSS] Collecting: " + feed_url)
    if speak:
        speak("Collecting articles. One moment, Yuvan.")
    result = collect_rss(feed_url, topic_tags=tag_list)
    if not result.get("success"):
        return "RSS collection failed: " + result.get("error", "Unknown error")
    return (
        'Collected ' + str(result["added"]) + ' new articles from "'
        + result["feed_title"] + '" (' + str(result["found"])
        + ' total found), Yuvan.'
    )
