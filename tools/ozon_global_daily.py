"""Утренняя сводка по кабинету Ozon (FBS / realFBS / FBP) для менеджера маркетплейсов.

Что делает:
  1. Отправления, которые ждут сборки и отгрузки, и сколько часов осталось до срока.
  2. Остатки по SKU против продаж за последние N дней: на сколько дней хватит.
  3. Начисления за вчера по типам операций: продажи, комиссия, доставка, возвраты.

Источник данных: Ozon Seller API (https://api-seller.ozon.ru), заголовки Client-Id и Api-Key.
Пути методов взяты из документации на 27.10.2026:
  POST /v3/posting/fbs/list           список отправлений FBS / realFBS за период
  POST /v4/product/info/stocks        остатки по товарам
  POST /v3/finance/transaction/list   транзакции за период
С настоящим ключом кабинета скрипт ещё не запускался: у автора нет кабинета. Поэтому есть режим
--sample <папка> с JSON-ответами той же формы (tools/sample). При подключении к живому кабинету
формы ответов сверяются с документацией и правятся в одном месте: функции parse_*.

Запуск:
  python tools/ozon_global_daily.py --sample tools/sample --out digest.md
  OZON_CLIENT_ID=... OZON_API_KEY=... python tools/ozon_global_daily.py --out digest.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api-seller.ozon.ru"
MSK = timezone(timedelta(hours=3))
AWAITING = {"awaiting_packaging", "awaiting_deliver", "awaiting_registration"}
SOLD = {"delivering", "delivered", "awaiting_deliver", "awaiting_packaging"}


# ---------- источники ----------

class Live:
    def __init__(self, client_id: str, api_key: str):
        import requests  # локальный импорт: в sample-режиме requests не нужен
        self.s = requests.Session()
        self.s.headers.update({"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"})

    def post(self, path: str, body: dict) -> dict:
        r = self.s.post(API + path, json=body, timeout=30)
        if r.status_code != 200:
            raise SystemExit(f"{path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def postings(self, since: datetime, to: datetime) -> dict:
        return self.post("/v3/posting/fbs/list", {
            "dir": "ASC",
            "filter": {"since": since.isoformat(), "to": to.isoformat(), "status": ""},
            "limit": 1000, "offset": 0,
            "with": {"analytics_data": False, "financial_data": True},
        })

    def stocks(self) -> dict:
        return self.post("/v4/product/info/stocks", {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": ""})

    def transactions(self, day: datetime) -> dict:
        frm = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.post("/v3/finance/transaction/list", {
            "filter": {"date": {"from": frm.isoformat(), "to": (frm + timedelta(days=1)).isoformat()},
                       "operation_type": [], "posting_number": "", "transaction_type": "all"},
            "page": 1, "page_size": 1000,
        })


class Sample:
    def __init__(self, folder: Path):
        self.folder = folder

    def _load(self, name: str) -> dict:
        return json.loads((self.folder / name).read_text(encoding="utf-8"))

    def postings(self, since, to):
        return self._load("postings.json")

    def stocks(self):
        return self._load("stocks.json")

    def transactions(self, day):
        return self._load("transactions.json")


# ---------- разбор ответов (единственное место, которое правится под живой API) ----------

def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_postings(raw: dict) -> list[dict]:
    out = []
    for p in raw.get("result", {}).get("postings", []):
        out.append({
            "number": p.get("posting_number"),
            "status": p.get("status"),
            "created": parse_dt(p.get("in_process_at")),
            "ship_by": parse_dt(p.get("shipment_date")),
            "products": [{"sku": x.get("offer_id"), "name": x.get("name"), "qty": int(x.get("quantity", 0))}
                         for x in p.get("products", [])],
        })
    return out


def parse_stocks(raw: dict) -> dict[str, int]:
    free: dict[str, int] = {}
    for it in raw.get("items", []):
        total = 0
        for s in it.get("stocks", []):
            total += int(s.get("present", 0)) - int(s.get("reserved", 0))
        free[it.get("offer_id")] = total
    return free


def parse_transactions(raw: dict) -> list[dict]:
    out = []
    for op in raw.get("result", {}).get("operations", []):
        out.append({
            "type": op.get("operation_type_name") or op.get("operation_type"),
            "sale": float(op.get("accruals_for_sale", 0)),
            "commission": float(op.get("sale_commission", 0)),
            "delivery": float(op.get("delivery_charge", 0)),
            "return_delivery": float(op.get("return_delivery_charge", 0)),
            "amount": float(op.get("amount", 0)),
            "posting": (op.get("posting") or {}).get("posting_number"),
        })
    return out


# ---------- сводка ----------

def fmt_rub(v: float) -> str:
    s = f"{abs(v):,.0f}".replace(",", " ")
    return ("−" if v < 0 else "") + s + " ₽"


def build(now: datetime, postings: list[dict], free: dict[str, int], ops: list[dict], days: int, min_cover: int) -> tuple[list[str], list[str]]:
    lines, short = [], []
    title = f"Сводка за {now.strftime('%d.%m.%Y')} · кабинет"
    lines.append(f"# {title}\n")
    short.append(title)

    # 1. ждут отгрузки
    waiting = [p for p in postings if p["status"] in AWAITING]
    waiting.sort(key=lambda p: p["ship_by"] or now)
    lines.append(f"## Ждут отгрузки: {len(waiting)}\n")
    if waiting:
        lines.append("| Отправление | Статус | Срок отгрузки | Осталось | Товары |")
        lines.append("|---|---|---|---|---|")
        for p in waiting:
            left = (p["ship_by"] - now).total_seconds() / 3600 if p["ship_by"] else None
            left_s = "—" if left is None else (f"просрочка {abs(left):.0f} ч" if left < 0 else f"{left:.0f} ч")
            goods = "; ".join(f"{x['sku']} ×{x['qty']}" for x in p["products"])
            lines.append(f"| {p['number']} | {p['status']} | {p['ship_by'].astimezone(MSK).strftime('%d.%m %H:%M') if p['ship_by'] else '—'} | {left_s} | {goods} |")
        first = waiting[0]
        left = (first["ship_by"] - now).total_seconds() / 3600 if first["ship_by"] else None
        short.append(f"Ждут отгрузки: {len(waiting)} отправления, ближайший срок через {left:.0f} ч ({first['number']})" if left is not None and left >= 0
                     else f"Ждут отгрузки: {len(waiting)}, есть просрочка: {first['number']}")
    else:
        short.append("Ждут отгрузки: нет")
    lines.append("")

    # 2. остатки против продаж
    sales: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    for p in postings:
        if p["status"] in SOLD and p["created"] and p["created"] >= now - timedelta(days=days):
            for x in p["products"]:
                sales[x["sku"]] += x["qty"]
                names[x["sku"]] = x["name"] or ""
    rows = []
    for sku, stock in free.items():
        per_day = sales.get(sku, 0) / days
        cover = stock / per_day if per_day else None
        rows.append((sku, stock, sales.get(sku, 0), cover))
    rows.sort(key=lambda r: (r[3] is None, r[3] if r[3] is not None else 0))
    low = [r for r in rows if r[3] is not None and r[3] < min_cover]
    lines.append(f"## Остатки: {len(low)} SKU заканчиваются (запас меньше {min_cover} дней)\n")
    lines.append(f"| SKU | Свободно | Продано за {days} дн. | Хватит на |")
    lines.append("|---|---|---|---|")
    for sku, stock, sold, cover in rows:
        cover_s = "нет продаж" if cover is None else f"{cover:.0f} дн." + (" ⚠" if cover < min_cover else "")
        lines.append(f"| {sku} {names.get(sku, '')} | {stock} | {sold} | {cover_s} |")
    if low:
        s0 = low[0]
        short.append(f"Заканчиваются: {len(low)} SKU, {s0[0]} хватит на {s0[3]:.0f} дн. при текущих продажах")
    else:
        short.append("Заканчиваются: нет")
    lines.append("")

    # 3. начисления за вчера
    by_type: dict[str, float] = defaultdict(float)
    sale_n = 0
    tot = {"sale": 0.0, "commission": 0.0, "delivery": 0.0, "return_delivery": 0.0}
    for op in ops:
        by_type[op["type"]] += op["amount"]
        for k in tot:
            tot[k] += op[k]
        if op["sale"] > 0:
            sale_n += 1
    tot["commission"] = sum(op["commission"] for op in ops if op["sale"] > 0)
    tot["delivery"] = sum(op["delivery"] for op in ops if op["sale"] > 0)
    yday = (now - timedelta(days=1)).strftime("%d.%m")
    lines.append(f"## Начисления за {yday}\n")
    lines.append("| Операция | Сумма |")
    lines.append("|---|---|")
    for t, v in sorted(by_type.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"| {t} | {fmt_rub(v)} |")
    lines.append(f"| **Итого** | **{fmt_rub(sum(by_type.values()))}** |")
    returns = sum(op["amount"] for op in ops if op["sale"] < 0 or op["return_delivery"] != 0)
    short.append(f"Начисления за вчера: продажи {sale_n} заказов, комиссия {fmt_rub(tot['commission'])}, "
                 f"доставка {fmt_rub(tot['delivery'])}, возвраты {fmt_rub(returns)}")
    return lines, short


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sample", help="папка с JSON-ответами API вместо живого кабинета")
    ap.add_argument("--date", help="дата сводки YYYY-MM-DD (по умолчанию сегодня, в sample-режиме 2026-10-27)")
    ap.add_argument("--days", type=int, default=7, help="окно продаж для расчёта запаса, дней")
    ap.add_argument("--min-cover", type=int, default=7, help="порог «заканчивается», дней")
    ap.add_argument("--out", default="digest.md")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if a.sample:
        src = Sample(Path(a.sample))
        now = datetime.fromisoformat((a.date or "2026-10-27") + "T08:30:00+03:00")
        tag = "(sample)"
    else:
        cid, key = os.environ.get("OZON_CLIENT_ID"), os.environ.get("OZON_API_KEY")
        if not cid or not key:
            ap.error("нужны OZON_CLIENT_ID и OZON_API_KEY в окружении или --sample")
        src = Live(cid, key)
        now = datetime.fromisoformat(a.date + "T08:30:00+03:00") if a.date else datetime.now(MSK)
        tag = ""

    postings = parse_postings(src.postings(now - timedelta(days=a.days), now + timedelta(days=1)))
    free = parse_stocks(src.stocks())
    ops = parse_transactions(src.transactions(now - timedelta(days=1)))
    lines, short = build(now, postings, free, ops, a.days, a.min_cover)
    Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    short[0] += f" {tag}".rstrip()
    print("\n".join(short))
    print(f"записано {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
