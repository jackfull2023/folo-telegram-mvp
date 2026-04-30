#!/usr/bin/env python3
"""
Folo Webhook -> Filter/Dedupe -> Telegram Channel MVP.

This app is intentionally dependency-free. It receives Folo Action webhooks,
scores entries with local JSON config, stores pushed/seen entries in SQLite,
and sends accepted entries to Telegram.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("RADAR_CONFIG", ROOT / "config.example.json"))
DB_PATH = Path(os.getenv("RADAR_DB", ROOT / "data" / "radar.sqlite"))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)

    config.setdefault("server", {})
    config.setdefault("telegram", {})
    config.setdefault("filter", {})
    config.setdefault("accounts", {})
    config.setdefault("keywords", {})
    config.setdefault("negative_keywords", {})
    config.setdefault("limits", {})
    config.setdefault("poll", {})
    config.setdefault("feeds", [])
    config.setdefault("scoring", {})

    return config


CONFIG = load_config()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
                feed_url TEXT,
                feed_title TEXT,
                title TEXT,
                url TEXT,
                published_at TEXT,
                inserted_at TEXT,
                score INTEGER,
                status TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_created_at
            ON entries(created_at)
            """
        )


def entry_id(payload: dict[str, Any]) -> str:
    entry = payload.get("entry") or {}
    feed = payload.get("feed") or {}
    raw_id = entry.get("guid") or entry.get("id") or entry.get("url")
    if raw_id:
        return str(raw_id)
    base = "|".join(
        [
            str(feed.get("url") or ""),
            str(entry.get("title") or ""),
            str(entry.get("publishedAt") or ""),
        ]
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def already_seen(item_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT 1 FROM entries WHERE id = ?", (item_id,)).fetchone()
    return row is not None


def reserve_entry(item_id: str, payload: dict[str, Any]) -> bool:
    entry = payload.get("entry") or {}
    feed = payload.get("feed") or {}
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT status FROM entries WHERE id = ?", (item_id,)).fetchone()
        if row and row[0] != "error":
            return False
        if row and row[0] == "error":
            con.execute(
                """
                UPDATE entries
                SET feed_url = ?, feed_title = ?, title = ?, url = ?, published_at = ?,
                    inserted_at = ?, score = 0, status = 'processing',
                    reason = 'retry_after_error', created_at = ?
                WHERE id = ?
                """,
                (
                    feed.get("url"),
                    feed.get("title"),
                    entry.get("title"),
                    entry.get("url"),
                    entry.get("publishedAt"),
                    entry.get("insertedAt"),
                    now_iso(),
                    item_id,
                ),
            )
            return True
        con.execute(
            """
            INSERT INTO entries (
                id, feed_url, feed_title, title, url, published_at, inserted_at,
                score, status, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'processing', 'processing', ?)
            """,
            (
                item_id,
                feed.get("url"),
                feed.get("title"),
                entry.get("title"),
                entry.get("url"),
                entry.get("publishedAt"),
                entry.get("insertedAt"),
                now_iso(),
            ),
        )
    return True


def feed_has_seen_entries(feed_url: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT 1 FROM entries WHERE feed_url = ? LIMIT 1", (feed_url,)).fetchone()
    return row is not None


def save_entry(
    item_id: str,
    payload: dict[str, Any],
    score: int,
    status: str,
    reason: str,
) -> None:
    entry = payload.get("entry") or {}
    feed = payload.get("feed") or {}
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO entries (
                id, feed_url, feed_title, title, url, published_at, inserted_at,
                score, status, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                feed_url = excluded.feed_url,
                feed_title = excluded.feed_title,
                title = excluded.title,
                url = excluded.url,
                published_at = excluded.published_at,
                inserted_at = excluded.inserted_at,
                score = excluded.score,
                status = excluded.status,
                reason = excluded.reason
            """,
            (
                item_id,
                feed.get("url"),
                feed.get("title"),
                entry.get("title"),
                entry.get("url"),
                entry.get("publishedAt"),
                entry.get("insertedAt"),
                score,
                status,
                reason,
                now_iso(),
            ),
        )


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def compact_for_compare(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)


def trim_title_ellipsis(value: str) -> str:
    return re.sub(r"(\.{3}|…)+$", "", value).strip()


def title_description_overlap(title: str, description: str) -> bool:
    title_key = compact_for_compare(trim_title_ellipsis(title))
    desc_key = compact_for_compare(description)
    if not title_key or not desc_key:
        return False
    if title_key == desc_key:
        return True
    if len(title_key) >= 12 and desc_key.startswith(title_key):
        return True
    if len(desc_key) >= 12 and title_key.startswith(desc_key):
        return True
    shorter = min(len(title_key), len(desc_key))
    if shorter < 20:
        return False
    shared = 0
    for a, b in zip(title_key, desc_key):
        if a != b:
            break
        shared += 1
    return shared / shorter >= 0.8


def text_blob(payload: dict[str, Any]) -> str:
    entry = payload.get("entry") or {}
    feed = payload.get("feed") or {}
    parts = [
        feed.get("title"),
        entry.get("author"),
        entry.get("title"),
        strip_html(entry.get("description")),
        strip_html(entry.get("content")),
    ]
    return "\n".join(str(p) for p in parts if p).lower()


def entry_text(payload: dict[str, Any], field: str) -> str:
    entry = payload.get("entry") or {}
    return strip_html(entry.get(field))


def feed_text(payload: dict[str, Any], field: str) -> str:
    feed = payload.get("feed") or {}
    return strip_html(feed.get(field))


def entry_url(payload: dict[str, Any]) -> str:
    return str((payload.get("entry") or {}).get("url") or "")


def field_blob(payload: dict[str, Any], fields: list[str]) -> str:
    entry = payload.get("entry") or {}
    feed = payload.get("feed") or {}
    values: list[str] = []
    for field in fields:
        source, _, name = field.partition(".")
        if source == "entry":
            values.append(strip_html(entry.get(name)))
        elif source == "feed":
            values.append(strip_html(feed.get(name)))
        elif field == "url":
            values.append(str(entry.get("url") or ""))
        elif field == "author":
            values.append(str(entry.get("author") or ""))
        elif field == "title":
            values.append(strip_html(entry.get("title")))
        elif field == "description":
            values.append(strip_html(entry.get("description")))
        elif field == "content":
            values.append(strip_html(entry.get("content")))
        elif field == "feed_title":
            values.append(strip_html(feed.get("title")))
        elif field == "feed_url":
            values.append(str(feed.get("url") or ""))
        elif field == "site_url":
            values.append(str(feed.get("siteUrl") or ""))
    return "\n".join(value for value in values if value).lower()


def match_weighted_terms(text: str, terms: dict[str, Any], reason_prefix: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    text = text.lower()
    for term, raw_weight in terms.items():
        keyword = str(term).lower()
        if not keyword:
            continue
        if keyword in text:
            weight = int(raw_weight)
            score += weight
            reasons.append(f"{reason_prefix}:{term}{weight:+d}")
    return score, reasons


def match_weighted_patterns(text: str, patterns: dict[str, Any], reason_prefix: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    for pattern, raw_weight in patterns.items():
        try:
            matched = re.search(str(pattern), text, re.IGNORECASE) is not None
        except re.error:
            matched = str(pattern).lower() in text.lower()
        if matched:
            weight = int(raw_weight)
            score += weight
            reasons.append(f"{reason_prefix}:{pattern}{weight:+d}")
    return score, reasons


def add_score(current: int, reasons: list[str], delta: int, delta_reasons: list[str]) -> int:
    if delta:
        current += delta
        reasons.extend(delta_reasons)
    return current


def published_age_hours(payload: dict[str, Any]) -> float | None:
    published = (payload.get("entry") or {}).get("publishedAt")
    if not published:
        return None
    try:
        published_dt = dt.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return None
    if published_dt.tzinfo is None:
        published_dt = published_dt.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - published_dt).total_seconds() / 3600


def account_key(payload: dict[str, Any]) -> str:
    entry = payload.get("entry") or {}
    feed = payload.get("feed") or {}
    candidates = [
        entry.get("author"),
        feed.get("title"),
        feed.get("siteUrl"),
        feed.get("url"),
        entry.get("url"),
    ]
    joined = " ".join(str(c) for c in candidates if c).lower()
    for name in CONFIG["accounts"]:
        normalized = name.lower().lstrip("@")
        if normalized and normalized in joined:
            return name
    return ""


def score_payload(payload: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    scoring = CONFIG.get("scoring") or {}
    has_scoring_config = bool(scoring)
    use_legacy_accounts = bool(scoring.get("use_legacy_accounts", True))
    use_legacy_keywords = bool(scoring.get("use_legacy_keywords", not has_scoring_config))
    use_legacy_negative_keywords = bool(scoring.get("use_legacy_negative_keywords", not has_scoring_config))
    blob = text_blob(payload)

    account = account_key(payload)
    if account and use_legacy_accounts:
        weight = int(CONFIG["accounts"].get(account, 0))
        score += weight
        reasons.append(f"account:{account}{weight:+d}")

    feed_title = feed_text(payload, "title")
    feed_url = feed_text(payload, "url")
    site_url = feed_text(payload, "siteUrl")
    title = entry_text(payload, "title")
    description = entry_text(payload, "description")
    content = entry_text(payload, "content")
    url = entry_url(payload)

    if use_legacy_keywords:
        for keyword, weight in CONFIG["keywords"].items():
            if keyword.lower() in blob:
                value = int(weight)
                score += value
                reasons.append(f"keyword:{keyword}{value:+d}")

    if use_legacy_negative_keywords:
        for keyword, weight in CONFIG["negative_keywords"].items():
            if keyword.lower() in blob:
                value = int(weight)
                score += value
                reasons.append(f"negative:{keyword}{value:+d}")

    delta, delta_reasons = match_weighted_terms(feed_title, scoring.get("feed_title_weights", {}), "feed")
    score = add_score(score, reasons, delta, delta_reasons)

    delta, delta_reasons = match_weighted_terms(feed_url, scoring.get("feed_url_weights", {}), "feed_url")
    score = add_score(score, reasons, delta, delta_reasons)

    delta, delta_reasons = match_weighted_terms(site_url, scoring.get("site_url_weights", {}), "site_url")
    score = add_score(score, reasons, delta, delta_reasons)

    delta, delta_reasons = match_weighted_terms(title, scoring.get("title_keywords", {}), "title")
    score = add_score(score, reasons, delta, delta_reasons)

    delta, delta_reasons = match_weighted_terms(
        "\n".join(part for part in [description, content] if part),
        scoring.get("body_keywords", {}),
        "body",
    )
    score = add_score(score, reasons, delta, delta_reasons)

    delta, delta_reasons = match_weighted_terms(url, scoring.get("url_keywords", {}), "url")
    score = add_score(score, reasons, delta, delta_reasons)

    delta, delta_reasons = match_weighted_patterns(blob, scoring.get("regex", {}), "regex")
    score = add_score(score, reasons, delta, delta_reasons)

    for rule in scoring.get("field_rules", []):
        if not isinstance(rule, dict):
            continue
        fields = [str(field) for field in rule.get("fields", [])]
        terms = rule.get("terms", {})
        if not fields or not isinstance(terms, dict):
            continue
        name = str(rule.get("name") or "field")
        delta, delta_reasons = match_weighted_terms(field_blob(payload, fields), terms, name)
        score = add_score(score, reasons, delta, delta_reasons)

    age_hours = published_age_hours(payload)
    freshness_rules = scoring.get("freshness")
    if isinstance(freshness_rules, list) and age_hours is not None:
        for rule in freshness_rules:
            if not isinstance(rule, dict):
                continue
            max_hours = float(rule.get("max_hours", 0))
            if max_hours > 0 and age_hours <= max_hours:
                weight = int(rule.get("score", 0))
                score += weight
                reasons.append(f"fresh<={max_hours:g}h{weight:+d}")
                break
    elif age_hours is not None and age_hours <= 3:
        score += 2
        reasons.append("fresh<=3h+2")

    return score, reasons


def hourly_push_count() -> int:
    one_hour_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM entries WHERE status = 'pushed' AND created_at >= ?",
            (one_hour_ago,),
        ).fetchone()
    return int(row[0] if row else 0)


def should_push(payload: dict[str, Any]) -> tuple[bool, int, str]:
    score, reasons = score_payload(payload)
    min_score = int(CONFIG["filter"].get("min_score", 8))
    max_per_hour = int(CONFIG["limits"].get("max_pushes_per_hour", 20))

    if hourly_push_count() >= max_per_hour:
        return False, score, f"hourly_limit:{max_per_hour}"
    if score < min_score:
        return False, score, f"low_score:{score}<{min_score}; {'; '.join(reasons)}"
    return True, score, "; ".join(reasons)


def telegram_message(payload: dict[str, Any], score: int, reason: str) -> str:
    entry = payload.get("entry") or {}
    feed = payload.get("feed") or {}

    feed_title = html.escape(str(feed.get("title") or "Folo"))
    raw_title = strip_html(entry.get("title")) or "New entry"
    url = html.escape(str(entry.get("url") or ""))
    author = html.escape(str(entry.get("author") or ""))
    raw_desc = strip_html(entry.get("description") or entry.get("content"))
    dedupe_desc = bool(CONFIG["telegram"].get("dedupe_title_description", True))
    use_desc_as_body = bool(dedupe_desc and title_description_overlap(raw_title, raw_desc))

    title = html.escape(raw_title)
    desc = raw_desc
    if len(desc) > 360:
        desc = desc[:357].rstrip() + "..."
    desc = html.escape(desc)

    show_debug = bool(CONFIG["telegram"].get("include_debug", False))
    lines = [f"<b>{feed_title}</b>"]
    if author:
        lines.append(f"<i>{author}</i>")
    lines.append("")
    if use_desc_as_body and desc:
        lines.append(desc)
    else:
        lines.append(f"<b>{title}</b>")
    if desc and not use_desc_as_body:
        lines.append("")
        lines.append(desc)
    if url:
        lines.append("")
        lines.append(url)
    if show_debug:
        lines.append("")
        lines.append(f"<code>score={score} {html.escape(reason)}</code>")
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or CONFIG["telegram"].get("bot_token")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or CONFIG["telegram"].get("chat_id")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": str(bool(CONFIG["telegram"].get("disable_web_page_preview", False))).lower(),
        }
    ).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Telegram returned HTTP {resp.status}")


def handle_payload(payload: dict[str, Any]) -> tuple[int, str]:
    item_id = entry_id(payload)
    if not reserve_entry(item_id, payload):
        return 200, "duplicate"

    score = 0
    reason = ""
    try:
        push, score, reason = should_push(payload)
        if not push:
            save_entry(item_id, payload, score, "skipped", reason)
            return 200, f"skipped:{reason}"

        message = telegram_message(payload, score, reason)
        send_telegram(message)
        save_entry(item_id, payload, score, "pushed", reason)
        return 200, "pushed"
    except Exception as exc:
        error_reason = f"{reason}; error:{exc}" if reason else f"error:{exc}"
        save_entry(item_id, payload, score, "error", error_reason[:500])
        raise


def parse_feed_datetime(value: str | None) -> str:
    if not value:
        return now_iso()
    value = value.strip()
    if not value:
        return now_iso()
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return now_iso()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def text_from(element: ET.Element | None, paths: list[str]) -> str:
    if element is None:
        return ""
    for path in paths:
        found = element.find(path)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def attr_from(element: ET.Element | None, paths: list[tuple[str, str]]) -> str:
    if element is None:
        return ""
    for path, attr in paths:
        found = element.find(path)
        if found is not None:
            value = found.attrib.get(attr)
            if value:
                return value.strip()
    return ""


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def children_by_name(element: ET.Element | None, name: str) -> list[ET.Element]:
    if element is None:
        return []
    expected = name.lower()
    return [child for child in element if local_name(child.tag) == expected]


def child_by_name(element: ET.Element | None, name: str) -> ET.Element | None:
    matches = children_by_name(element, name)
    return matches[0] if matches else None


def text_by_name(element: ET.Element | None, names: list[str]) -> str:
    if element is None:
        return ""
    expected = {name.lower() for name in names}
    for child in element:
        if local_name(child.tag) in expected and child.text:
            return child.text.strip()
    return ""


def text_any(element: ET.Element | None, paths: list[str], names: list[str] | None = None) -> str:
    return text_from(element, paths) or text_by_name(element, names or paths)


def first_link(element: ET.Element | None, atom: bool = False) -> str:
    if element is None:
        return ""
    if atom:
        for link in element.findall("{http://www.w3.org/2005/Atom}link"):
            rel = link.attrib.get("rel", "alternate")
            href = link.attrib.get("href")
            if href and rel == "alternate":
                return href.strip()
        return attr_from(element, [("{http://www.w3.org/2005/Atom}link", "href")])
    return text_any(element, ["link"])


def fetch_xml(url: str) -> ET.Element:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "folo-telegram-mvp/0.2 (+https://github.com/jackfull2023/folo-telegram-mvp)",
        },
    )
    timeout = int(CONFIG["poll"].get("fetch_timeout_seconds", 20))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return ET.fromstring(raw)


def configured_feeds() -> list[dict[str, str]]:
    feeds: list[dict[str, str]] = []
    seen: set[str] = set()

    for feed in CONFIG.get("feeds", []):
        if not isinstance(feed, dict):
            continue
        url = str(feed.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        feeds.append(
            {
                "url": url,
                "title": str(feed.get("title") or "").strip(),
                "siteUrl": str(feed.get("siteUrl") or feed.get("site_url") or "").strip(),
                "author": str(feed.get("author") or "").strip(),
            }
        )

    poll_config = CONFIG["poll"]
    if bool(poll_config.get("auto_from_accounts", False)):
        rsshub_base = str(
            os.getenv("RSSHUB_BASE")
            or poll_config.get("rsshub_base")
            or "https://rsshub.app"
        ).rstrip("/")
        route_template = str(poll_config.get("rsshub_route_template") or "/twitter/user/{account}")
        for account in CONFIG.get("accounts", {}):
            name = str(account).lstrip("@")
            if not name:
                continue
            path = route_template.format(account=urllib.parse.quote(name, safe=""))
            url = f"{rsshub_base}{path if path.startswith('/') else '/' + path}"
            if url in seen:
                continue
            seen.add(url)
            feeds.append(
                {
                    "url": url,
                    "title": f"Twitter @{name}",
                    "siteUrl": f"https://x.com/{name}",
                    "author": name,
                }
            )

    return feeds


def rss_items(root: ET.Element, feed_config: dict[str, str]) -> list[dict[str, Any]]:
    channel = root.find("channel") or child_by_name(root, "channel")
    if channel is None:
        return []

    feed_title = feed_config.get("title") or text_any(channel, ["title"]) or feed_config["url"]
    site_url = feed_config.get("siteUrl") or first_link(channel)
    items: list[dict[str, Any]] = []

    channel_items = channel.findall("item") or children_by_name(channel, "item")
    for item in channel_items:
        guid = text_any(item, ["guid"])
        title = text_any(item, ["title"])
        link = first_link(item)
        description = text_any(
            item,
            [
                "description",
                "{http://purl.org/rss/1.0/modules/content/}encoded",
                "{http://search.yahoo.com/mrss/}description",
            ],
            ["description", "encoded"],
        )
        author = (
            text_any(item, ["author", "{http://purl.org/dc/elements/1.1/}creator"], ["author", "creator"])
            or feed_config.get("author", "")
        )
        published = parse_feed_datetime(
            text_any(
                item,
                [
                    "pubDate",
                    "published",
                    "updated",
                    "{http://purl.org/dc/elements/1.1/}date",
                ],
                ["pubDate", "published", "updated", "date"],
            )
        )
        items.append(
            {
                "entry": {
                    "id": guid or link or hashlib.sha256(f"{feed_config['url']}|{title}|{published}".encode("utf-8")).hexdigest(),
                    "guid": guid or link,
                    "publishedAt": published,
                    "insertedAt": now_iso(),
                    "title": title,
                    "description": description,
                    "author": author,
                    "url": link,
                    "media": None,
                },
                "feed": {
                    "url": feed_config["url"],
                    "siteUrl": site_url,
                    "title": feed_title,
                    "checkedAt": now_iso(),
                },
                "view": 0,
            }
        )
    return items


def atom_items(root: ET.Element, feed_config: dict[str, str]) -> list[dict[str, Any]]:
    atom = "{http://www.w3.org/2005/Atom}"
    feed_title = feed_config.get("title") or text_from(root, [f"{atom}title"]) or feed_config["url"]
    site_url = feed_config.get("siteUrl") or first_link(root, atom=True)
    items: list[dict[str, Any]] = []

    for item in root.findall(f"{atom}entry"):
        guid = text_from(item, [f"{atom}id"])
        title = text_from(item, [f"{atom}title"])
        link = first_link(item, atom=True)
        description = text_from(item, [f"{atom}summary", f"{atom}content"])
        author = (
            text_from(item, [f"{atom}author/{atom}name"])
            or feed_config.get("author", "")
        )
        published = parse_feed_datetime(text_from(item, [f"{atom}published", f"{atom}updated"]))
        items.append(
            {
                "entry": {
                    "id": guid or link or hashlib.sha256(f"{feed_config['url']}|{title}|{published}".encode("utf-8")).hexdigest(),
                    "guid": guid or link,
                    "publishedAt": published,
                    "insertedAt": now_iso(),
                    "title": title,
                    "description": description,
                    "author": author,
                    "url": link,
                    "media": None,
                },
                "feed": {
                    "url": feed_config["url"],
                    "siteUrl": site_url,
                    "title": feed_title,
                    "checkedAt": now_iso(),
                },
                "view": 0,
            }
        )
    return items


def rdf_items(root: ET.Element, feed_config: dict[str, str]) -> list[dict[str, Any]]:
    channel = child_by_name(root, "channel")
    feed_title = feed_config.get("title") or text_any(channel, ["title"]) or feed_config["url"]
    site_url = feed_config.get("siteUrl") or first_link(channel)
    items: list[dict[str, Any]] = []

    for item in children_by_name(root, "item"):
        guid = item.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")
        title = text_any(item, ["title"])
        link = first_link(item)
        description = text_any(
            item,
            ["description", "{http://purl.org/rss/1.0/modules/content/}encoded"],
            ["description", "encoded"],
        )
        author = text_any(item, ["{http://purl.org/dc/elements/1.1/}creator"], ["creator"]) or feed_config.get("author", "")
        published = parse_feed_datetime(text_any(item, ["{http://purl.org/dc/elements/1.1/}date"], ["date"]))
        items.append(
            {
                "entry": {
                    "id": guid or link or hashlib.sha256(f"{feed_config['url']}|{title}|{published}".encode("utf-8")).hexdigest(),
                    "guid": guid or link,
                    "publishedAt": published,
                    "insertedAt": now_iso(),
                    "title": title,
                    "description": description,
                    "author": author,
                    "url": link,
                    "media": None,
                },
                "feed": {
                    "url": feed_config["url"],
                    "siteUrl": site_url,
                    "title": feed_title,
                    "checkedAt": now_iso(),
                },
                "view": 0,
            }
        )
    return items


def fetch_feed_payloads(feed_config: dict[str, str]) -> list[dict[str, Any]]:
    root = fetch_xml(feed_config["url"])
    tag = root.tag.lower()
    if tag.endswith("rss"):
        return rss_items(root, feed_config)
    if tag.endswith("feed"):
        return atom_items(root, feed_config)
    if tag.endswith("rdf"):
        return rdf_items(root, feed_config)
    raise ValueError(f"Unsupported feed root: {root.tag}")


def run_poll_once(dry_run: bool = False) -> dict[str, int]:
    init_db()
    feeds = configured_feeds()
    max_items = int(CONFIG["poll"].get("max_items_per_feed", 20))
    skip_existing = bool(CONFIG["poll"].get("skip_existing_on_first_run", True))
    totals = {"feeds": len(feeds), "items": 0, "pushed": 0, "skipped": 0, "duplicate": 0, "baseline": 0, "errors": 0}
    if not feeds:
        print("No feeds configured. Add config.feeds or enable poll.auto_from_accounts.", flush=True)
        return totals

    for feed in feeds:
        try:
            payloads = fetch_feed_payloads(feed)[:max_items]
        except Exception as exc:
            totals["errors"] += 1
            print(f"[{now_iso()}] poll error feed={feed['url']} error={exc}", flush=True)
            continue

        totals["items"] += len(payloads)
        baseline_feed = bool(skip_existing and not dry_run and payloads and not feed_has_seen_entries(feed["url"]))
        for payload in payloads:
            if dry_run:
                score, reasons = score_payload(payload)
                print(
                    f"[dry-run] score={score} feed={payload['feed'].get('title')} "
                    f"title={payload['entry'].get('title')} reasons={'; '.join(reasons)}",
                    flush=True,
                )
                continue
            if baseline_feed:
                item_id = entry_id(payload)
                if not already_seen(item_id):
                    save_entry(item_id, payload, 0, "baseline", "baseline_on_first_poll")
                    totals["baseline"] += 1
                    print(
                        f"[{now_iso()}] poll baseline feed={payload['feed'].get('title')} "
                        f"title={payload['entry'].get('title')}",
                        flush=True,
                    )
                else:
                    totals["duplicate"] += 1
                continue
            status, message = handle_payload(payload)
            if status >= 400:
                totals["errors"] += 1
            elif message == "pushed":
                totals["pushed"] += 1
            elif message == "duplicate":
                totals["duplicate"] += 1
            else:
                totals["skipped"] += 1
            print(
                f"[{now_iso()}] poll {message} feed={payload['feed'].get('title')} "
                f"title={payload['entry'].get('title')}",
                flush=True,
            )
    return totals


def run_poll_loop(dry_run: bool = False) -> None:
    interval = int(os.getenv("POLL_INTERVAL_SECONDS") or CONFIG["poll"].get("interval_seconds", 300))
    if interval < 30:
        raise ValueError("poll interval must be at least 30 seconds")
    print(f"Folo Telegram MVP polling every {interval}s", flush=True)
    while True:
        started = time.time()
        totals = run_poll_once(dry_run=dry_run)
        print(f"[{now_iso()}] poll cycle done {totals}", flush=True)
        elapsed = time.time() - started
        time.sleep(max(1, interval - elapsed))


class Handler(BaseHTTPRequestHandler):
    server_version = "FoloTelegramMVP/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))

    def do_GET(self) -> None:
        if self.path == "/health":
            self.respond(200, {"ok": True, "time": now_iso()})
            return
        self.respond(404, {"error": "not_found"})

    def do_POST(self) -> None:
        expected_path = CONFIG["server"].get("webhook_path", "/webhook")
        secret = os.getenv("WEBHOOK_SECRET") or CONFIG["server"].get("webhook_secret", "")
        expected_secret_path = f"{expected_path}/{secret}" if secret else expected_path

        if self.path != expected_secret_path:
            self.respond(404, {"error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            status, message = handle_payload(payload)
            self.respond(status, {"ok": True, "result": message})
        except json.JSONDecodeError:
            self.respond(400, {"ok": False, "error": "invalid_json"})
        except urllib.error.URLError as exc:
            self.respond(502, {"ok": False, "error": f"telegram_error:{exc}"})
        except Exception as exc:
            self.respond(500, {"ok": False, "error": str(exc)})

    def respond(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_server() -> None:
    init_db()
    host = str(CONFIG["server"].get("host", "0.0.0.0"))
    port = int(os.getenv("PORT") or CONFIG["server"].get("port", 8080))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Folo Telegram MVP listening on http://{host}:{port}", flush=True)
    httpd.serve_forever()


def send_test() -> None:
    init_db()
    sample = {
        "entry": {
            "id": f"test-{int(time.time())}",
            "guid": f"test-{int(time.time())}",
            "publishedAt": now_iso(),
            "insertedAt": now_iso(),
            "title": "AI agent benchmark and open source tooling",
            "description": "A short test entry about AI agents, benchmarks, and RSS workflows.",
            "author": "karpathy",
            "url": "https://x.com/karpathy/status/test",
            "media": None,
        },
        "feed": {
            "url": "https://rsshub.example.com/twitter/user/karpathy",
            "siteUrl": "https://x.com/karpathy",
            "title": "Twitter @karpathy",
            "checkedAt": now_iso(),
        },
        "view": 0,
    }
    status, message = handle_payload(sample)
    print(f"{status} {message}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "test", "poll", "poll-once"], nargs="?", default="serve")
    parser.add_argument("--dry-run", action="store_true", help="Fetch feeds and score entries without saving or sending.")
    args = parser.parse_args()
    if args.command == "test":
        send_test()
    elif args.command == "poll":
        run_poll_loop(dry_run=args.dry_run)
    elif args.command == "poll-once":
        totals = run_poll_once(dry_run=args.dry_run)
        print(json.dumps(totals, ensure_ascii=False))
    else:
        run_server()


if __name__ == "__main__":
    main()
