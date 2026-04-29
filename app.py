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
            INSERT OR IGNORE INTO entries (
                id, feed_url, feed_title, title, url, published_at, inserted_at,
                score, status, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    blob = text_blob(payload)

    account = account_key(payload)
    if account:
        weight = int(CONFIG["accounts"].get(account, 0))
        score += weight
        reasons.append(f"account:{account}+{weight}")

    for keyword, weight in CONFIG["keywords"].items():
        if keyword.lower() in blob:
            score += int(weight)
            reasons.append(f"keyword:{keyword}+{weight}")

    for keyword, weight in CONFIG["negative_keywords"].items():
        if keyword.lower() in blob:
            score += int(weight)
            reasons.append(f"negative:{keyword}{weight}")

    published = (payload.get("entry") or {}).get("publishedAt")
    if published:
        try:
            published_dt = dt.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            hours_old = (dt.datetime.now(dt.timezone.utc) - published_dt).total_seconds() / 3600
            if hours_old <= 3:
                score += 2
                reasons.append("fresh+2")
        except ValueError:
            pass

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
    title = html.escape(strip_html(entry.get("title")) or "New entry")
    url = html.escape(str(entry.get("url") or ""))
    author = html.escape(str(entry.get("author") or ""))
    desc = strip_html(entry.get("description") or entry.get("content"))
    if len(desc) > 360:
        desc = desc[:357].rstrip() + "..."
    desc = html.escape(desc)

    show_debug = bool(CONFIG["telegram"].get("include_debug", False))
    lines = [f"<b>{feed_title}</b>"]
    if author:
        lines.append(f"<i>{author}</i>")
    lines.append("")
    lines.append(f"<b>{title}</b>")
    if desc:
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
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Telegram returned HTTP {resp.status}")


def handle_payload(payload: dict[str, Any]) -> tuple[int, str]:
    item_id = entry_id(payload)
    if already_seen(item_id):
        return 200, "duplicate"

    push, score, reason = should_push(payload)
    if not push:
        save_entry(item_id, payload, score, "skipped", reason)
        return 200, f"skipped:{reason}"

    message = telegram_message(payload, score, reason)
    send_telegram(message)
    save_entry(item_id, payload, score, "pushed", reason)
    return 200, "pushed"


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
    parser.add_argument("command", choices=["serve", "test"], nargs="?", default="serve")
    args = parser.parse_args()
    if args.command == "test":
        send_test()
    else:
        run_server()


if __name__ == "__main__":
    main()
