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


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_price(F: float, K: float, T: float, vol: float, cp: str) -> float:
    """Futures/forward Black76 undiscounted (r≈0 for IV shape); MO priced vs IM."""
    if T <= 0 or vol <= 0 or F <= 0 or K <= 0:
        return max(0.0, (F - K) if cp == "C" else (K - F))
    s = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * vol * vol * T) / s
    d2 = d1 - s
    if cp == "C":
        return F * norm_cdf(d1) - K * norm_cdf(d2)
    return K * norm_cdf(-d2) - F * norm_cdf(-d1)

def implied_vol(F: float, K: float, T: float, price: float, cp: str) -> float | None:
    """Bisection IV; price in index points."""
    if price is None or price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    lo, hi = 1e-4, 3.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        model = bs_price(F, K, T, mid, cp)
        if model > price:
            hi = mid
        else:
            lo = mid
    iv = 0.5 * (lo + hi)
    if iv <= 1e-3 or iv >= 2.99:
        return None
    return iv

def calendar_years_to_expiry(exp_yyyy_mm_dd: str, now) -> float:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    exp = datetime.fromisoformat(exp_yyyy_mm_dd + "T15:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    secs = (exp - now).total_seconds()
    return max(secs / (365.0 * 24 * 3600), 1.0 / 365.0)

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
    # ATM IV: Black-76 style invert on ATM call & put mids, then average
    iv = None
    cp, pp = chain.get(f"C{atm}"), chain.get(f"P{atm}")
    now_for_t = datetime.now(TZ)
    T = calendar_years_to_expiry("2026-11-20", now_for_t)  # MO2611 第三周五
    if cp and pp and last:
        mid_c = (cp["bid"] + cp["ask"]) / 2 if cp["bid"] and cp["ask"] else cp["last"]
        mid_p = (pp["bid"] + pp["ask"]) / 2 if pp["bid"] and pp["ask"] else pp["last"]
        iv_c = implied_vol(last, float(atm), T, mid_c, "C")
        iv_p = implied_vol(last, float(atm), T, mid_p, "P")
        vals = [x for x in (iv_c, iv_p) if x is not None]
        if vals:
            iv = sum(vals) / len(vals)
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
