"""
Auto-trader for A. Pierce Capital signals on Bybit USDT perpetuals.

Flow: new channel post -> parse -> check -> market entry with stop loss ->
reduce-only take profit orders -> Telegram alert after the trade is placed.
After TP1 fills, the stop loss moves to entry. A final alert fires when the
position closes.

Default mode is Bybit DEMO. Live needs BYBIT_MODE=live and CONFIRM_LIVE=yes.
"""
import asyncio, json, os, time, urllib.parse, urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from dotenv import load_dotenv
from parse_signals import parse_signal_text

load_dotenv()
KEYWORD = os.getenv("CHANNEL_KEYWORD", "A. Pierce Capital").lower()
MODE = os.getenv("BYBIT_MODE", "demo").lower()          # demo | testnet | live
RISK_USD = float(os.getenv("RISK_USD", "2"))            # loss if stop loss hits
LEVERAGE = float(os.getenv("LEVERAGE", "10"))
MAX_NOTIONAL_USD = float(os.getenv("MAX_NOTIONAL_USD", "500"))
MAX_CHASE_PCT = float(os.getenv("MAX_CHASE_PCT", "0.3")) # skip if price ran this far past entry
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
# Share of the position closed at each target, in order. "100" = close everything at TP1.
TP_WEIGHTS = [float(x) for x in os.getenv("TP_WEIGHTS", "100").split(",") if x.strip()]
BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN")
CHAT_ID = os.getenv("ALERT_CHAT_ID")
STATE_FILE = "state.json"
bybit = None


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"seen": [], "trades": {}}


STATE = load_state()


def save_state():
    json.dump(STATE, open(STATE_FILE, "w"), indent=1)


def push(text):
    print(text, flush=True)
    if not (BOT_TOKEN and CHAT_ID):
        return
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data, timeout=10)
    except Exception as ex:
        print("alert failed:", ex, flush=True)


def fmt(d):
    return format(Decimal(d).normalize(), "f")


def floor_step(x, step):
    step = Decimal(step)
    return (Decimal(str(x)) / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_tick(x, tick):
    tick = Decimal(tick)
    return (Decimal(str(x)) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


def get_instrument(symbol):
    try:
        lst = bybit.get_instruments_info(category="linear", symbol=symbol)["result"]["list"]
    except Exception:
        return None
    return lst[0] if lst and lst[0].get("status") == "Trading" else None


def last_price(symbol):
    return float(bybit.get_tickers(category="linear", symbol=symbol)["result"]["list"][0]["lastPrice"])


def get_position(symbol):
    for p in bybit.get_positions(category="linear", symbol=symbol)["result"]["list"]:
        if float(p.get("size") or 0) > 0:
            return p
    return None


def wait_position(symbol, tries=12):
    for _ in range(tries):
        p = get_position(symbol)
        if p:
            return p
        time.sleep(0.5)
    return None


def execute(sig, msg_id):
    """Blocking. Run in a thread."""
    sym = sig["symbol"]
    if str(msg_id) in STATE["seen"]:
        return
    STATE["seen"].append(str(msg_id))
    STATE["seen"] = STATE["seen"][-500:]
    save_state()
    head = f"{sym} {sig['side'].upper()}"
    if not sig["valid"]:
        return push(f"SKIPPED {head}: {sig['reason']}")
    if sym in STATE["trades"]:
        return push(f"SKIPPED {head}: a trade on {sym} is already open")
    info = get_instrument(sym)
    if not info:
        return push(f"SKIPPED {head}: not available on Bybit USDT perpetuals")

    long = sig["side"] == "long"
    entry, sl, targets = sig["entry"], sig["sl"], sig["targets"]
    price = last_price(sym)
    chase = (1 if long else -1) * (price - entry) / entry * 100
    if chase > MAX_CHASE_PCT:
        return push(f"SKIPPED {head}: price {price} is {chase:.2f}% past entry {entry}")
    if (long and price <= sl) or (not long and price >= sl):
        return push(f"SKIPPED {head}: price {price} is already through the stop loss {sl}")

    step = info["lotSizeFilter"]["qtyStep"]
    min_qty = Decimal(info["lotSizeFilter"]["minOrderQty"])
    tick = info["priceFilter"]["tickSize"]
    qty = floor_step(min(RISK_USD / abs(entry - sl), MAX_NOTIONAL_USD / entry), step)
    if qty < min_qty:
        return push(f"SKIPPED {head}: size {qty} is below Bybit minimum {min_qty}")
    risk = float(qty) * abs(entry - sl)
    if DRY_RUN:
        return push(f"DRY RUN {head}: would buy/sell {fmt(qty)} at ~{price}, SL {sl}, TPs {targets}, risk ${risk:.2f}")

    lev = min(LEVERAGE, float(info["leverageFilter"]["maxLeverage"]))
    try:
        bybit.set_leverage(category="linear", symbol=sym, buyLeverage=fmt(lev), sellLeverage=fmt(lev))
    except Exception as ex:
        if "110043" not in str(ex):   # 110043 = leverage not modified
            raise
    bybit.place_order(category="linear", symbol=sym, side="Buy" if long else "Sell",
                      orderType="Market", qty=fmt(qty), stopLoss=fmt(round_tick(sl, tick)),
                      slTriggerBy="LastPrice", positionIdx=0)
    pos = wait_position(sym)
    if not pos:
        return push(f"CHECK BYBIT {head}: order sent but no open position found")
    size, avg = Decimal(pos["size"]), float(pos["avgPrice"])

    ws = TP_WEIGHTS[:len(targets)]
    while len(ws) > 1 and ws[-1] == 0:
        ws.pop()
    while True:
        tot = sum(ws)
        chunks = [floor_step(size * Decimal(str(w / tot)), step) for w in ws]
        chunks[-1] = size - sum(chunks[:-1])
        if len(ws) == 1 or min(chunks) >= min_qty:
            break
        ws = ws[:-1]
    n, ids = len(ws), []
    for i in range(n):
        r = bybit.place_order(category="linear", symbol=sym, side="Sell" if long else "Buy",
                              orderType="Limit", qty=fmt(chunks[i]), price=fmt(round_tick(targets[i], tick)),
                              reduceOnly=True, timeInForce="GTC", positionIdx=0)
        ids.append(r["result"]["orderId"])

    STATE["trades"][sym] = dict(side=sig["side"], entry=avg, qty=str(size), sl=sl, tp_ids=ids,
                                tp_prices=targets[:n], tp1_done=False, ts=time.time(), msg_id=msg_id)
    save_state()
    push(f"TRADE PLACED [{MODE}]: {head}\n"
         f"Size: {fmt(size)} at avg {avg} (signal entry {entry})\n"
         f"Stop loss: {sl} (risk about ${risk:.2f})\n"
         f"Take profits: {', '.join(str(x) for x in targets[:n])}"
         + ("\nStop loss moves to entry after TP1." if n > 1 else ""))


def check_trade(sym, tr):
    """Blocking. Run in a thread."""
    pos = get_position(sym)
    if not pos:
        try:
            bybit.cancel_all_orders(category="linear", symbol=sym)
        except Exception:
            pass
        pnl = None
        try:
            rows = bybit.get_closed_pnl(category="linear", symbol=sym, limit=50)["result"]["list"]
            pnl = sum(float(r["closedPnl"]) for r in rows if int(r["createdTime"]) >= tr["ts"] * 1000 - 5000)
        except Exception:
            pass
        del STATE["trades"][sym]
        save_state()
        return push(f"TRADE CLOSED: {sym} {tr['side'].upper()}" + (f"\nResult: {pnl:+.2f} USDT" if pnl is not None else ""))
    if not tr["tp1_done"]:
        oid = tr["tp_ids"][0]
        open_o = bybit.get_open_orders(category="linear", symbol=sym, orderId=oid)["result"]["list"]
        if not open_o:
            hist = bybit.get_order_history(category="linear", symbol=sym, orderId=oid)["result"]["list"]
            status = hist[0]["orderStatus"] if hist else "Unknown"
            tr["tp1_done"] = True
            save_state()
            if status == "Filled":
                bybit.set_trading_stop(category="linear", symbol=sym, stopLoss=fmt(Decimal(str(tr["entry"]))), positionIdx=0)
                push(f"TP1 HIT: {sym}. Stop loss moved to entry {tr['entry']}.")
            else:
                push(f"CHECK BYBIT: {sym} TP1 order status is {status}.")


async def monitor():
    while True:
        await asyncio.sleep(5)
        for sym, tr in list(STATE["trades"].items()):
            try:
                await asyncio.to_thread(check_trade, sym, tr)
            except Exception as ex:
                print("monitor error:", sym, ex, flush=True)


async def main():
    global bybit
    from telethon import TelegramClient, events
    from pybit.unified_trading import HTTP
    if MODE == "live" and os.getenv("CONFIRM_LIVE") != "yes":
        raise SystemExit("Live mode needs CONFIRM_LIVE=yes in .env")
    bybit = HTTP(testnet=MODE == "testnet", demo=MODE == "demo",
                 api_key=os.getenv("BYBIT_API_KEY"), api_secret=os.getenv("BYBIT_API_SECRET"))
    client = TelegramClient("session", int(os.getenv("TG_API_ID")), os.getenv("TG_API_HASH"))

    @client.on(events.NewMessage)
    async def on_message(event):
        chat = await event.get_chat()
        if KEYWORD not in (getattr(chat, "title", "") or "").lower():
            return
        text = event.raw_text or ""
        with open("messages.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "id": event.id, "reply_to": event.reply_to_msg_id,
                                "text": text}, ensure_ascii=False) + "\n")
        sig = parse_signal_text(text)
        if not sig:
            return
        try:
            await asyncio.to_thread(execute, sig, event.id)
        except Exception as ex:
            push(f"ERROR on {sig['symbol']}: {ex}")

    await client.start()
    asyncio.create_task(monitor())
    push(f"Bot started. Mode: {MODE}{' (dry run)' if DRY_RUN else ''}. Risk per trade: ${RISK_USD}.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())