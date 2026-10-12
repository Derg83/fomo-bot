"""
Smart-Money-Radar: Kaufen unsere Pool-Wallets gerade einen kleinen Coin?

Statt jede Wallet einzeln abzufragen (kostet Helius-Credits), schaut der Radar in die letzten
Trades (ab 100 $) kleiner und neuer Coins bei GeckoTerminal (gratis) und sucht dort unsere Wallets.

  Coin-Liste   alle Coins aus dem Trichter-Eingang mit MC 80k-1,5 Mio. + neue Pools von GeckoTerminal
  pro Runde    ein paar Coins der Reihe nach (Coins mit offener Smart-Position jede Runde)
  Signal       eine gute Wallet (auch ohne größten Treffer im Plus) kauft ab 150 $
               oder zwei verschiedene Pool-Wallets kaufen, jeweils in den letzten 3 Stunden
  Verkauf      verkauft eine Wallet, die das Signal ausgelöst hat, wird das ebenfalls gemerkt

Alles wird in bot_meta.json unter "radar" gespeichert. Nur Daten lesen, kein Handel.
"""

import calendar
import json
import os
import time

import memecoin_funnel as funnel

HERE = os.path.dirname(os.path.abspath(__file__))
WALLETS_FILE = os.path.join(HERE, "wallets.json")

POOL_MC_MIN = 80_000          # Coins für den Radar: MC ab ...
POOL_MC_MAX = 1_500_000       # ... bis ...
POOL_MIN_LIQ = 15_000         # Liquidität mindestens
POOL_KEEP_H = 48              # so lange ohne neue Bestätigung in der Liste behalten
POOLS_PER_TICK = 8            # so viele Coins pro Runde (GeckoTerminal: 30 Abfragen pro Minute)
MIN_TRADE_USD = 100           # kleinere Trades ignorieren
SIGNAL_H = 3                  # Käufe der letzten 3 Stunden zählen
GOOD_MIN_USD = 150            # eine gute Wallet reicht ab diesem Kaufbetrag
SEEN_MAX = 4000               # so viele Transaktions-IDs merken (keine Doppelzählung)
SIGNALS_KEEP_H = 72           # Signale so lange für das Dashboard merken
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
# Käufer-Log: alle größeren Käufer der Radar-Coins merken. Läuft ein Coin später 3x, wissen wir,
# wer VOR dem Lauf gekauft hat (Auswertung in rueckblick_wallets.py, Modus "radar").
BUYERS_FILE = os.path.join(HERE, "rueckblick", "kaeufer.json")
BUYER_MIN_USD = 200           # Käufe ab 200 $ merken
BUYERS_PER_COIN = 400         # höchstens so viele Käufer pro Coin (die frühesten bleiben)
BUYERS_KEEP_D = 10            # Coins so lange im Log behalten


def log(msg):
    print(f"[Radar] {msg}", flush=True)


_wallet_cache = {"mtime": None, "data": {}}


def wallets():
    """Pool-Wallets aus dem Wallet-Sammler. good = auch ohne den größten Treffer im Plus."""
    try:
        mt = os.path.getmtime(WALLETS_FILE)
    except OSError:
        return {}
    if _wallet_cache["mtime"] != mt:
        with open(WALLETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _wallet_cache["data"] = {a: {"good": (w.get("pnl10_ohne_top") or 0) > 0, "pnl10": w.get("pnl10") or 0}
                                 for a, w in data.get("wallets", {}).items() if w.get("status") == "Pool"}
        _wallet_cache["mtime"] = mt
    return _wallet_cache["data"]


def state(meta):
    r = meta.setdefault("radar", {})
    for k, v in (("pools", {}), ("cursor", 0), ("signals", {}), ("seen", []), ("checked", {})):
        r.setdefault(k, v)
    return r


def update_pools(meta, coins):
    """Nach jedem Trichter-Lauf: kleine Coins aus dem Eingang + neue Pools in die Radar-Liste."""
    r, now = state(meta), time.time()
    added = 0
    for c in coins:
        if (POOL_MC_MIN <= c["mc"] <= POOL_MC_MAX and c["liq"] >= POOL_MIN_LIQ and c.get("main_pair")):
            if c["addr"] not in r["pools"]:
                added += 1
            r["pools"][c["addr"]] = {"pair": c["main_pair"], "sym": c["sym"], "mc": c["mc"],
                                     "vol24": c.get("vol24") or 0, "seen": now}
    for page in (1, 2):
        try:
            data = funnel.get(f"{GT}/new_pools?page={page}")
        except Exception as e:
            log(f"neue Pools Seite {page}: {e}")
            continue
        for p in data.get("data", []):
            a = p.get("attributes") or {}
            mc = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0)
            liq = float(a.get("reserve_in_usd") or 0)
            mint = p["relationships"]["base_token"]["data"]["id"].replace("solana_", "")
            if mint in funnel.SKIP or not (POOL_MC_MIN <= mc <= POOL_MC_MAX and liq >= POOL_MIN_LIQ):
                continue
            if mint not in r["pools"]:
                added += 1
            r["pools"][mint] = {"pair": a.get("address"), "sym": (a.get("name") or "?").split(" / ")[0],
                                "mc": mc, "vol24": float((a.get("volume_usd") or {}).get("h24") or 0), "seen": now}
        time.sleep(2.1)
    # alte Coins vergessen (außer es gibt ein frisches Signal)
    keep = now - POOL_KEEP_H * 3600
    sig_keep = now - SIGNALS_KEEP_H * 3600
    r["pools"] = {m: p for m, p in r["pools"].items()
                  if p["seen"] >= keep or r["signals"].get(m, {}).get("last", 0) >= sig_keep}
    log(f"{len(r['pools'])} Coins im Radar ({added} neu)")


def _trades(pair):
    data = funnel.get(f"{GT}/pools/{pair}/trades?trade_volume_in_usd_greater_than={MIN_TRADE_USD}")
    return data.get("data") or []


def tick(meta, priority=(), budget_s=25):
    """Ein paar Coins prüfen. priority = Coins, die jede Runde dran sind (offene Smart-Positionen)."""
    r, now = state(meta), time.time()
    ws = wallets()
    if not ws or not r["pools"]:
        return
    seen = set(r["seen"])
    order = list(r["pools"])
    todo = [m for m in priority if m in r["pools"]]
    i = r["cursor"] % max(1, len(order))
    while len(todo) < POOLS_PER_TICK + len(priority) and len(todo) < len(order):
        m = order[i % len(order)]
        i += 1
        if m not in todo:
            todo.append(m)
    r["cursor"] = i % max(1, len(order))
    start = time.time()
    buyers_log = _load_buyers()
    for m in todo:
        if time.time() - start > budget_s:
            break
        p = r["pools"][m]
        try:
            rows = _trades(p["pair"])
        except Exception as e:
            log(f"Trades ${p['sym']}: {e}")
            time.sleep(2.1)
            continue
        _log_buyers(buyers_log, m, p, rows, now)
        for row in rows:
            a = row.get("attributes") or {}
            w, h = a.get("tx_from_address"), a.get("tx_hash")
            if w not in ws or not h:
                continue
            key = f"{h[:24]}:{m[:8]}"
            if key in seen:
                continue
            if a.get("to_token_address") == m:
                kind = "buy"
            elif a.get("from_token_address") == m:
                kind = "sell"
            else:
                continue
            seen.add(key)
            r["seen"].append(key)
            try:
                t = calendar.timegm(time.strptime(a["block_timestamp"], "%Y-%m-%dT%H:%M:%SZ"))
            except Exception:
                t = now
            usd = float(a.get("volume_in_usd") or 0)
            s = r["signals"].setdefault(m, {"sym": p["sym"], "buys": [], "sells": [], "first": t, "mc_first": p["mc"]})
            s[kind + "s"].append({"w": w, "t": t, "usd": round(usd, 2), "good": ws[w]["good"]})
            s["last"] = max(s.get("last", 0), t)
            log(f"{'KAUF' if kind == 'buy' else 'VERKAUF'} Smart-Wallet {w[:4]}…{w[-4:]} ${p['sym']} "
                f"{usd:.0f} $  CA: {m}")
        time.sleep(2.1)
    _save_buyers(buyers_log, now)
    r["seen"] = r["seen"][-SEEN_MAX:]
    cut = now - SIGNALS_KEEP_H * 3600
    r["signals"] = {m: s for m, s in r["signals"].items() if s.get("last", 0) >= cut}


def _load_buyers():
    try:
        with open(BUYERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _log_buyers(log_, mint, p, rows, now):
    """Erster Kauf (Zeit, Kurs) und Summe je Wallet für diesen Coin."""
    c = log_.setdefault(mint, {"sym": p["sym"], "b": {}})
    c["seen"] = now
    b = c["b"]
    for row in rows:
        a = row.get("attributes") or {}
        if a.get("to_token_address") != mint:
            continue
        usd = float(a.get("volume_in_usd") or 0)
        w = a.get("tx_from_address")
        if usd < BUYER_MIN_USD or not w:
            continue
        try:
            t = calendar.timegm(time.strptime(a["block_timestamp"], "%Y-%m-%dT%H:%M:%SZ"))
            price = float(a.get("price_to_in_usd") or 0)
        except Exception:
            continue
        if w in b:
            if t < b[w][0]:
                b[w][0], b[w][2] = t, price
            continue          # Summe nicht doppelt zählen (dieselben Trades kommen mehrfach)
        if len(b) >= BUYERS_PER_COIN and t >= max(x[0] for x in b.values()):
            continue
        b[w] = [t, round(usd), price]
        if len(b) > BUYERS_PER_COIN:
            del b[max(b, key=lambda k: b[k][0])]


def _save_buyers(log_, now):
    cut = now - BUYERS_KEEP_D * 86400
    log_ = {m: c for m, c in log_.items() if c.get("seen", now) >= cut}
    try:
        os.makedirs(os.path.dirname(BUYERS_FILE), exist_ok=True)
        tmp = BUYERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(log_, f, separators=(",", ":"))
        os.replace(tmp, BUYERS_FILE)
    except Exception as e:
        log(f"Käufer-Log: {e}")


def active_signals(meta):
    """Coins mit frischem Smart-Money-Signal: {mint: {sym, wallets, good, usd, t}}"""
    r, now = state(meta), time.time()
    out = {}
    for m, s in r["signals"].items():
        last_sell = {}
        for x in s.get("sells", []):
            last_sell[x["w"]] = max(last_sell.get(x["w"], 0), x["t"])
        # Wallets, die nach ihrem Kauf schon wieder verkauft haben, zählen nicht mehr
        recent = [b for b in s["buys"] if now - b["t"] <= SIGNAL_H * 3600 and last_sell.get(b["w"], 0) < b["t"]]
        if not recent:
            continue
        good = [b for b in recent if b["good"] and b["usd"] >= GOOD_MIN_USD]
        distinct = {b["w"] for b in recent}
        if good or len(distinct) >= 2:
            out[m] = {"sym": s["sym"], "wallets": sorted(distinct), "good": len({b["w"] for b in good}),
                      "usd": round(sum(b["usd"] for b in recent), 2), "t": max(b["t"] for b in recent)}
    return out


def sold_since(meta, mint, wallets_, since):
    """Hat eine der Wallets diesen Coin seit 'since' verkauft?"""
    s = state(meta)["signals"].get(mint) or {}
    return any(x["w"] in wallets_ and x["t"] >= since for x in s.get("sells", []))


def safety(meta, mint):
    """Contract- und Holder-Check aus dem Trichter (einmal pro Coin, 6 h gemerkt). Liefert (ok, Gründe)."""
    r, now = state(meta), time.time()
    hit = r["checked"].get(mint)
    if hit and now - hit["t"] < 6 * 3600:
        return hit["ok"], hit["why"]
    try:
        coins = funnel.market_data({mint: {"smart"}})
        c = coins[0] if coins else None
        if not c:
            why = ["keine Marktdaten"]
        else:
            rc = funnel.rugcheck_live(mint)
            why = funnel.stage_contract(c, rc) or funnel.stage_holder(c, rc)
    except Exception as e:
        why = [f"Prüfung fehlgeschlagen: {str(e)[:60]}"]
    r["checked"][mint] = {"ok": not why, "why": why, "t": now}
    r["checked"] = {m: v for m, v in r["checked"].items() if now - v["t"] < 24 * 3600}
    return not why, why
