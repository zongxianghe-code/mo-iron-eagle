#!/usr/bin/env python3
"""Fetch Sina quotes with finance.sina Referer and write quotes.json for GitHub Pages.

On Asia/Shanghai weekdays, also calls snapshot_close.upsert_today() during
15:00–15:20. After 15:05 this script does not rewrite quotes.json; it only
upserts daily_snapshots.json from the last in-window quotes.
"""
from __future__ import annotations
import json, math, re, urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "quotes.json"
TZ = ZoneInfo("Asia/Shanghai")
BASE = [
    "s_sh000852", "s_sh000905",
    "nf_IM2611", "nf_IM2612", "nf_IM2703", "nf_IM2706",
    "nf_IC2611", "nf_IC2612", "nf_IC2703", "nf_IC2706",
    # months that appear in the Sina list once the older ones expire (missing codes are simply ignored)
    "nf_IM2701", "nf_IM2702", "nf_IC2701", "nf_IC2702",
]
IV_HIST_KEEP_HOURS = 72   # ≈ 2 trading days plus weekend slack
IV_HIST_MAX = 400
SWITCH_DAYS = 5           # near month with <= this many trading days left -> use next month

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


# ---------------------------------------------------------------- expiry / signal month
def third_friday(y: int, m: int) -> date:
    d = date(y, m, 15)  # third Friday is always in 15..21
    return d + timedelta(days=(4 - d.weekday()) % 7)


def month_code(y: int, m: int) -> str:
    return f"{y % 100:02d}{m:02d}"


def code_to_ym(code: str) -> tuple[int, int]:
    return 2000 + int(code[:2]), int(code[2:])


def exp_of(code: str) -> date:
    y, m = code_to_ym(code)
    return third_friday(y, m)


def next_month_code(code: str) -> str:
    y, m = code_to_ym(code)
    return month_code(y + (m == 12), 1 if m == 12 else m + 1)


def near_month_code(now: datetime) -> str:
    """First monthly MO contract whose expiry (3rd Friday 15:00) has not passed."""
    code = month_code(now.year, now.month)
    for _ in range(3):
        e = exp_of(code)
        if now < datetime(e.year, e.month, e.day, 15, 0, tzinfo=TZ):
            return code
        code = next_month_code(code)
    return code


def trading_days_left(exp: date, now: datetime) -> int:
    """Weekdays from today to expiry inclusive; today counts only before 15:00.
    APPROXIMATION: exchange holidays are NOT excluded (weekends only)."""
    d = now.date()
    if now.hour >= 15:
        d += timedelta(days=1)
    n = 0
    while d <= exp:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def pick_signal_month(now: datetime) -> tuple[str, str, int, bool]:
    """-> (near, signal, near_days_left, switched)"""
    near = near_month_code(now)
    days = trading_days_left(exp_of(near), now)
    if days <= SWITCH_DAYS:
        return near, next_month_code(near), days, True
    return near, near, days, False


def fetch_chain(code: str, atm: int) -> dict:
    strikes = list(range(atm - 600, atm + 901, 100))
    opts = [f"P_OP_mo{code}P{k}" for k in strikes] + [f"P_OP_mo{code}C{k}" for k in strikes]
    chain: dict = {}
    for i in range(0, len(opts), 40):
        mm = parse_map(fetch(",".join(opts[i : i + 40])))
        for k in strikes:
            pk, ck = f"P_OP_mo{code}P{k}", f"P_OP_mo{code}C{k}"
            if pk in mm:
                chain[f"P{k}"] = parse_opt(mm[pk])
            if ck in mm:
                chain[f"C{k}"] = parse_opt(mm[ck])
    return chain


def atm_iv(chain: dict, atm: int, F: float, exp: date, now: datetime) -> float | None:
    """ATM IV: Black-76 invert on ATM call & put mids (else last), averaged."""
    cp, pp = chain.get(f"C{atm}"), chain.get(f"P{atm}")
    if not (cp and pp and F):
        return None
    T = calendar_years_to_expiry(exp.isoformat(), now)
    mid_c = (cp["bid"] + cp["ask"]) / 2 if cp["bid"] and cp["ask"] else cp["last"]
    mid_p = (pp["bid"] + pp["ask"]) / 2 if pp["bid"] and pp["ask"] else pp["last"]
    vals = [x for x in (implied_vol(F, float(atm), T, mid_c, "C"), implied_vol(F, float(atm), T, mid_p, "P")) if x is not None]
    return sum(vals) / len(vals) if vals else None


def load_iv_hist(now: datetime) -> list:
    try:
        old = json.loads(OUT.read_text(encoding="utf-8")).get("ivHist") or []
    except Exception:
        return []
    cutoff = (now - timedelta(hours=IV_HIST_KEEP_HOURS)).strftime("%Y-%m-%d %H:%M")
    return [e for e in old if isinstance(e, dict) and e.get("t", "") >= cutoff and e.get("iv")]


def _mins(now: datetime) -> int:
    return now.hour * 60 + now.minute


def in_late_snapshot_window(now: datetime) -> bool:
    """Weekday 15:05–15:20: snapshot only, do not touch quotes.json."""
    return now.weekday() < 5 and (15 * 60 + 5) <= _mins(now) < (15 * 60 + 20)


def in_snapshot_window(now: datetime) -> bool:
    """Weekday 15:00–15:20, covering the last in-window quotes run and late fires."""
    return now.weekday() < 5 and (15 * 60) <= _mins(now) < (15 * 60 + 20)


def maybe_snapshot_close(now: datetime) -> None:
    if not in_snapshot_window(now):
        return
    # tools/ is on sys.path when this file is run as a script
    import snapshot_close
    snapshot_close.upsert_today(now=now, force=True)


def main() -> None:
    now = datetime.now(TZ)
    if in_late_snapshot_window(now):
        print(f"skip quotes write after 15:05 (now={now.strftime('%Y-%m-%d %H:%M:%S')} Asia/Shanghai); snapshot only")
        maybe_snapshot_close(now)
        return
    m1 = parse_map(fetch(",".join(BASE)))
    now0 = datetime.now(TZ)
    near, sig, near_days, switched = pick_signal_month(now0)
    # legacy fields (持仓页 / snapshot_close): near-month IM + MO chain. Until 2026-11-20 that is IM2611/MO2611
    # (held legs); afterwards they follow the new near month automatically.
    im = parse_fut(m1[f"nf_IM{near}"]) if m1.get(f"nf_IM{near}") else None
    idx = parse_idx(m1["s_sh000852"]) if m1.get("s_sh000852") else None
    last = (im and im["last"]) or (idx and idx["last"]) or 6500.0
    atm = int(round(last / 100.0) * 100)
    chain = fetch_chain(near, atm)
    iv = atm_iv(chain, atm, last, exp_of(near), now0)
    # signal block: next month when near month has <= SWITCH_DAYS trading days left
    if sig == near:
        s_im, s_atm, s_chain = im, atm, chain
    else:
        s_im = parse_fut(m1[f"nf_IM{sig}"]) if m1.get(f"nf_IM{sig}") else None
        if s_im is None:
            s_im = parse_fut(parse_map(fetch(f"nf_IM{sig}"))[f"nf_IM{sig}"])
        s_atm = int(round(s_im["last"] / 100.0) * 100)
        s_chain = fetch_chain(sig, s_atm)
    s_iv = atm_iv(s_chain, s_atm, s_im["last"], exp_of(sig), now0) if s_im else None
    iv_hist = load_iv_hist(now0)
    if s_iv:
        t = now0.strftime("%Y-%m-%d %H:%M")
        iv_hist = [e for e in iv_hist if e.get("t") != t]
        iv_hist.append({"t": t, "month": sig, "iv": round(s_iv, 4)})
    iv_hist = iv_hist[-IV_HIST_MAX:]
    now = datetime.now(TZ)
    payload = {
        "asOf": now.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Shanghai",
        "source": "sina-via-bot",
        "idx": idx,
        "im": im,
        "atm": atm,
        "iv": round(iv, 4) if iv else None,
        "chain": chain,
        "nearMonth": near,
        "nearExp": exp_of(near).isoformat(),
        "nearDaysLeft": near_days,
        "signalMonth": sig,
        "signalExp": exp_of(sig).isoformat(),
        "signalSwitched": switched,
        "signalDaysLeft": trading_days_left(exp_of(sig), now0),
        "signalF": s_im["last"] if s_im else None,
        "signalIm": s_im,
        "signalAtm": s_atm,
        "signalIv": round(s_iv, 4) if s_iv else None,
        "signalChain": s_chain,
        "ivHist": iv_hist,
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
    print(f"ok {OUT} asOf={payload['asOf']} IM={im and im['last']} ATM={atm} IV={payload['iv']} legs={len(chain)} | near={near}({near_days}d) signal={sig} F={payload['signalF']} ATM={s_atm} IV={payload['signalIv']} legs={len(s_chain)} hist={len(iv_hist)}")
    maybe_snapshot_close(datetime.now(TZ))

if __name__ == "__main__":
    main()
