#!/usr/bin/env python3
"""Fetch Sina quotes with finance.sina Referer and write quotes.json for GitHub Pages."""
from __future__ import annotations
import json, math, re, urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "quotes.json"
TZ = ZoneInfo("Asia/Shanghai")
BASE = [
    "s_sh000852", "s_sh000905",
    "nf_IM2611", "nf_IM2612", "nf_IM2703", "nf_IM2706",
    "nf_IC2611", "nf_IC2612", "nf_IC2703", "nf_IC2706",
]

def fetch(lst: str) -> str:
    req = urllib.request.Request(
        "https://hq.sinajs.cn/list=" + lst,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read().decode("gbk", errors="replace")

def parse_map(raw: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r'hq_str_([^=]+)="([^"]*)"', raw) if m.group(2)}

def parse_idx(s: str) -> dict:
    p = s.split(",")
    return {"name": p[0], "last": float(p[1] or 0), "chg": float(p[2] or 0), "pct": float(p[3] or 0)}

def parse_fut(s: str) -> dict:
    p = s.split(",")
    return {
        "open": float(p[0] or 0), "high": float(p[1] or 0), "low": float(p[2] or 0), "last": float(p[3] or 0),
        "settle": float(p[14] or 0) if len(p) > 14 else 0,
        "prev": float(p[14] or 0) if len(p) > 14 else 0,
        "bid": float(p[16] or 0) if len(p) > 16 else 0,
        "ask": float(p[26] or 0) if len(p) > 26 else 0,
        "time": ((p[36] if len(p) > 36 else "") + " " + (p[37] if len(p) > 37 else "")).strip(),
    }

def parse_opt(s: str) -> dict:
    p = s.split(",")
    return {
        "bvol": float(p[0] or 0), "bid": float(p[1] or 0), "last": float(p[2] or 0),
        "ask": float(p[3] or 0), "svol": float(p[4] or 0),
    }

def main() -> None:
    m1 = parse_map(fetch(",".join(BASE)))
    im = parse_fut(m1["nf_IM2611"]) if m1.get("nf_IM2611") else None
    idx = parse_idx(m1["s_sh000852"]) if m1.get("s_sh000852") else None
    last = (im and im["last"]) or (idx and idx["last"]) or 6500.0
    atm = int(round(last / 100.0) * 100)
    strikes = list(range(atm - 600, atm + 901, 100))
    opts = [f"P_OP_mo2611P{k}" for k in strikes] + [f"P_OP_mo2611C{k}" for k in strikes]
    chain: dict = {}
    for i in range(0, len(opts), 40):
        mm = parse_map(fetch(",".join(opts[i : i + 40])))
        for k in strikes:
            pk, ck = f"P_OP_mo2611P{k}", f"P_OP_mo2611C{k}"
            if pk in mm:
                chain[f"P{k}"] = parse_opt(mm[pk])
            if ck in mm:
                chain[f"C{k}"] = parse_opt(mm[ck])
    iv = None
    cp, pp = chain.get(f"C{atm}"), chain.get(f"P{atm}")
    if cp and pp and last:
        mid_c = (cp["bid"] + cp["ask"]) / 2 if cp["bid"] and cp["ask"] else cp["last"]
        mid_p = (pp["bid"] + pp["ask"]) / 2 if pp["bid"] and pp["ask"] else pp["last"]
        T = max(10 / 365.0, 1 / 365.0)
        iv = max(0.05, min(1.5, (mid_c + mid_p) / (0.8 * last * math.sqrt(T))))
    now = datetime.now(TZ)
    payload = {
        "asOf": now.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Shanghai",
        "source": "sina-via-bot",
        "idx": idx,
        "im": im,
        "atm": atm,
        "iv": round(iv, 4) if iv else None,
        "chain": chain,
        "basis": {
            "idx": {
                "s_sh000852": parse_idx(m1["s_sh000852"]) if m1.get("s_sh000852") else None,
                "s_sh000905": parse_idx(m1["s_sh000905"]) if m1.get("s_sh000905") else None,
            },
            "fut": {k: parse_fut(v) for k, v in m1.items() if k.startswith("nf_")},
            "time": (im or {}).get("time") or now.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"ok {OUT} asOf={payload['asOf']} IM={im and im['last']} ATM={atm} IV={payload['iv']} legs={len(chain)}")

if __name__ == "__main__":
    main()
