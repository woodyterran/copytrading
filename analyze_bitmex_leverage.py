import argparse
import os
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation

def parse_num(x):
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s == "":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None

def parse_time(x):
    if x is None:
        return None
    s = str(x).strip()
    if s == "":
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).isoformat(sep=" ", timespec="seconds")
        except Exception:
            pass
    return s

def read_records(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for d in r:
            et = (d.get("execType") or "").strip().lower()
            if et and et != "trade":
                continue
            qty = parse_num(d.get("qty"))
            price = parse_num(d.get("price"))
            foreign_notional = parse_num(d.get("foreignNotional"))
            notional = foreign_notional
            if notional is None and qty is not None and price is not None:
                notional = abs(qty) * price
            if notional is None:
                continue
            rows.append({
                "time": parse_time(d.get("timestamp")),
                "symbol": d.get("symbol") or "",
                "side": d.get("side") or "",
                "qty": qty,
                "price": price,
                "notional": notional
            })
    return rows

def fmt(x, n=4):
    if x is None:
        return ""
    try:
        return f"{x:.{n}f}"
    except Exception:
        return str(x)

def write_csv(path, rows):
    cols = ["time","symbol","side","notional","qty","price"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get("time") or "", r.get("symbol") or "", r.get("side") or "", fmt(r.get("notional")), fmt(r.get("qty")), fmt(r.get("price"))])

def print_rows(title, rows, topn):
    print(f"\n{title}（前{topn}）")
    print("时间 | 交易对 | 方向 | 名义规模(USD) | 数量 | 价格")
    for r in rows[:topn]:
        print(f"{r.get('time') or ''} | {r.get('symbol') or ''} | {r.get('side') or ''} | {fmt(r.get('notional'))} | {fmt(r.get('qty'))} | {fmt(r.get('price'))}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--top", type=int, default=100)
    args = ap.parse_args()
    records = read_records(args.csv_path)
    records.sort(key=lambda r: r["notional"], reverse=True)
    out_path = os.path.join(os.path.dirname(os.path.abspath(args.csv_path)), "bitmex_top100_by_notional.csv")
    write_csv(out_path, records[:args.top])
    print_rows("名义规模最大的交易（作为杠杆强度代理）", records, args.top)
    print(f"\n已导出: {out_path}")

if __name__ == "__main__":
    main()