#!/usr/bin/env python3
"""Upsert today's close snapshot into daily_snapshots.json for the MO iron-eagle Pages site.

How the 5-minute Pages / quotes routine should commit this file
---------------------------------------------------------------
After running tools/update_quotes.py (which calls upsert_today() on Asia/Shanghai
weekdays during 15:00–15:20):

    git add daily_snapshots.json

whenever that file changed, and commit it. quotes.json must never be hand-edited
or hand-committed; only update_quotes.py writes it, and only during the session
(quotes.json is left unchanged after 15:05 — a late 15:05–15:20 fire may still
snapshot from the last in-window quotes). A same-day rerun is an idempotent
upsert of today's row only; earlier dates are left untouched.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
QUOTES = ROOT / "quotes.json"
OUT = ROOT / "daily_snapshots.json"
TZ = ZoneInfo("Asia/Shanghai")


def mark_of(opt: dict | None) -> float | None:
    """Same rule as the page: last if nonzero, else mid, else bid or ask."""
    if not opt:
        return None
    last = opt.get("last") or 0
    if last:
        return float(last)
    bid, ask = opt.get("bid") or 0, opt.get("ask") or 0
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    if bid:
        return float(bid)
    if ask:
        return float(ask)
    return None


def _extract_js_value(src: str, const_name: str) -> str:
    m = re.search(rf"const\s+{re.escape(const_name)}\s*=", src)
    if not m:
        raise ValueError(f"missing const {const_name} in {INDEX}")
    i = m.end()
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    start = i
    depth = 0
    in_str = None
    escape = False
    for j in range(start, len(src)):
        c = src[j]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == in_str:
                in_str = None
            continue
        if c in ('"', "'", "`"):
            in_str = c
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        elif c == ";" and depth == 0:
            return src[start:j].strip()
    raise ValueError(f"unterminated const {const_name} in {INDEX}")


def load_page_data(index_path: Path = INDEX) -> dict:
    """Parse POSITION and CLOSED JS constants from index.html via node."""
    src = index_path.read_text(encoding="utf-8")
    pos = _extract_js_value(src, "POSITION")
    closed = _extract_js_value(src, "CLOSED")
    script = (
        "const POSITION = "
        + pos
        + ";\nconst CLOSED = "
        + closed
        + ";\nconsole.log(JSON.stringify({POSITION, CLOSED}));\n"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("node failed to parse POSITION/CLOSED:\n" + (proc.stderr or proc.stdout))
    return json.loads(proc.stdout)


def realized_of(trade: dict, mult: float) -> int:
    sign = 1 if trade.get("side") == "short" else -1
    return int(round(sign * (trade["entry"] - trade["exit"]) * trade["qty"] * mult))


def realized_total(page: dict) -> int:
    pos = page["POSITION"]
    mult = pos.get("mult") or 100
    return sum(realized_of(t, mult) for t in page["CLOSED"])


def build_snapshot(page: dict, quotes: dict, today: str) -> dict:
    pos = page["POSITION"]
    mult = pos.get("mult") or 100
    chain = quotes.get("chain") or {}
    realized = realized_total(page)
    legs_out = []
    mtm = 0.0
    missing = []
    for leg in pos["legs"]:
        key = f"{leg['cp']}{leg['strike']}"
        mk = mark_of(chain.get(key))
        row = {
            "side": leg["side"],
            "cp": leg["cp"],
            "strike": leg["strike"],
            "qty": leg["qty"],
            "entry": leg["entry"],
        }
        if mk is None:
            missing.append(key)
        else:
            row["mark"] = mk
            if leg["side"] == "short":
                mtm += (leg["entry"] - mk) * leg["qty"] * mult
            else:
                mtm += (mk - leg["entry"]) * leg["qty"] * mult
        legs_out.append(row)
    if missing:
        raise RuntimeError("missing marks for legs: " + ", ".join(missing))
    mtm_i = int(round(mtm))
    as_of = quotes.get("asOf") or ""
    im = (quotes.get("im") or {}).get("last")
    return {
        "date": today,
        "basis": "收盘价",
        "source": f"新浪盘末最新价（quotes.json asOf {as_of}）" if as_of else "新浪盘末最新价",
        "asOf": as_of,
        "im": im,
        "cumPnL": int(realized + mtm_i),
        "realized": int(realized),
        "legs": legs_out,
    }


def load_snapshots(path: Path = OUT) -> list:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("snapshots"), list):
        return list(data["snapshots"])
    raise ValueError(f"unexpected snapshots shape in {path}")


def write_snapshots(rows: list, path: Path = OUT) -> None:
    rows = sorted((r for r in rows if r and r.get("date")), key=lambda r: r["date"])
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def quotes_asof_dt(quotes: dict) -> datetime | None:
    raw = (quotes.get("asOf") or "").replace(" Asia/Shanghai", "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError:
            return None


def in_snapshot_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 15 * 60 <= mins < 15 * 60 + 20


def upsert_today(
    *,
    now: datetime | None = None,
    force: bool = False,
    quotes_path: Path = QUOTES,
    index_path: Path = INDEX,
    out_path: Path = OUT,
) -> dict:
    now = now or datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if not force and not in_snapshot_window(now):
        raise SystemExit(
            f"skip snapshot: {now.strftime('%Y-%m-%d %H:%M:%S')} Asia/Shanghai "
            "is outside 15:00–15:20 weekday window (pass --force to override)"
        )
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    asof = quotes_asof_dt(quotes)
    if asof is None:
        raise RuntimeError("quotes.json missing a parseable asOf")
    asof_date = asof.strftime("%Y-%m-%d")
    if asof_date != today:
        raise RuntimeError(
            f"quotes.json asOf date {asof_date} is not today {today}; refusing to snapshot"
        )
    if asof.hour * 60 + asof.minute < 15 * 60:
        print(
            f"warning: quotes.json asOf {quotes.get('asOf')} is before 15:00; "
            "still upserting today's close from this file",
            file=sys.stderr,
        )
    page = load_page_data(index_path)
    snap = build_snapshot(page, quotes, today)
    rows = [r for r in load_snapshots(out_path) if r.get("date") != today]
    rows.append(snap)
    write_snapshots(rows, out_path)
    print(
        f"ok {out_path} date={today} cumPnL={snap['cumPnL']} "
        f"realized={snap['realized']} asOf={snap.get('asOf')} IM={snap.get('im')}"
    )
    return snap


def main() -> None:
    ap = argparse.ArgumentParser(description="Upsert today's MO close snapshot")
    ap.add_argument(
        "--force",
        action="store_true",
        help="upsert even outside the 15:00–15:20 Asia/Shanghai weekday window",
    )
    args = ap.parse_args()
    upsert_today(force=args.force)


if __name__ == "__main__":
    main()
