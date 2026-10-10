"""
Parser for A. Pierce Capital channel posts.

parse_signal_text(text) -> dict | None
    None  : the message is not a trade signal at all (chatter, results, etc.)
    dict  : coin, symbol, side, entry, sl, targets, valid, reason
            valid=False means it looked like a signal but the numbers don't add up,
            so bot.py should skip it instead of trading it.

Rebuild signals.json from history.jsonl:
    python parse_signals.py [history.jsonl] [out.json]
"""
import json, re, sys

HEAD = re.compile(r"^\W*([A-Z0-9]{2,15})\s*[\u2013\u2014\-]+\s*(LONG|SHORT)\b", re.I | re.M)
NUM = r"(\d+(?:[.,]\d+)?)"
ENTRY = re.compile(r"Entry\s*:?\s*" + NUM, re.I)
SL = re.compile(r"Stop\s*Loss\s*:?\s*" + NUM, re.I)
TARGETS = re.compile(r"Targets?\s*:?\s*((?:" + NUM + r"[\s/,]*)+)", re.I)   # one per line, or 1 / 2 / 3


def _f(s):
    return float(s.replace(",", "."))


def parse_signal_text(text):
    text = text or ""
    h = HEAD.search(text)
    e, s = ENTRY.search(text), SL.search(text)
    if not (h and e and s):
        return None
    coin, side = h.group(1).upper(), h.group(2).lower()
    t = TARGETS.search(text)
    targets = [_f(x) for x in re.findall(NUM, t.group(1))] if t else []
    sig = dict(coin=coin, symbol=coin + "USDT", side=side,
               entry=_f(e.group(1)), sl=_f(s.group(1)), targets=targets,
               valid=True, reason="")
    entry, sl, long = sig["entry"], sig["sl"], side == "long"
    if not targets:
        return {**sig, "valid": False, "reason": "no targets found"}
    if (long and not sl < entry) or (not long and not sl > entry):
        return {**sig, "valid": False, "reason": "stop loss is on the wrong side of entry"}
    ordered = all((b > a) if long else (b < a) for a, b in zip([entry] + targets, targets))
    if not ordered:
        return {**sig, "valid": False, "reason": "targets are not in order away from entry"}
    return sig


# ---- channel follow-up messages (win / stop claims) -------------------------

ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4}


def classify_claim(text):
    t = (text or "").lower()
    if "almost" in t:                       # "almost took the target" is not a win claim
        return None
    if re.search(r"stop\s*loss|stopped|\bsl\b|missed", t) and not re.search(r"first (tp|target|take)", t.split("missed")[0]):
        return dict(kind="SL", n=None)
    m = re.search(r"tp\s*#?\s*(\d)", t)
    if m:
        return dict(kind="TP", n=int(m.group(1)))
    m = re.search(r"\b(first|second|third|fourth|1st|2nd|3rd)\b[^.\n]{0,20}(tp|take profit|target)", t)
    if m:
        return dict(kind="TP", n=ORD[m.group(1)])
    m = re.search(r"(first|second|third)\s+(target|tp)", t)
    if m:
        return dict(kind="TP", n=ORD[m.group(1)])
    if re.search(r"target|\btp\b|take profit", t):
        return dict(kind="OTHER", n=None)
    return None


def build(history_path="history.jsonl", out_path="signals.json"):
    rows = [json.loads(l) for l in open(history_path, encoding="utf-8") if l.strip()]
    rows.sort(key=lambda r: r["id"])
    sigs, seen = {}, set()
    for r in rows:
        p = parse_signal_text(r["text"])
        if p:
            key = (p["symbol"], p["side"], p["entry"], p["sl"])
            p.update(id=r["id"], date=r["date"], duplicate=key in seen, claims=[])
            seen.add(key)
            sigs[r["id"]] = p
    for r in rows:
        tgt = sigs.get(r["reply_to"])
        c = classify_claim(r["text"]) if tgt else None
        if c:
            tgt["claims"].append(dict(**c, id=r["id"], date=r["date"], text=(r["text"] or "")[:200]))
    out = sorted(sigs.values(), key=lambda s: s["date"], reverse=True)
    json.dump(out, open(out_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return out


if __name__ == "__main__":
    out = build(*sys.argv[1:3])
    print(f"{len(out)} signals, {sum(s['valid'] for s in out)} valid, {sum(s['duplicate'] for s in out)} duplicates")
