"""
Wallet-Sammler (Stufe 1 für Bot 4 "Smart-Wallets")
Sucht Wallets, die bei gut gelaufenen Solana-Memecoins Geld gemacht haben,
liest ihre letzten Trades aus der Chain und sortiert aus:

  1. Gewinner-Coins   Coins, die mehrere Tage überlebt haben, gewachsen sind und Liquidität halten
  2. Kandidaten       Top-Holder (ohne Pools, Dev, Insider) + große Trader der letzten 24 h
  3. Auslesen         letzte Transaktionen jeder Wallet über Helius (Solana-RPC)
  4. Sieben           Bot? Sniper/Insider? zu wenig Trades? letzte 10 Trades im Minus?
  5. Pool             was übrig bleibt, plus Gruppen mit gleicher Geldquelle (= evtl. eine Person)

Start:   HELIUS_API_KEY=... python wallet_collector.py
         python wallet_collector.py page     nur die Seite neu schreiben
Ergebnis: wallets.json, wallet_pool.csv (Excel), docs/wallets.html

Nur Daten auslesen. Kein Handel, kein Schlüssel, keine Wallet nötig.
"""

import csv
import glob
import html
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

import memecoin_funnel as funnel

# ======================= REGELN (hier anpassen) =======================
# Gewinner-Coins
WIN_MIN_MC = 300_000          # Market Cap mindestens ($)
WIN_MIN_AGE_D = 2             # mindestens so viele Tage überlebt
WIN_MAX_AGE_D = 45            # nicht älter (sonst sind die Trader von damals weg)
WIN_MIN_LIQ_RATIO = 0.05      # Liquidität mindestens 5 % der MC
NEW_WINNERS_PER_RUN = 15      # so viele neue Gewinner-Coins pro Lauf auswerten
WINNERS_KEEP_D = 30           # Gewinner-Coins so lange merken

# Kandidaten pro Gewinner-Coin
HOLDERS_PER_COIN = 5          # Top-Holder (ohne Pools/Dev/Insider) – 1. Lauf: kaum echte Trader darunter
HOLDER_MAX_PCT = 8            # über 8 % des Supplys = eher Dev/Locker/Börse, nicht nehmen
GT_TRADERS_PER_COIN = 15      # große Trader der letzten 24 h
GT_MIN_TRADE_USD = 300        # ab dieser Tradegröße

# Auslesen und Bewerten
TX_PER_WALLET = 150           # so viele letzte Transaktionen pro Wallet auswerten
SIG_SCAN = 1000               # so viele Signaturen für die Aktivität (Trades pro Tag)
LAST_N = 10                   # die letzten N abgeschlossenen Trades zählen
MIN_CLOSED = 10               # weniger abgeschlossene Trades = noch nicht bewertbar
CLOSE_LEFT = 0.05             # Position gilt als geschlossen, wenn unter 5 % vom Höchststand übrig

BOT_TX_PER_DAY = 300          # mehr Transaktionen pro Tag = Bot (Arbitrage/MEV)
BOT_MEDIAN_HOLD_MIN = 2       # Haltedauer im Median darunter = Bot
SNIPER_SEC = 120              # Kauf innerhalb von 2 Min. nach Pool-Start = Sniper-Kauf
SNIPER_SHARE = 0.5            # ab 50 % Sniper-Käufe = nicht nachmachbar

FUNDER_MAX_PAGES = 5          # Geldquelle: höchstens 5 x 1000 Signaturen zurückblättern
EXCHANGE_FUNDS = 5            # eine Geldquelle, die mehr als 5 unserer Wallets versorgt = Börse, keine Gruppe

WHALE_SOL = 20                # Ø-Einsatz ab 20 SOL pro Trade = Wal (bewegt den Kurs selbst)
BOT_WINNERS_D = 7             # Coins, mit denen unsere Papier-Bots in den letzten 7 Tagen Gewinn gemacht haben

RESCORE_D = 3                # Pool-Wallets alle 3 Tage neu bewerten
RECHECK_OUT_D = 14            # aussortierte Wallets frühestens nach 14 Tagen nochmal
MAX_SCORE_PER_RUN = 200       # so viele Wallets pro Lauf auslesen (~22.000 Credits)
CREDITS_MONTH_MAX = 800_000   # Helius-Gratis-Tarif hat 1 Mio. Credits/Monat, Rest als Puffer
RPC_PAUSE = 0.12              # Pause zwischen Abfragen (Gratis-Tarif: 10 pro Sekunde)
# =====================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "wallets.json")
CSV_FILE = os.path.join(HERE, "wallet_pool.csv")
PAGE = os.path.join(HERE, "docs", "wallets.html")

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT
MIN_SOL_MOVE = 0.0005         # kleinere SOL-Bewegungen sind nur Gebühren

STATUS_ORDER = ["Pool", "Zu wenig Trades", "Im Minus", "Sniper/Insider", "Bot", "Keine Daten"]


def now():
    return time.time()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"winners": {}, "wallets": {}, "credits": {}, "runs": [], "created": {}}


def save(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, DATA_FILE)


def sol_price():
    """Aktueller SOL-Kurs in $ (für Trades, die mit USDC/USDT bezahlt wurden)."""
    try:
        pairs = funnel.get("https://api.dexscreener.com/tokens/v1/solana/" + WSOL)
        for p in sorted(pairs, key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0)):
            if ((p.get("baseToken") or {}).get("address") == WSOL
                    and (p.get("quoteToken") or {}).get("address") in STABLES and p.get("priceUsd")):
                return float(p["priceUsd"])
    except Exception as e:
        log(f"SOL-Kurs nicht abrufbar: {e}")
    return 150.0


# ---------------------------------------------------------------- Helius / RPC
class Rpc:
    def __init__(self, key, data):
        self.url = f"https://mainnet.helius-rpc.com/?api-key={key}"
        self.data = data
        self.month = time.strftime("%Y-%m")
        self.used = 0
        self.gtfa = True      # getTransactionsForAddress (Helius-eigen) verfügbar? Wird beim 1. Fehler abgeschaltet
        self.sol_usd = sol_price()

    def credits_left(self):
        return CREDITS_MONTH_MAX - self.data["credits"].get(self.month, 0)

    def _spend(self, n):
        self.used += n
        self.data["credits"][self.month] = self.data["credits"].get(self.month, 0) + n

    def call(self, method, params, credits=1):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for attempt in range(4):
            time.sleep(RPC_PAUSE)
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    res = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 + attempt * 3)
                    continue
                if e.code in (401, 403):
                    raise SystemExit("Helius lehnt den API-Key ab (Fehler %d). Secret HELIUS_API_KEY prüfen." % e.code)
                if attempt == 3:
                    raise
                time.sleep(2)
                continue
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2)
                continue
            if "error" in res:
                if res["error"].get("code") == 429 or "rate" in str(res["error"]).lower():
                    time.sleep(2 + attempt * 3)
                    continue
                raise RuntimeError(f"{method}: {res['error']}")
            self._spend(credits)
            return res.get("result")
        raise RuntimeError(f"{method}: zu viele Versuche")

    def signatures(self, addr, limit=SIG_SCAN):
        return self.call("getSignaturesForAddress", [addr, {"limit": limit}]) or []

    def transactions(self, addr, sigs):
        """Letzte Transaktionen voll laden. Erst der günstige Helius-Weg, sonst einzeln."""
        if self.gtfa:
            try:
                res = self.call("getTransactionsForAddress", [addr, {
                    "transactionDetails": "full", "sortOrder": "desc", "limit": TX_PER_WALLET,
                    "encoding": "json", "maxSupportedTransactionVersion": 0,
                    "filters": {"status": "succeeded", "tokenAccounts": "balanceChanged"}}],
                    credits=10 * -(-TX_PER_WALLET // 100))
                return (res or {}).get("data") or []
            except RuntimeError as e:
                log(f"  getTransactionsForAddress nicht nutzbar ({str(e)[:80]}), lese einzeln")
                self.gtfa = False
        out = []
        for s in [s for s in sigs if not s.get("err")][:TX_PER_WALLET]:
            try:
                tx = self.call("getTransaction", [s["signature"], {
                    "encoding": "json", "maxSupportedTransactionVersion": 0}])
            except RuntimeError:
                continue
            if tx:
                out.append(tx)
        return out

    def first_transactions(self, addr, sigs=None):
        """Älteste Transaktionen (für die Geldquelle)."""
        if self.gtfa:
            try:
                res = self.call("getTransactionsForAddress", [addr, {
                    "transactionDetails": "full", "sortOrder": "asc", "limit": 5,
                    "encoding": "json", "maxSupportedTransactionVersion": 0}], credits=10)
                return (res or {}).get("data") or []
            except RuntimeError:
                pass
        # Ersatzweg: Signaturen bis zum Anfang zurückblättern (1 Credit pro 1000), dann die ältesten laden
        s = sigs if sigs is not None else self.signatures(addr)
        pages = 0
        while len(s) >= SIG_SCAN and pages < FUNDER_MAX_PAGES:
            more = self.call("getSignaturesForAddress", [addr, {"limit": SIG_SCAN, "before": s[-1]["signature"]}]) or []
            pages += 1
            if not more:
                break
            s = more
        if len(s) >= SIG_SCAN:
            return []     # zu viel Verlauf, Anfang nicht erreichbar
        out = []
        for x in reversed(s[-3:]):
            try:
                tx = self.call("getTransaction", [x["signature"], {
                    "encoding": "json", "maxSupportedTransactionVersion": 0}])
            except RuntimeError:
                continue
            if tx:
                out.append(tx)
        return out


# ---------------------------------------------------------------- Transaktionen lesen
def account_keys(tx):
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg.get("accountKeys") or []]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def token_amounts(balances, owner):
    out = {}
    for b in balances or []:
        if b.get("owner") != owner:
            continue
        ui = b.get("uiTokenAmount") or {}
        amt = ui.get("uiAmountString")
        amt = float(amt) if amt not in (None, "") else float(ui.get("uiAmount") or 0)
        out[b["mint"]] = out.get(b["mint"], 0.0) + amt
    return out


def parse_tx(tx, wallet, sol_usd=150.0):
    """Was hat diese Transaktion mit der Wallet gemacht?
    Liefert dict(kind=buy|sell|out|in|skip, mint, sol, bal, t).
    sol = Gegenwert in SOL (USDC/USDT werden zum aktuellen SOL-Kurs umgerechnet)."""
    meta = tx.get("meta") or {}
    t = tx.get("blockTime") or 0
    if meta.get("err") is not None:
        return {"kind": "skip", "t": t}
    keys = account_keys(tx)
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol = (post[i] - pre[i]) / 1e9
    pre_t = token_amounts(meta.get("preTokenBalances"), wallet)
    post_t = token_amounts(meta.get("postTokenBalances"), wallet)
    sol += post_t.get(WSOL, 0) - pre_t.get(WSOL, 0)
    deltas = {}
    for m in set(pre_t) | set(post_t):
        if m == WSOL:
            continue
        d = post_t.get(m, 0) - pre_t.get(m, 0)
        if abs(d) > 1e-9:
            if m in STABLES:
                sol += d / sol_usd    # mit USDC/USDT bezahlt: in SOL umrechnen
            else:
                deltas[m] = d
    if not deltas:
        return {"kind": "skip", "t": t}
    if len(deltas) > 1:
        return {"kind": "skip", "t": t, "multi": True}
    mint, d = next(iter(deltas.items()))
    bal = post_t.get(mint, 0)
    if d > 0 and sol < -MIN_SOL_MOVE:
        kind = "buy"
    elif d < 0 and sol > MIN_SOL_MOVE:
        kind = "sell"
    elif d < 0:
        kind = "out"     # weggeschickt, nicht verkauft
    else:
        kind = "in"      # bekommen, nicht gekauft
    return {"kind": kind, "mint": mint, "sol": sol, "bal": bal, "t": t}


def trades_from(txs, wallet, sol_usd=150.0):
    """Baut aus den Transaktionen abgeschlossene Trades pro Coin (Kauf ... Verkauf bis fast 0)."""
    events = sorted((parse_tx(tx, wallet, sol_usd) for tx in txs), key=lambda e: e["t"])
    pos, closed = {}, []
    for e in events:
        m = e.get("mint")
        if e["kind"] == "skip" or not m:
            continue
        p = pos.get(m)
        if e["kind"] == "buy":
            if p is None:
                p = pos[m] = {"mint": m, "open": e["t"], "out": 0.0, "in": 0.0, "peak": 0.0,
                              "buys": 0, "sells": 0, "unclear": False}
            p["out"] += -e["sol"]
            p["buys"] += 1
            p["peak"] = max(p["peak"], e["bal"])
        elif p is None:
            continue    # Kauf lag vor dem ausgewerteten Zeitraum
        elif e["kind"] == "sell":
            p["in"] += e["sol"]
            p["sells"] += 1
        elif e["kind"] == "out":
            p["unclear"] = True
        elif e["kind"] == "in":
            p["peak"] = max(p["peak"], e["bal"])
        if e["kind"] in ("sell", "out") and e["bal"] <= CLOSE_LEFT * p["peak"]:
            closed.append({"mint": m, "open": p["open"], "close": e["t"], "sol_out": round(p["out"], 4),
                           "sol_in": round(p["in"], 4), "pnl": round(p["in"] - p["out"], 4),
                           "hold_s": e["t"] - p["open"], "unclear": p["unclear"]})
            del pos[m]
    return [c for c in closed if not c["unclear"] and c["sol_out"] > 0], len(pos)


def funder_of(txs, wallet):
    """Wer hat die Wallet zuerst mit SOL versorgt?"""
    for tx in sorted(txs, key=lambda x: x.get("blockTime") or 0):
        meta = tx.get("meta") or {}
        keys = account_keys(tx)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if wallet not in keys:
            continue
        i = keys.index(wallet)
        if i >= len(pre) or post[i] - pre[i] <= 0:
            continue
        best, amt = None, 0
        for j, k in enumerate(keys[:len(pre)]):
            d = pre[j] - post[j]
            if k != wallet and d > amt:
                best, amt = k, d
        if best:
            return best
    return None


# ---------------------------------------------------------------- Bewertung
def created_times(mints, data):
    """Startzeit des ersten Pools je Coin (DexScreener), mit Zwischenspeicher."""
    cache = data.setdefault("created", {})
    todo = [m for m in set(mints) if m not in cache]
    for i in range(0, len(todo), 30):
        chunk = todo[i:i + 30]
        try:
            pairs = funnel.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
        except Exception:
            pairs = []
        for p in pairs or []:
            a = (p.get("baseToken") or {}).get("address")
            c = p.get("pairCreatedAt")
            if a in chunk and c:
                cache[a] = min(cache.get(a) or c / 1000, c / 1000)
        for m in chunk:
            cache.setdefault(m, 0)    # 0 = unbekannt, nicht nochmal fragen
        time.sleep(0.3)
    return cache


def score(closed, sigs, data):
    """Kennzahlen + Urteil. closed = abgeschlossene Trades (alt -> neu)."""
    s = {"closed": len(closed)}
    if sigs:
        ts = [x.get("blockTime") or 0 for x in sigs if x.get("blockTime")]
        span_d = (max(ts) - min(ts)) / 86400 if len(ts) > 1 else 0
        s["tx_day"] = round(len(sigs) / max(span_d, 1 / 24), 1)
        s["last_active"] = max(ts) if ts else None
    else:
        s["tx_day"] = 0
    holds = [c["hold_s"] for c in closed]
    s["hold_med_min"] = round(statistics.median(holds) / 60, 1) if holds else None

    created = created_times([c["mint"] for c in closed], data)
    early = [c for c in closed if created.get(c["mint"]) and c["open"] - created[c["mint"]] < SNIPER_SEC]
    s["sniper_share"] = round(len(early) / len(closed), 2) if closed else 0

    last = closed[-LAST_N:]
    out = sum(c["sol_out"] for c in last)
    s["pnl10"] = round(sum(c["pnl"] for c in last), 3)
    s["win10"] = round(sum(1 for c in last if c["pnl"] > 0) / len(last), 2) if last else None
    s["roi10"] = round(s["pnl10"] / out * 100, 1) if out else None
    s["best10"] = round(max((c["pnl"] for c in last), default=0), 3)
    s["avg_sol"] = round(out / len(last), 2) if last else None
    s["whale"] = bool(s["avg_sol"] and s["avg_sol"] >= WHALE_SOL)
    # Ohne den größten Treffer noch im Plus? (ein 50x und neun Verluste = Glück)
    s["pnl10_ohne_top"] = round(s["pnl10"] - max(s["best10"], 0), 3)

    if s["tx_day"] > BOT_TX_PER_DAY:
        st, why = "Bot", f"{s['tx_day']:.0f} Transaktionen pro Tag"
    elif s["hold_med_min"] is not None and len(closed) >= 5 and s["hold_med_min"] < BOT_MEDIAN_HOLD_MIN:
        st, why = "Bot", f"hält im Schnitt nur {s['hold_med_min']} Min."
    elif len(closed) < MIN_CLOSED:
        st, why = "Zu wenig Trades", f"nur {len(closed)} abgeschlossene Trades im Zeitraum"
    elif s["sniper_share"] >= SNIPER_SHARE:
        st, why = "Sniper/Insider", f"{s['sniper_share'] * 100:.0f} % der Käufe in den ersten {SNIPER_SEC // 60} Min."
    elif s["pnl10"] <= 0:
        st, why = "Im Minus", f"letzte {LAST_N} Trades {s['pnl10']:+.2f} SOL"
    else:
        st, why = "Pool", f"letzte {LAST_N} Trades {s['pnl10']:+.2f} SOL"
    s["status"], s["why"] = st, why
    s["last10"] = last
    return s


def evaluate(rpc, w, data):
    addr = w["addr"]
    try:
        sigs = rpc.signatures(addr)
        txs = rpc.transactions(addr, sigs) if sigs else []
    except RuntimeError as e:
        w.update(status="Keine Daten", why=str(e)[:120], checked=now())
        return
    closed, still_open = trades_from(txs, addr, rpc.sol_usd)
    s = score(closed, sigs, data)
    s["open_pos"] = still_open
    s["tx_read"] = len(txs)
    if not txs:
        s["status"], s["why"] = "Keine Daten", "keine Transaktionen gefunden"
    if s["status"] == "Pool" and not w.get("funder_checked"):
        w["funder"] = funder_of(rpc.first_transactions(addr, sigs), addr)
        w["funder_checked"] = True
    w.update(s)
    w["checked"] = now()


# ---------------------------------------------------------------- Kandidaten sammeln
def bot_winners():
    """Coins, die durch den Trichter kamen und mit denen ein Papier-Bot Gewinn gemacht hat."""
    out, cut = set(), now() - BOT_WINNERS_D * 86400
    for path in glob.glob(os.path.join(HERE, "bot_state_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            continue
        for t in st.get("closed") or []:
            if t.get("pnl", 0) > 0 and t.get("exit_time", 0) >= cut:
                out.add(t["addr"])
    return out


def find_winners(data):
    log("Suche Gewinner-Coins ...")
    src = funnel.collect_candidates()
    from_bots = bot_winners()
    for a in from_bots:
        src.setdefault(a, set()).add("bot")
    coins = funnel.market_data(src)
    fresh = []
    for c in coins:
        age_d = c["age_h"] / 24
        ratio = c["liq"] / c["mc"] if c["mc"] else 0
        c["from_bot"] = c["addr"] in from_bots
        # Bot-Gewinner haben den Trichter schon bestanden -> keine MC-/Mindestalter-Hürde
        big_enough = c["from_bot"] or (c["mc"] >= WIN_MIN_MC and age_d >= WIN_MIN_AGE_D)
        if (big_enough and age_d <= WIN_MAX_AGE_D
                and ratio >= WIN_MIN_LIQ_RATIO and c["addr"] not in data["winners"]):
            fresh.append(c)
    fresh.sort(key=lambda c: (c["from_bot"], c["mc"]), reverse=True)
    log(f"  davon aus den Papier-Bots: {sum(c['from_bot'] for c in fresh)}")
    log(f"  {len(coins)} Coins geprüft, {len(fresh)} neue Gewinner, nehme {min(len(fresh), NEW_WINNERS_PER_RUN)}")
    return fresh[:NEW_WINNERS_PER_RUN]


def holders_of(c):
    rc = funnel.rugcheck_live(c["addr"])
    if not rc or rc.get("rugged"):
        return [], rc
    pools = {m.get("pubkey") for m in rc.get("markets") or []} | set(c["pairs"])
    for m in rc.get("markets") or []:
        for k in ("liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            if isinstance(m.get(k), str):
                pools.add(m[k])
    creator = rc.get("creator")
    known = set((rc.get("knownAccounts") or {}).keys())    # Pools, Börsen, Locker
    out = []
    for h in rc.get("topHolders") or []:
        o = h.get("owner")
        if (not o or o in pools or o in known or o == creator or h.get("insider")
                or (h.get("pct") or 0) > HOLDER_MAX_PCT):
            continue
        out.append(o)
        if len(out) >= HOLDERS_PER_COIN:
            break
    return out, rc


def traders_of(c):
    pool = c.get("main_pair")
    if not pool:
        return []
    url = (f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/trades"
           f"?trade_volume_in_usd_greater_than={GT_MIN_TRADE_USD}")
    try:
        rows = funnel.get(url).get("data") or []
    except Exception as e:
        log(f"  GeckoTerminal Trades für {c['sym']} nicht erreichbar: {e}")
        return []
    vol = {}
    for r in rows:
        a = r.get("attributes") or {}
        w = a.get("tx_from_address")
        if w:
            vol[w] = vol.get(w, 0) + float(a.get("volume_in_usd") or 0)
    time.sleep(2.1)   # GeckoTerminal: 30 Abfragen pro Minute
    return [w for w, _ in sorted(vol.items(), key=lambda x: -x[1])[:GT_TRADERS_PER_COIN]]


def collect(data):
    try:
        winners = find_winners(data)
    except Exception as e:
        log(f"Gewinner-Suche fehlgeschlagen: {e}")
        winners = []
    for c in winners:
        try:
            holders, rc = holders_of(c)
        except Exception as e:
            log(f"  ${c['sym']}: RugCheck-Fehler {e}")
            continue
        if rc.get("rugged"):
            continue
        traders = traders_of(c)
        data["winners"][c["addr"]] = {"sym": c["sym"], "mc": c["mc"], "age_d": round(c["age_h"] / 24, 1),
                                      "src": "Papier-Bot" if c.get("from_bot") else "Trending",
                                      "found": now(), "holders": len(holders), "traders": len(traders)}
        for addr, how in [(h, "Holder") for h in holders] + [(t, "Trader") for t in traders]:
            w = data["wallets"].setdefault(addr, {"addr": addr, "first_seen": now(), "coins": {}, "status": "Neu"})
            w["coins"][c["addr"]] = how
        log(f"  ${c['sym']}: {len(holders)} Holder, {len(traders)} Trader")
    # alte Gewinner vergessen
    cut = now() - WINNERS_KEEP_D * 86400
    data["winners"] = {k: v for k, v in data["winners"].items() if v["found"] >= cut}


def queue(data):
    t = now()
    todo = []
    for w in data["wallets"].values():
        st, last = w.get("status", "Neu"), w.get("checked") or 0
        if st == "Neu":
            due = True
        elif st in ("Pool", "Zu wenig Trades", "Keine Daten"):
            due = t - last > RESCORE_D * 86400
        else:
            due = t - last > RECHECK_OUT_D * 86400
        if due:
            todo.append(w)
    # Trader zuerst: im 1. Lauf kamen alle Pool-Wallets aus den Tradern, fast keine aus den Holdern
    todo.sort(key=lambda w: (w.get("status") != "Pool", "Trader" not in (w.get("coins") or {}).values(),
                             -len(w.get("coins") or {}), w.get("checked") or 0))
    return todo


# ---------------------------------------------------------------- Ergebnis
def groups(pool, all_wallets=None):
    """Wallets mit gleicher Geldquelle = vermutlich eine Person -> gleiche Gruppe.
    Versorgt eine Quelle sehr viele Wallets, ist es eher eine Börse -> keine Gruppe."""
    funded = {}
    for w in all_wallets or pool:
        if w.get("funder"):
            funded[w["funder"]] = funded.get(w["funder"], 0) + 1
    by = {}
    for w in pool:
        if w.get("funder"):
            by.setdefault(w["funder"], []).append(w["addr"])
    gid, out = 0, {}
    for f, ws in by.items():
        if 1 < len(ws) and funded.get(f, 0) <= EXCHANGE_FUNDS:
            gid += 1
            for a in ws:
                out[a] = gid
    return out


def short(a):
    return f"{a[:4]}…{a[-4:]}"


def de(x, d=2):
    return "" if x is None else f"{x:.{d}f}".replace(".", ",")


def write_csv(pool, grp):
    fields = ["Wallet", "Gewinner-Coins", "Abgeschl. Trades", f"Gewinn letzte {LAST_N} (SOL)",
              "Ohne größten Treffer (SOL)", "Treffer %", "ROI %", "Haltedauer Median (Min.)",
              "Trades/Tag", "Ø Einsatz (SOL)", "Wal", "Sniper-Anteil %", "Gleiche Geldquelle (Gruppe)", "Geprüft"]
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(fields)
        for w in pool:
            wr.writerow([w["addr"], len(w.get("coins") or {}), w.get("closed"), de(w.get("pnl10"), 3),
                         de(w.get("pnl10_ohne_top"), 3), de((w.get("win10") or 0) * 100, 0), de(w.get("roi10"), 1),
                         de(w.get("hold_med_min"), 1), de(w.get("tx_day"), 1),
                         de(w.get("avg_sol"), 2), "ja" if w.get("whale") else "",
                         de((w.get("sniper_share") or 0) * 100, 0), grp.get(w["addr"], ""),
                         time.strftime("%d.%m.%Y %H:%M", time.localtime(w.get("checked") or 0))])


def write_page(data):
    from fomo_bot import CSS, berlin, pct
    ws = list(data["wallets"].values())
    for w in ws:   # ältere Einträge: Ø-Einsatz aus den gespeicherten letzten Trades nachrechnen
        if "avg_sol" not in w and w.get("last10"):
            w["avg_sol"] = round(sum(c["sol_out"] for c in w["last10"]) / len(w["last10"]), 2)
            w["whale"] = w["avg_sol"] >= WHALE_SOL
    # Oben: Wallets, die auch ohne ihren größten Treffer im Plus sind, dann nach ROI
    pool = sorted([w for w in ws if w.get("status") == "Pool"],
                  key=lambda w: ((w.get("pnl10_ohne_top") or 0) > 0, w.get("roi10") or 0), reverse=True)
    grp = groups(pool, ws)
    write_csv(pool, grp)

    counts = {s: 0 for s in STATUS_ORDER + ["Neu"]}
    for w in ws:
        counts[w.get("status", "Neu")] = counts.get(w.get("status", "Neu"), 0) + 1
    chips = "".join(f'<span class="chip">{html.escape(k)}: {v}</span>' for k, v in counts.items() if v)

    rows = []
    for w in pool:
        a = w["addr"]
        g = grp.get(a)
        flags = []
        if g:
            flags.append(f'<span class="chip">Gruppe {g}</span>')
        if (w.get("pnl10_ohne_top") or 0) <= 0:
            flags.append('<span class="chip">nur 1 großer Treffer</span>')
        if w.get("whale"):
            flags.append(f'<span class="chip">Wal · Ø {w.get("avg_sol")} SOL</span>')
        rows.append(
            f'<tr><td><code>{a}</code><br><span class="small"><a href="https://solscan.io/account/{a}">Solscan</a> · '
            f'<a href="https://gmgn.ai/sol/address/{a}">GMGN</a></span><br>{"".join(flags)}</td>'
            f'<td>{len(w.get("coins") or {})}</td><td>{w.get("closed")}</td>'
            f'<td>{(w.get("pnl10") or 0):+.2f} SOL<br><span class="small muted">ohne Top-Trade {(w.get("pnl10_ohne_top") or 0):+.2f}</span></td>'
            f'<td>{(w.get("win10") or 0) * 100:.0f} %</td><td>{pct(w.get("roi10"))}</td>'
            f'<td>{w.get("hold_med_min") or "–"} Min.</td><td>{w.get("avg_sol") if w.get("avg_sol") is not None else "–"} SOL</td><td>{w.get("tx_day")}</td></tr>')

    out_rows = []
    for w in sorted([w for w in ws if w.get("status") not in ("Pool", "Neu")],
                    key=lambda w: -(w.get("checked") or 0))[:40]:
        out_rows.append(f'<tr><td><code>{short(w["addr"])}</code></td><td>{html.escape(w.get("status", ""))}</td>'
                        f'<td class="small">{html.escape(w.get("why", ""))}</td></tr>')

    win_rows = "".join(
        f'<tr><td><b>${html.escape(v["sym"])}</b><br><code>{k}</code></td><td>{funnel.fmt(v["mc"])}</td>'
        f'<td>{v["age_d"]} Tage</td><td>{v["holders"]} / {v["traders"]}</td><td class="small">{v.get("src", "Trending")}</td></tr>'
        for k, v in sorted(data["winners"].items(), key=lambda x: -x[1]["found"])[:30])

    month = time.strftime("%Y-%m")
    used = data["credits"].get(month, 0)
    last = data["runs"][-1] if data["runs"] else None
    run_txt = (f'Letzter Lauf: {berlin(last["t"]):%d.%m.%Y %H:%M} Uhr · {last["scored"]} Wallets ausgelesen · '
               f'{last["credits"]} Credits') if last else "Noch kein Lauf."
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart-Wallets</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>Smart-Wallets · Stufe 1</h1><a class="small" href="index.html">← zum Bot-Dashboard</a></header>
<p class="small muted">{run_txt} · Helius-Credits diesen Monat: {used:,} von {CREDITS_MONTH_MAX:,}</p>
<div class="card"><h3>Stand aller Wallets ({len(ws)})</h3>{chips}
<p class="small muted">Pool = mindestens {MIN_CLOSED} abgeschlossene Trades, letzte {LAST_N} im Plus, kein Bot, kein Sniper.
Gleiche „Gruppe“ = gleiche Geldquelle, zählt beim Anker nur einmal.</p></div>
<h2>Pool ({len(pool)})</h2>
<div class="card scroll">{'<table><tr><th>Wallet</th><th>Gewinner-Coins</th><th>Trades</th><th>Letzte ' + str(LAST_N) + '</th><th>Treffer</th><th>ROI</th><th>Haltedauer</th><th>Ø Einsatz</th><th>Tx/Tag</th></tr>' + ''.join(rows) + '</table>' if rows else '<p class="muted">Noch keine Wallet hat alle Prüfungen bestanden.</p>'}</div>
<h2>Zuletzt aussortiert</h2>
<div class="card scroll">{'<table><tr><th>Wallet</th><th>Status</th><th>Grund</th></tr>' + ''.join(out_rows) + '</table>' if out_rows else '<p class="muted">Noch nichts.</p>'}</div>
<h2>Gewinner-Coins (Quelle)</h2>
<div class="card scroll">{'<table><tr><th>Coin</th><th>MC beim Fund</th><th>Alter</th><th>Holder / Trader</th><th>Quelle</th></tr>' + win_rows + '</table>' if win_rows else '<p class="muted">Noch keine.</p>'}</div>
<footer>Nur ausgelesene Chain-Daten, kein Handel. Gewinne in SOL inkl. Gebühren. Trades, deren Kauf vor dem
ausgewerteten Zeitraum lag oder deren Coins weggeschickt wurden, zählen nicht. Kein Kaufsignal.</footer></div></body></html>"""
    os.makedirs(os.path.dirname(PAGE), exist_ok=True)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(doc)


# ---------------------------------------------------------------- Start
def main():
    data = load()
    if len(sys.argv) > 1 and sys.argv[1] == "page":
        write_page(data)
        return
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        raise SystemExit("HELIUS_API_KEY fehlt. Im GitHub-Repo unter Settings > Secrets > Actions eintragen.")
    rpc = Rpc(key, data)
    rpc.gtfa = data.get("gtfa", True)    # einmal festgestellt, dass es im Tarif fehlt -> nicht jedes Mal neu testen
    collect(data)
    save(data)

    # Geldquelle für Pool-Wallets nachholen, die noch keine Prüfung hatten (kostet nur wenige Credits)
    for w in [w for w in data["wallets"].values() if w.get("status") == "Pool" and not w.get("funder_checked")]:
        try:
            w["funder"] = funder_of(rpc.first_transactions(w["addr"]), w["addr"])
            w["funder_checked"] = True
        except Exception as e:
            log(f"  Geldquelle {short(w['addr'])}: {e}")
    data["gtfa"] = rpc.gtfa

    todo = queue(data)[:MAX_SCORE_PER_RUN]
    log(f"Lese {len(todo)} Wallets aus ({len(data['wallets'])} bekannt, Credits übrig: {rpc.credits_left():,})")
    done = 0
    for w in todo:
        if rpc.credits_left() < 2000:
            log("Monatsbudget an Credits fast aufgebraucht, höre auf.")
            break
        try:
            evaluate(rpc, w, data)
        except SystemExit:
            raise
        except Exception as e:   # eine kaputte Wallet soll nicht den ganzen Lauf abbrechen
            w.update(status="Keine Daten", why=f"Fehler: {str(e)[:100]}", checked=now())
        done += 1
        log(f"  {short(w['addr'])}: {w['status']} – {w.get('why', '')}")
        if done % 10 == 0:
            save(data)
    data["gtfa"] = rpc.gtfa
    data["runs"] = (data["runs"] + [{"t": now(), "scored": done, "credits": rpc.used}])[-60:]
    save(data)
    write_page(data)
    pool = sum(1 for w in data["wallets"].values() if w.get("status") == "Pool")
    log(f"Fertig: {done} Wallets ausgelesen, {pool} im Pool, {rpc.used} Credits verbraucht.")


if __name__ == "__main__":
    main()
