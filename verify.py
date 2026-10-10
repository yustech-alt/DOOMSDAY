"""
Check the channel's results against real Bybit prices.
Replays every signal in signals.json on 1 minute candles with the same rules as bot.py:
market entry after the post, equal split across targets, stop loss moves to entry after TP1.
If the stop loss and a target sit in the same candle, the stop loss counts first.

Run:  python verify.py [signals.json] [from_date YYYY-MM-DD]
Needs internet access to api.bybit.com (set BYBIT_HOST=https://api.bytick.com if blocked).
"""
import csv, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

HOST = os.getenv("BYBIT_HOST", "https://api.bybit.com")
DELAY_MIN = int(os.getenv("DELAY_MIN", "1"))        # minutes between post and entry
MAX_DAYS = int(os.getenv("MAX_DAYS", "7"))
MAX_CHASE_PCT = float(os.getenv("MAX_CHASE_PCT", "0.3"))
TAKER, MAKER = 0.00055, 0.0002


def fetch_bars(symbol, start_ms, end_ms):
    rows, cur_end = [], end_ms
    while True:
        q = urllib.parse.urlencode(dict(category="linear", symbol=symbol, interval="1",
                                        start=start_ms, end=cur_end, limit=1000))
        with urllib.request.urlopen(f"{HOST}/v5/market/kline?{q}", timeout=20) as r:
            data = json.load(r)
        if data.get("retCode") != 0:
            raise RuntimeError(data.get("retMsg"))
        batch = data["result"]["list"]
        if not batch:
            break
        rows += batch
        oldest = int(batch[-1][0])
        if oldest <= start_ms or len(batch) < 1000:
            break
        cur_end = oldest - 1
        time.sleep(0.15)
    bars = sorted({int(r[0]): (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows}.values())
    return bars


def simulate(sig, bars):
    """bars: ascending (t, o, h, l, c), first bar is the entry bar. Returns dict."""
    long = sig["side"] == "long"
    d = 1 if long else -1
    entry, sl, tps = bars[0][1], sig["sl"], sig["targets"]
    chase = d * (entry - sig["entry"]) / sig["entry"] * 100
    if chase > MAX_CHASE_PCT:
        return dict(outcome="SKIP chase", tp_hits=0, R=0.0, R_net=0.0, entry=entry)
    if d * (entry - sl) <= 0:
        return dict(outcome="SKIP past stop", tp_hits=0, R=0.0, R_net=0.0, entry=entry)
    risk = abs(entry - sl)
    n, w = len(tps), 1.0 / len(tps)
    stop, hits, rem, pnl, fee, label = sl, 0, 1.0, 0.0, TAKER, "OPEN"
    for t, o, h, l, c in bars:
        if d * (l - stop) <= 0 if long else d * (h - stop) <= 0:
            pnl += rem * d * (stop - entry)
            fee += rem * TAKER
            label, rem = ("SL" if hits == 0 else f"BE after TP{hits}"), 0.0
            break
        while hits < n and (h >= tps[hits] if long else l <= tps[hits]):
            pnl += w * d * (tps[hits] - entry)
            fee += w * MAKER
            rem -= w
            hits += 1
            if hits == 1:
                stop = entry
        if hits == n:
            label, rem = "ALL targets", 0.0
            break
    if rem > 1e-9:                                   # still open at the end of the window
        pnl += rem * d * (bars[-1][4] - entry)
        fee += rem * TAKER
        label = f"OPEN after TP{hits}" if hits else "OPEN"
    R = pnl / risk
    R_net = R - fee * entry / risk
    return dict(outcome=label, tp_hits=hits, R=R, R_net=R_net, entry=entry)


def main(path="signals.json", since="2000-01-01"):
    sigs = [s for s in json.load(open(path)) if s["valid"] and not s["duplicate"] and s["date"][:10] >= since]
    now_ms = int(time.time() * 1000)
    out = []
    for s in sigs:
        t0 = int(datetime.fromisoformat(s["date"]).timestamp() * 1000) + DELAY_MIN * 60000
        t1 = min(t0 + MAX_DAYS * 86400000, now_ms)
        try:
            bars = fetch_bars(s["symbol"], t0, t1)
            bars = [b for b in bars if b[0] >= t0]
            if not bars:
                raise RuntimeError("no candles")
            r = simulate(s, bars)
        except Exception as ex:
            r = dict(outcome=f"NO DATA ({ex})", tp_hits=0, R=0.0, R_net=0.0, entry=0)
        kinds = {c["kind"] for c in s.get("claims", [])}
        claimed = "win" if kinds & {"TP", "ALL_TP", "OTHER"} else "stop" if "SL" in kinds else "none"
        out.append({**{k: s[k] for k in ("id", "date", "symbol", "side")}, **r, "claimed": claimed})
        print(f"{s['date'][:16]} {s['symbol']:<10}{s['side']:<6}{r['outcome']:<18} TPs={r['tp_hits']} netR={r['R_net']:+.2f} claimed={claimed}", flush=True)

    ok = [o for o in out if not o["outcome"].startswith(("SKIP", "NO DATA"))]
    sl = [o for o in ok if o["outcome"] == "SL"]
    tp1 = [o for o in ok if o["tp_hits"] >= 1]
    print("\n--- summary ---")
    print(f"signals {len(out)}  simulated {len(ok)}  skipped {sum(o['outcome'].startswith('SKIP') for o in out)}  no data {sum(o['outcome'].startswith('NO DATA') for o in out)}")
    if ok:
        print(f"stopped out before TP1: {len(sl)}   reached TP1: {len(tp1)} ({len(tp1) / len(ok):.0%})")
        print(f"total R gross {sum(o['R'] for o in ok):+.1f}   net of fees {sum(o['R_net'] for o in ok):+.1f}   avg net R per trade {sum(o['R_net'] for o in ok) / len(ok):+.3f}")
    bad = [o for o in ok if o["claimed"] == "win" and o["tp_hits"] == 0]
    print(f"\nchannel claimed a win but price never reached TP1 first: {len(bad)}")
    for o in bad:
        print("  ", o["date"][:16], o["symbol"], o["side"], o["outcome"])
    silent = [o for o in ok if o["claimed"] == "none" and o["tp_hits"] == 0]
    print(f"no claim linked and price did not reach TP1 (check for unreported stops): {len(silent)}")
    for o in silent:
        print("  ", o["date"][:16], o["symbol"], o["side"], o["outcome"])
    with open("verify_results.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        wr.writeheader()
        wr.writerows(out)
    print("\nSaved verify_results.csv")


if __name__ == "__main__":
    main(*sys.argv[1:])