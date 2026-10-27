"""Сбор публичных данных о Telegram-каналах и чатах с t.me (без API и без логина).

Для каждого username берём две публичные страницы:
  https://t.me/<u>    — точное число подписчиков/участников, описание
  https://t.me/s/<u>  — последние посты (дата, текст, просмотры); у чатов этой страницы нет

Темп: ~1 запрос/с с джиттером, браузерный User-Agent, ошибки не повторяем.

    python tools/collect_tg.py tools/channels.txt sellehome/channels.json
"""
import html
import json
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

sys.stdout.reconfigure(encoding="utf-8")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
S = requests.Session()
S.headers["User-Agent"] = UA


def get(url):
    time.sleep(1.0 + random.random() * 0.6)
    r = S.get(url, timeout=25)
    r.raise_for_status()
    return r.text


def strip(s):
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return scrub(html.unescape(s).strip())


def scrub(s):
    """Телефоны в публичных описаниях не сохраняем: в выгрузке остаются только @username и e-mail для рекламы."""
    return re.sub(r"(?:\+7|\b8)[\s()-]*\d{3}[\s()-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b", "[телефон]", s)


def num(s):
    """'55 784' / '55.8K' / '1.2M' -> int"""
    s = s.replace("\xa0", " ").replace(" ", "").upper()
    m = re.match(r"([\d.]+)([KM]?)", s)
    if not m:
        return None
    v = float(m.group(1))
    return int(v * {"": 1, "K": 1000, "M": 1_000_000}[m.group(2)])


def parse_card(page):
    """t.me/<u>: заголовок, счётчик, описание, тип (канал/чат)."""
    title = re.search(r'tgme_page_title"[^>]*>\s*<span dir="auto">(.*?)</span>', page, re.S)
    extra = re.search(r'tgme_page_extra">([^<]*)', page)
    desc = re.search(r'tgme_page_description"[^>]*>(.*?)</div>', page, re.S)
    extra_txt = html.unescape(extra.group(1)).strip() if extra else ""
    kind = "chat" if "member" in extra_txt else "channel"
    cnt = re.search(r"([\d\s.,KM]+)\s*(subscribers|members)", extra_txt)
    return {
        "title": strip(title.group(1)) if title else None,
        "count": num(cnt.group(1)) if cnt else None,
        "kind": kind,
        "online": (num(m.group(1)) if (m := re.search(r"([\d\s]+) online", extra_txt)) else None),
        "description": strip(desc.group(1))[:400] if desc else "",
    }


def parse_posts(page):
    posts = []
    for block in re.split(r'class="tgme_widget_message_wrap', page)[1:]:
        d = re.search(r'<time datetime="([^"]+)"', block)
        t = re.search(r'tgme_widget_message_text js-message_text" dir="auto">(.*?)</div>', block, re.S)
        v = re.search(r'tgme_widget_message_views">([^<]*)', block)
        link = re.search(r'tgme_widget_message_date" href="([^"]+)"', block)
        if not d:
            continue
        posts.append({
            "date": d.group(1),
            "text": strip(t.group(1))[:300] if t else "",
            "views": num(v.group(1)) if v else None,
            "url": link.group(1) if link else None,
        })
    return posts


def collect(username):
    row = {"username": username, "url": f"https://t.me/{username}", "collected_at": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    try:
        row.update(parse_card(get(f"https://t.me/{username}")))
    except Exception as e:  # noqa: BLE001
        row["error"] = f"card: {e}"
        return row
    if row["kind"] == "channel":
        try:
            posts = parse_posts(get(f"https://t.me/s/{username}"))
        except Exception as e:  # noqa: BLE001
            posts, row["error"] = [], f"posts: {e}"
        posts = [p for p in posts if p["date"]]
        row["posts"] = posts[-5:]
        views = [p["views"] for p in posts if p["views"]]
        row["avg_views"] = int(sum(views) / len(views)) if views else None
        if len(posts) >= 2:
            first = datetime.fromisoformat(posts[0]["date"])
            last = datetime.fromisoformat(posts[-1]["date"])
            days = max((last - first).total_seconds() / 86400, 1)
            row["posts_per_week"] = round(len(posts) / days * 7, 1)
            row["last_post"] = posts[-1]["date"]
        if row.get("count") and row.get("avg_views"):
            row["err"] = round(row["avg_views"] / row["count"] * 100, 1)  # охват поста, % от подписчиков
    return row


def main(src, dst):
    names = [l.strip().lstrip("@") for l in open(src, encoding="utf-8") if l.strip() and not l.startswith("#")]
    out, errors = [], 0
    for i, u in enumerate(names, 1):
        row = collect(u)
        out.append(row)
        status = row.get("error") or f"{row['kind']} {row.get('count')} avg_views={row.get('avg_views')}"
        print(f"[{i}/{len(names)}] {u}: {status}", flush=True)
        errors = errors + 1 if row.get("error") else 0
        if errors >= 3:
            print("три ошибки подряд — стоп", flush=True)
            break
        if i % 40 == 0:
            time.sleep(20)
    json.dump({"collected_at": datetime.now(timezone.utc).isoformat(timespec="minutes"), "items": out},
              open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"записано {len(out)} → {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
