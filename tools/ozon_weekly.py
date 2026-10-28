"""Недельный отчёт по карточкам Ozon с выводами: что просело, что срочно, что масштабировать.

Что делает:
  1. Берёт воронку по SKU за две недели (эта и прошлая): показы, визиты, в корзину, заказы.
  2. Добавляет остаток в днях (остаток / средние заказы в день) и индекс цен.
  3. Прогоняет каждую карточку через правила (те же, что на странице /ozon-manager) и пишет
     Markdown-отчёт: СРОЧНО / НА НЕДЕЛЕ / МАСШТАБИРОВАТЬ / в норме.

Источники данных (на выбор):
  --sample <папка>   JSON-ответы той же формы, что Seller API (tools/sample_rf). Для показа без ключа.
  --csv <файл>       выгрузка из «Аналитика → Товары» или из Google Таблиц (разделитель ; , или таб),
                     колонки: карточка, показы, показы_прошлая, визиты, визиты_прошлая, корзина,
                     корзина_прошлая, заказы, заказы_прошлая, цена, индекс, запас_дн, дрр, рейтинг.
  переменные OZON_CLIENT_ID и OZON_API_KEY   живой кабинет через Seller API (https://api-seller.ozon.ru):
     POST /v1/analytics/data          воронка по SKU за период (нужен доступ кабинета к аналитике по API)
     POST /v4/product/info/stocks     остатки
     POST /v5/product/info/prices     цена и индекс цен
  С живым кабинетом скрипт не запускался (у автора нет ключа): формы ответов взяты из документации
  на 28.10.2026 и при подключении сверяются и правятся в одном месте, функции parse_*.

Запуск:
  python tools/ozon_weekly.py --sample tools/sample_rf --out weekly.md
  python tools/ozon_weekly.py --csv export.csv --out weekly.md
  OZON_CLIENT_ID=... OZON_API_KEY=... python tools/ozon_weekly.py --out weekly.md
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

API = "https://api-seller.ozon.ru"
METRICS = ["hits_view", "session_view", "hits_tocart", "ordered_units"]
INDEX_RU = {"PROFIT": "выгодный", "AVG_PROFIT": "умеренный", "NON_PROFIT": "невыгодный", "WITHOUT_INDEX": "—"}


@dataclass
class Row:
    name: str
    imp: float = 0
    imp0: float = 0
    vis: float = 0
    vis0: float = 0
    cart: float = 0
    cart0: float = 0
    ord: float = 0
    ord0: float = 0
    price: float = 0
    idx: str = "—"
    stock: float = 30
    drr: float = 0
    rating: float = 5
    findings: list = field(default_factory=list)


# ---------- источники ----------

class Live:
    def __init__(self, client_id: str, api_key: str):
        import requests  # локальный импорт: в sample/csv-режиме requests не нужен
        self.s = requests.Session()
        self.s.headers.update({"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"})

    def post(self, path: str, body: dict) -> dict:
        r = self.s.post(API + path, json=body, timeout=30)
        if r.status_code != 200:
            raise SystemExit(f"{path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def analytics(self, d_from: date, d_to: date) -> dict:
        return self.post("/v1/analytics/data", {
            "date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
            "metrics": METRICS, "dimension": ["sku"], "filters": [], "sort": [], "limit": 1000, "offset": 0,
        })

    def stocks(self) -> dict:
        return self.post("/v4/product/info/stocks", {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": ""})

    def prices(self) -> dict:
        return self.post("/v5/product/info/prices", {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": ""})


class Sample:
    def __init__(self, folder: Path):
        self.folder = folder

    def _load(self, name: str) -> dict:
        return json.loads((self.folder / name).read_text(encoding="utf-8"))

    def analytics(self, d_from: date, d_to: date) -> dict:
        return self._load("analytics_cur.json" if d_to >= date.today() - timedelta(days=8) else "analytics_prev.json")

    def stocks(self) -> dict:
        return self._load("stocks.json")

    def prices(self) -> dict:
        return self._load("prices.json")


# ---------- разбор ответов ----------

def parse_analytics(resp: dict) -> dict[str, dict]:
    """sku -> {name, hits_view, session_view, hits_tocart, ordered_units}"""
    out = {}
    for item in resp.get("result", {}).get("data", []):
        dim = item["dimensions"][0]
        m = dict(zip(METRICS, item["metrics"]))
        m["name"] = dim.get("name") or dim["id"]
        out[str(dim["id"])] = m
    return out


def parse_stocks(resp: dict) -> dict[str, float]:
    """sku -> остаток (present - reserved) по всем складам"""
    out = {}
    for it in resp.get("items", []):
        total = sum(s.get("present", 0) - s.get("reserved", 0) for s in it.get("stocks", []))
        for key in (it.get("sku"), it.get("product_id"), it.get("offer_id")):
            if key is not None:
                out[str(key)] = total
    return out


def parse_prices(resp: dict) -> dict[str, tuple[float, str]]:
    """sku -> (цена, индекс цен по-русски)"""
    out = {}
    for it in resp.get("items", []):
        price = float(it.get("price", {}).get("price", 0) or 0)
        idx = INDEX_RU.get(it.get("price_indexes", {}).get("color_index", "WITHOUT_INDEX"), "—")
        for key in (it.get("sku"), it.get("product_id"), it.get("offer_id")):
            if key is not None:
                out[str(key)] = (price, idx)
    return out


def rows_from_api(src, week_end: date) -> list[Row]:
    cur = parse_analytics(src.analytics(week_end - timedelta(days=6), week_end))
    prev = parse_analytics(src.analytics(week_end - timedelta(days=13), week_end - timedelta(days=7)))
    stocks = parse_stocks(src.stocks())
    prices = parse_prices(src.prices())
    rows = []
    for sku, m in cur.items():
        p = prev.get(sku, {})
        r = Row(name=m["name"], imp=m["hits_view"], imp0=p.get("hits_view", 0), vis=m["session_view"],
                vis0=p.get("session_view", 0), cart=m["hits_tocart"], cart0=p.get("hits_tocart", 0),
                ord=m["ordered_units"], ord0=p.get("ordered_units", 0))
        r.price, r.idx = prices.get(sku, (0, "—"))
        per_day = max(r.ord, 1) / 7
        r.stock = round(stocks.get(sku, 0) / per_day) if sku in stocks else 30
        rows.append(r)
    return rows


COLS = {
    "name": ["карточка", "товар", "название", "name", "sku"],
    "imp": ["показы", "hits_view"], "imp0": ["показы_прошлая", "показы прошлая"],
    "vis": ["визиты", "посетители", "session_view"], "vis0": ["визиты_прошлая", "визиты прошлая"],
    "cart": ["корзина", "в корзину", "hits_tocart"], "cart0": ["корзина_прошлая", "корзина прошлая"],
    "ord": ["заказы", "ordered_units"], "ord0": ["заказы_прошлая", "заказы прошлая"],
    "price": ["цена", "price"], "idx": ["индекс", "индекс цен"], "stock": ["запас_дн", "запас", "остаток_дн"],
    "drr": ["дрр", "drr"], "rating": ["рейтинг", "rating"],
}


def num(s: str) -> float:
    try:
        return float(str(s).replace(" ", "").replace(" ", "").replace("%", "").replace(",", "."))
    except ValueError:
        return 0.0


def rows_from_csv(path: Path) -> list[Row]:
    text = path.read_text(encoding="utf-8-sig")
    first = text.splitlines()[0]
    sep = "\t" if "\t" in first else (";" if ";" in first else ",")
    rdr = csv.reader(text.splitlines(), delimiter=sep)
    head = [h.strip().lower() for h in next(rdr)]
    pos = {}
    for key, aliases in COLS.items():
        for a in aliases:
            if a in head:
                pos[key] = head.index(a)
                break
    if "name" not in pos or "imp" not in pos or "ord" not in pos:
        raise SystemExit(f"не нашёл колонки карточка/показы/заказы, есть: {', '.join(head)}")
    rows = []
    for rec in rdr:
        if not rec or not rec[pos["name"]].strip():
            continue
        g = lambda k, d=0: rec[pos[k]] if k in pos and pos[k] < len(rec) else d  # noqa: E731
        rows.append(Row(name=g("name").strip(), imp=num(g("imp")), imp0=num(g("imp0")), vis=num(g("vis")),
                        vis0=num(g("vis0")), cart=num(g("cart")), cart0=num(g("cart0")), ord=num(g("ord")),
                        ord0=num(g("ord0")), price=num(g("price")), idx=(g("idx", "—") or "—").strip().lower(),
                        stock=num(g("stock", 30)), drr=num(g("drr")), rating=num(g("rating", 5))))
    return rows


# ---------- правила ----------

def delta(a: float, b: float):
    return None if not b else (a - b) / b


def pct(v: float) -> str:
    return f"{v * 100:.1f} %".replace(".", ",")


def dtxt(d) -> str:
    return "—" if d is None else f"{d * 100:+.0f} %"


def analyze(r: Row, drr_hi: float = 15, drr_lo: float = 7) -> str:
    ctr, ctr0 = (r.vis / r.imp if r.imp else 0), (r.vis0 / r.imp0 if r.imp0 else 0)
    cc, cc0 = (r.cart / r.vis if r.vis else 0), (r.cart0 / r.vis0 if r.vis0 else 0)
    oc, oc0 = (r.ord / r.cart if r.cart else 0), (r.ord0 / r.cart0 if r.cart0 else 0)
    d_imp, d_ctr, d_cc, d_oc, d_ord = delta(r.imp, r.imp0), delta(ctr, ctr0), delta(cc, cc0), delta(oc, oc0), delta(r.ord, r.ord0)
    f = r.findings
    add = lambda sev, what, act: f.append((sev, what, act))  # noqa: E731
    if r.stock == 0:
        add("urgent", f"остаток 0, показы {dtxt(d_imp)}", "заявка на поставку сегодня; до прихода снять карточку с рекламы")
    elif r.stock < 7:
        add("urgent", f"запаса {r.stock:.0f} дн при {r.ord:.0f} заказах/нед", "заявка на поставку сегодня")
    elif r.stock < 14:
        add("week", f"запаса {r.stock:.0f} дн", "запланировать поставку на этой неделе")
    imp_drop = r.stock > 0 and d_imp is not None and d_imp <= -0.2
    if imp_drop:
        add("week", f"показы {dtxt(d_imp)}" + (", индекс «невыгодный»" if r.idx == "невыгодный" else ""),
            ("цена до порога индекса; " if r.idx == "невыгодный" else "") + "проверить ставку продвижения и позиции")
    if d_ctr is not None and d_ctr <= -0.15 and r.imp > 2000:
        add("week", f"CTR {pct(ctr)} против {pct(ctr0)}", "тест обложки, сверить цену с топ-5 выдачи")
    cc_drop = d_cc is not None and d_cc <= -0.15 and r.vis > 300
    if cc_drop:
        add("week", f"в корзину {pct(cc)} против {pct(cc0)}" + (f", рейтинг {r.rating}" if r.rating < 4.5 else ""),
            "цена внутри карточки, " + ("отзывы по причинам, " if r.rating < 4.5 else "") + "характеристики, рич-контент")
    if d_oc is not None and d_oc <= -0.15 and r.cart > 50:
        add("week", f"корзина → заказ {pct(oc)} против {pct(oc0)}", "цена и срок доставки, наличие на ближайшем складе")
    if r.drr > drr_hi:
        add("week", f"ДРР {r.drr:.1f} %", "снизить ставку, минус-слова; без роста заказов две недели — снять с рекламы")
    if r.idx == "невыгодный" and not imp_drop:
        add("week", "индекс цен «невыгодный»", "пересчитать цену до порога индекса, проверить маржу")
    if r.rating and r.rating < 4.5 and not cc_drop:
        add("watch", f"рейтинг {r.rating}", "разобрать последние 20 отзывов по причинам")
    if 0 < r.drr < drr_lo and d_ord is not None and d_ord >= 0.03 and r.stock >= 21:
        add("scale", f"заказы {dtxt(d_ord)}, ДРР {r.drr:.1f} %", "поднять ставку на 20 %, проверить через 3 дня")
    elif 0 < r.drr < drr_lo and d_ord is not None and d_ord > -0.03 and r.stock >= 21 and not f:
        add("scale", f"стабильно, ДРР {r.drr:.1f} %", "добавить в «Продвижение в поиске»")
    if not f:
        add("watch", "в норме", "без изменений")
    sev = [s for s, _, _ in f]
    return "urgent" if "urgent" in sev else "week" if "week" in sev else "scale" if "scale" in sev else "watch"


# ---------- отчёт ----------

TITLES = {"urgent": "СРОЧНО", "week": "НА НЕДЕЛЕ", "scale": "МАСШТАБИРОВАТЬ"}


def report(rows: list[Row], week_end: date, cabinet: str) -> str:
    tops = {r.name: analyze(r) for r in rows}
    s = lambda k: sum(getattr(r, k) for r in rows)  # noqa: E731
    ctr, ctr0 = s("vis") / max(s("imp"), 1), s("vis0") / max(s("imp0"), 1)
    cc, cc0 = s("cart") / max(s("vis"), 1), s("cart0") / max(s("vis0"), 1)
    d0, d1 = week_end - timedelta(days=6), week_end
    p0, p1 = d0 - timedelta(days=7), d1 - timedelta(days=7)
    lines = [f"# Неделя {d0:%d.%m}–{d1:%d.%m} против {p0:%d.%m}–{p1:%d.%m} · кабинет «{cabinet}» · {len(rows)} карточек",
             f"Заказы {s('ord'):.0f} ({dtxt(delta(s('ord'), s('ord0')))}) · CTR {pct(ctr)} ({dtxt(delta(ctr, ctr0))}) · "
             f"в корзину {pct(cc)} ({dtxt(delta(cc, cc0))})", ""]
    for sev in ("urgent", "week", "scale"):
        part = [f"  {r.name} — {what}: {act}" for r in rows for s_, what, act in r.findings if s_ == sev]
        if part:
            lines += [TITLES[sev], *part, ""]
    ok = [r.name for r in rows if tops[r.name] == "watch"]
    if ok:
        lines.append("В норме: " + ", ".join(ok))
    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=Path, help="папка с JSON-ответами той же формы, что Seller API")
    ap.add_argument("--csv", type=Path, help="выгрузка CSV/TSV из кабинета или Google Таблиц")
    ap.add_argument("--week-end", type=date.fromisoformat, default=None, help="последний день недели, по умолчанию вчера")
    ap.add_argument("--cabinet", default="Инструмент", help="название кабинета для заголовка")
    ap.add_argument("--out", type=Path, default=Path("weekly.md"))
    a = ap.parse_args()
    week_end = a.week_end or (date.today() - timedelta(days=1))

    if a.csv:
        rows = rows_from_csv(a.csv)
    elif a.sample:
        rows = rows_from_api(Sample(a.sample), week_end)
    elif os.environ.get("OZON_CLIENT_ID") and os.environ.get("OZON_API_KEY"):
        rows = rows_from_api(Live(os.environ["OZON_CLIENT_ID"], os.environ["OZON_API_KEY"]), week_end)
    else:
        raise SystemExit("нужен --sample, --csv или переменные OZON_CLIENT_ID и OZON_API_KEY")

    text = report(rows, week_end, a.cabinet)
    a.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\nЗаписано: {a.out}")


if __name__ == "__main__":
    main()
