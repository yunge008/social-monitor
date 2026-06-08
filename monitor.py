"""
Social Media Monitor
Monitors X and Truth Social for new posts and pushes to Feishu.
No browser required — uses RSS feeds and public APIs.

Dependencies: pip install requests deep-translator
"""

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests

BEIJING = timezone(timedelta(hours=8))

# ── Config ────────────────────────────────────────────────────────────────────

FEISHU_WEBHOOK = os.environ.get(
    "FEISHU_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/62447cfa-ca4c-4fb6-b037-5e04108f2932",
)

SEEN_FILE = "seen_ids.json"

X_ACCOUNTS = ["aleabitoreddit", "elonmusk"]

TRUTH_SOCIAL_ACCOUNT_ID = "107780257626128497"  # @realDonaldTrump

# RSSHub / Nitter instances to try for X (tried in order, first success wins)
X_RSS_SOURCES = [
    "https://rsshub.app/twitter/user/{username}",
    "https://nitter.net/{username}/rss",
    "https://nitter.privacydev.net/{username}/rss",
    "https://nitter.poast.org/{username}/rss",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SocialMonitor/1.0)"}

# ── Helpers ───────────────────────────────────────────────────────────────────


def clean_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def format_time(dt: datetime) -> str:
    """Return a formatted time string with both local and Beijing time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    beijing_dt = dt.astimezone(BEIJING)
    local_str = dt.strftime("%Y-%m-%d %H:%M %Z")
    beijing_str = beijing_dt.strftime("%Y-%m-%d %H:%M 北京时间")
    if dt.utcoffset() == timedelta(hours=8):
        return beijing_str  # already Beijing, no need to show twice
    return f"{local_str}  /  {beijing_str}"


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    # Keep only the most recent 500 IDs to avoid unbounded growth
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen)[-500:], f, indent=2)


# ── Fetchers ──────────────────────────────────────────────────────────────────


def fetch_x_posts(username: str) -> list[dict]:
    """Fetch latest posts for an X account via RSS (RSSHub or Nitter)."""
    for url_tmpl in X_RSS_SOURCES:
        url = url_tmpl.format(username=username)
        try:
            r = requests.get(url, timeout=15, headers=HEADERS)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            posts = []
            for item in root.findall(".//item")[:5]:
                link = item.findtext("link", "")
                # Only keep original posts (skip retweets / likes from others)
                if f"/{username}/status/" not in link.lower():
                    continue
                desc = item.findtext("description", "")
                title = item.findtext("title", "")
                text = clean_html(desc or title)[:500]
                post_id = link.split("/status/")[-1].split("?")[0]
                pub_date = item.findtext("pubDate", "")
                try:
                    dt = parsedate_to_datetime(pub_date) if pub_date else None
                    time_str = format_time(dt) if dt else ""
                except Exception:
                    time_str = ""
                if text and len(text) > 5:
                    posts.append(
                        {"id": post_id, "text": text, "link": link,
                         "username": username, "source": "X", "time_str": time_str}
                    )
            if posts:
                print(f"  [@{username}] Got {len(posts)} posts via {url}")
                return posts
        except Exception as e:
            print(f"  [@{username}] Source {url} failed: {e}")

    print(f"  [@{username}] All RSS sources failed")
    return []


def fetch_truth_social_posts() -> list[dict]:
    """Fetch latest posts from Trump's Truth Social via Mastodon-compatible API."""
    url = f"https://truthsocial.com/api/v1/accounts/{TRUTH_SOCIAL_ACCOUNT_ID}/statuses?limit=5"
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        posts = []
        for s in r.json()[:5]:
            text = clean_html(s.get("content", ""))
            # Skip empty posts and bare retweets
            if not text or len(text) < 5 or text.startswith("RT @"):
                continue
            try:
                dt = datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
                time_str = format_time(dt)
            except Exception:
                time_str = ""
            posts.append(
                {"id": s["id"], "text": text[:500],
                 "link": s.get("url", ""),
                 "username": "realDonaldTrump", "source": "TruthSocial",
                 "time_str": time_str}
            )
        print(f"  [@realDonaldTrump] Got {len(posts)} posts via API")
        return posts
    except Exception as e:
        print(f"  [@realDonaldTrump] API fetch failed: {e}")
        return []


# ── Translation ───────────────────────────────────────────────────────────────


def translate_to_chinese(text: str) -> str:
    """Translate text to Chinese using Google Translate (no API key required)."""
    try:
        from deep_translator import GoogleTranslator
        # Google Translate has a ~5000 char limit per request
        return GoogleTranslator(source="auto", target="zh-CN").translate(text[:4500])
    except Exception as e:
        print(f"  Translation failed: {e}")
        return "（翻译失败，请查看原文）"


# ── Feishu ────────────────────────────────────────────────────────────────────


def push_to_feishu(post: dict, translation: str) -> bool:
    source_label = "X" if post["source"] == "X" else "Truth Social"
    title = f"【{source_label} · @{post['username']}】新动态"
    time_line = f"🕐 发帖时间：{post['time_str']}\n\n" if post.get("time_str") else ""
    body = (
        f"{time_line}"
        f"📄 原文\n\n{post['text']}"
        f"\n\n---\n\n"
        f"🇨🇳 中文译文\n\n{translation}"
    )

    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [[
                        {"tag": "text", "text": body},
                        {"tag": "a", "text": "\n\n🔗 查看原文", "href": post["link"]},
                    ]],
                }
            }
        },
    }

    try:
        r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        result = r.json()
        ok = result.get("code") == 0
        print(f"  Feishu {'OK' if ok else 'FAILED'}: {result.get('msg', '')}")
        return ok
    except Exception as e:
        print(f"  Feishu push error: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    seen = load_seen()
    print(f"Loaded {len(seen)} seen IDs\n")

    all_posts: list[dict] = []

    for username in X_ACCOUNTS:
        print(f"Fetching X/@{username}...")
        all_posts.extend(fetch_x_posts(username))

    print("Fetching Truth Social/@realDonaldTrump...")
    all_posts.extend(fetch_truth_social_posts())

    new_posts = [p for p in all_posts if p["id"] not in seen]
    print(f"\nFound {len(new_posts)} new post(s) out of {len(all_posts)} fetched\n")

    pushed = 0
    for post in new_posts:
        preview = post["text"][:60].replace("\n", " ")
        print(f"Processing: @{post['username']} — {preview}...")
        translation = translate_to_chinese(post["text"])
        success = push_to_feishu(post, translation)
        if success:
            seen.add(post["id"])
            pushed += 1
        time.sleep(2)  # rate-limit between pushes

    save_seen(seen)
    print(f"\nDone. Pushed {pushed} new post(s).")


if __name__ == "__main__":
    main()
