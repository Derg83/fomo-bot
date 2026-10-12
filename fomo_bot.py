"""
Fomo-Bot – TESTMODUS (Paper Trading), drei Bots im Vergleich, mit Live-Dashboard
Handelt mit echten Live-Kursen, aber nur auf dem Papier. Kein Geld, keine Wallet, kein Schlüssel.

Mehrere Bots mit getrennten Papier-Konten (je 13 % pro Trade):
  "alle"     – jeder Coin, der den Trichter besteht (ab 6 h alt), TP +20 % / Stop -20 %
  "14tage"   – nur Coins ab 14 Tagen Alter, TP +20 % / Stop -20 %
  "tempo"    – wie "alle", aber TP +10 % / Stop -8 %, 5 Positionen, max. 6 h -> mehr Trades
  "trail"    – Hälfte bei +15 % verkaufen, Rest bis 10 % unter dem Hoch laufen lassen
  "qualitaet" – nur MC ab 1 Mio., Liquidität ab 10 % der MC, Top-10 unter 25 %, Einstieg nur mit Momentum,
                TP +20 % / Stop -30 % (viele spätere Gewinner fallen vorher 15-27 %)

Läuft auf zwei Arten:
  python fomo_bot.py           Dauerschleife auf deinem PC (jede Minute, Strg+C stoppt)
  python fomo_bot.py once      genau eine Runde (für GitHub Actions, alle 10 Min.)
  python fomo_bot.py report    Vergleich im Textfenster
  python fomo_bot.py reset     beide Papier-Konten zurücksetzen

Nach jeder Runde wird docs/index.html neu geschrieben = das Live-Dashboard.
memecoin_funnel.py muss im selben Ordner liegen.

Kein Kaufsignal und keine Garantie: Papier-Ergebnisse sind besser als echte.
"""

import csv
import html
import json
import os
import sys
import time
from datetime import datetime

import memecoin_funnel as funnel
import smart_radar as radar
import notify
import muster

# ======================= EINSTELLUNGEN =======================
# Jeder Bot kann einzelne Einstellungen von unten überschreiben.
BOTS = {
    "alle":   {"label": "Alle Coins",      "min_age_days": 0},
    "14tage": {"label": "Nur ab 14 Tagen", "min_age_days": 14},
    "tempo":  {"label": "Tempo",           "min_age_days": 0,
               "take_profit": 0.10, "stop_loss": 0.08, "max_open": 5, "max_hold_h": 6,
               "cooldown_h": 4, "entry_h1_min": -20, "entry_h1_max": 10},
    # Gewinner laufen lassen: Hälfte bei +15 % verkaufen, Rest bis 10 % unter dem Hoch
    "trail":  {"label": "Trailing",        "min_age_days": 0,
               "take_profit": 99, "stop_loss": 0.10, "max_hold_h": 72,
               "partial_tp": 0.15, "partial_part": 0.5, "trail_dist": 0.10},
    # Nur große, breit verteilte Coins mit Momentum; weiter Stop, weil viele Gewinner erst 15-27 % fallen
    "qualitaet": {"label": "Qualität + Momentum", "min_age_days": 0,
                  "take_profit": 0.20, "stop_loss": 0.30, "max_hold_h": 24,
                  "min_mc": 1_000_000, "min_liq_mc": 0.10, "max_top10": 25, "momentum": True},
    # Kauft sofort mit, wenn unsere Smart-Wallets einen kleinen Coin kaufen (Radar), verkauft, wenn sie verkaufen
    "smart":  {"label": "Smart-Money", "min_age_days": 0, "source": "smart", "max_open": 4,
               "min_mc": 100_000, "max_mc_entry": 1_500_000,
               "entry_h1_min": -50, "entry_h1_max": 100, "entry_m5_min": -100, "entry_max_24h": 1000,
               "take_profit": 99, "stop_loss": 0.30, "max_hold_h": 48,
               "partial_tp": 0.50, "partial_part": 0.5, "trail_dist": 0.25, "cooldown_h": 24},
    # Muster aus dem Rückblick: 60-92 % unter dem ersten Hoch, 1-14 Tage, MC 50k-1,5 Mio., Volumen ≥50 % MC, 1 h ≤ +3 %
    "muster": {"label": "Muster (Rückblick)", "min_age_days": 1, "source": "muster", "ignore_losers": True,
               "entry_h1_min": -20, "entry_h1_max": 3, "entry_m5_min": -100, "entry_max_24h": 400,
               "take_profit": 1.0, "stop_loss": 0.30, "max_hold_h": 48, "cooldown_h": 24},
}

# Warum kauft dieser Bot? (für die Push-Nachrichten, in einfachen Worten)
WHY = {
    "alle": "Coin hat alle Trichter-Regeln bestanden und sich nach einem Rücksetzer gefangen.",
    "14tage": "Wie 'Alle Coins', aber nur Coins, die schon älter als 14 Tage sind.",
    "tempo": "Schnelle Variante: kleine Ziele, enger Stop, viele Trades.",
    "trail": "Lässt Gewinner laufen: Hälfte bei +15 % verkaufen, Rest folgt dem Kurs nach oben.",
    "qualitaet": "Großer, breit verteilter Coin (ab 1 Mio. MC), Volumen steigt, mehr Käufe als Verkäufe.",
    "smart": "Eine unserer Smart-Wallets (Wallets mit Gewinn in ihren letzten Trades) hat gerade gekauft.",
    "muster": "Passt zum Gewinner-Muster aus dem Rückblick: 60-92 % unter dem ersten Hoch, wird noch gehandelt.",
}

START_BALANCE = 30.0        # Papier-Startkapital pro Bot in $
POSITION_PCT = 0.13         # Einsatz pro Trade: 13 % des Kontos
MAX_OPEN = 3                # höchstens so viele Positionen gleichzeitig (pro Bot)
FEE_SLIPPAGE = 0.01         # 1 % Gebühren + Slippage pro Kauf UND Verkauf

TAKE_PROFIT = 0.20          # +20 % -> verkaufen
STOP_LOSS = 0.20            # -20 % -> verkaufen
MAX_HOLD_H = 24             # nach 24 h raus, egal wie es steht
LIQ_DROP_EXIT = 0.50        # Liquidität -50 % seit Kauf -> sofort raus (Rug-Verdacht)
DEAD_AFTER_MIN = 120        # 2 h keine Kursdaten -> als Totalverlust werten

ENTRY_H1_MIN = -15          # letzte Stunde nicht schlechter als -15 %
ENTRY_H1_MAX = 5            # letzte Stunde nicht besser als +5 % (kein Reinjagen)
ENTRY_M5_MIN = 0            # letzte 5 Min. nicht mehr fallend
ENTRY_MAX_24H = 400         # heute nicht schon +400 % gelaufen

SCAN_EVERY_MIN = 20         # Trichter-Intervall
CHECK_EVERY_SEC = 60        # Kurs-Check-Intervall in der Dauerschleife
COOLDOWN_H = 24             # denselben Coin frühestens nach 24 h wieder kaufen
LOSS_COOLDOWN_H = 48        # nach einem Verlust: Coin 48 h gesperrt (gilt für alle Bots)
PARTIAL_TP = 0              # Teilverkauf ab +x (0 = aus)
PARTIAL_PART = 0.5          # so viel beim Teilverkauf verkaufen
TRAIL_DIST = 0              # nach dem Teilverkauf: raus, wenn x unter dem Hoch (0 = aus)
HISTORY_EVERY_MIN = 10      # Konto-Stand fürs Diagramm alle 10 Min. merken
AFTER_H = (1, 6, 24)        # Kurs so viele Stunden nach dem Verkauf mitschreiben (für Auswertungen)
HISTORY_MAX = 3000          # so viele Konto-Stände fürs Diagramm merken
MIN_MC = 0                  # Coin-Filter je Bot: Market Cap mindestens ($)
MIN_LIQ_MC = 0              # Liquidität mindestens x der Market Cap
MAX_TOP10 = 100             # Top-10-Holder höchstens x %
MAX_MC_ENTRY = 0            # Market Cap beim Kauf höchstens (0 = egal)
IGNORE_LOSERS = False       # Verlust-Sperre anderer Bots ignorieren
SOURCE = "trichter"         # woher die Kauf-Kandidaten kommen: "trichter" oder "smart" (Radar)
MOMENTUM = False            # Einstieg nur, wenn Volumen der letzten 6 h über dem Tagesschnitt und mehr Käufe als Verkäufe (1 h)
# =============================================================

HERE = os.path.dirname(os.path.abspath(__file__))
RADAR_META = {}   # Verweis auf meta der laufenden Runde (für den Radar)
LOSERS = {}   # CA -> gesperrt bis (gemeinsam für alle Bots, gespeichert in bot_meta.json)


def P(name, key):
    """Einstellung eines Bots: eigener Wert, sonst der globale von oben."""
    return BOTS[name].get(key, globals()[key.upper()])
META_FILE = os.path.join(HERE, "bot_meta.json")
DASHBOARD = os.path.join(HERE, "docs", "index.html")


def state_file(name):
    return os.path.join(HERE, f"bot_state_{name}.json")


def journal_file(name):
    return os.path.join(HERE, f"bot_journal_{name}.csv")


def berlin(ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo("Europe/Berlin"))
    except Exception:
        return datetime.fromtimestamp(ts)


def log(msg):
    print(f"[{berlin(time.time()):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- Zustand
def new_state():
    return {"cash": START_BALANCE, "open": {}, "closed": [], "watch": {},
            "cooldown": {}, "started": time.time()}


def new_meta():
    return {"last_scan": 0, "counts": None, "history": [], "last_run": 0}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default()


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load_all():
    return ({n: load_json(state_file(n), new_state) for n in BOTS},
            load_json(META_FILE, new_meta))


def save_all(states, meta):
    for n, s in states.items():
        save_json(state_file(n), s)
    save_json(META_FILE, meta)


# ---------------------------------------------------------------- Kurse
def get_prices(addrs):
    out = {}
    addrs = list(addrs)
    for i in range(0, len(addrs), 30):
        try:
            pairs = funnel.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(addrs[i:i + 30]))
        except Exception as e:
            log(f"Kursabfrage fehlgeschlagen: {e}")
            continue
        for p in pairs:
            a = (p.get("baseToken") or {}).get("address")
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if a not in addrs or (a in out and out[a]["liq"] >= liq):
                continue
            ch = p.get("priceChange") or {}
            try:
                price = float(p.get("priceUsd") or 0)
            except ValueError:
                price = 0
            if price > 0:
                vol = p.get("volume") or {}
                tx1 = (p.get("txns") or {}).get("h1") or {}
                out[a] = {"price": price, "liq": liq, "m5": ch.get("m5") or 0,
                          "h1": ch.get("h1") or 0, "h24": ch.get("h24") or 0,
                          "mc": p.get("marketCap") or p.get("fdv") or 0,
                          "v6": vol.get("h6") or 0, "v24": vol.get("h24") or 0,
                          "b1": tx1.get("buys") or 0, "s1": tx1.get("sells") or 0}
    return out


# ---------------------------------------------------------------- Trichter
def scan(meta):
    log("Trichter läuft ...")
    try:
        src = funnel.collect_candidates()
        coins = funnel.market_data(src)
        try:
            radar.update_pools(meta, coins)
        except Exception as e:
            log(f"Radar-Liste: {e}")
        remember_seen(coins)
        counts, final, watch, allc = funnel.run_funnel(coins, funnel.rugcheck_live)
    except Exception as e:
        log(f"Trichter-Fehler: {e}")
        return None
    meta["counts"] = counts
    log(f"Trichter: {counts['Eingang']} rein, {len(final)} durch, {len(watch)} Watchlist")
    out = [{"addr": c["addr"], "sym": c["sym"], "age_d": c["age_h"] / 24, "mc": c["mc"],
            "cluster": c.get("cluster"), "liq": c.get("liq") or 0, "top10": c.get("top10")}
           for c in final + watch]
    if any(P(n, "source") == "muster" for n in BOTS):
        try:
            out += [dict(m, only_muster=True) for m in muster.candidates(allc, log)]
        except Exception as e:
            log(f"Muster-Fehler: {e}")
    return out


SEEN_FILE = os.path.join(HERE, "rueckblick", "gesehen.json")


def remember_seen(coins):
    """Jeden Coin, den der Trichter je gesehen hat, mit Zeitpunkt merken (für den Rückblick ohne Gewinner-Verzerrung)."""
    try:
        seen = load_json(SEEN_FILE, dict)
        now = int(time.time())
        new = 0
        for c in coins:
            if c["addr"] not in seen:
                seen[c["addr"]] = {"t": now, "mc": round(c.get("mc") or 0), "src": "trichter"}
                new += 1
        if new:
            os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
            save_json(SEEN_FILE, seen)
    except Exception as e:
        log(f"Gesehen-Liste: {e}")


def update_watch(name, state, candidates):
    if P(name, "source") == "smart":
        return      # Kandidaten kommen vom Radar, nicht vom Trichter
    now = time.time()
    state["watch"] = {}
    is_muster = P(name, "source") == "muster"
    for c in candidates:
        a = c["addr"]
        if bool(c.get("only_muster")) != is_muster:
            continue      # Muster-Kandidaten nur für den Muster-Bot, Trichter-Kandidaten nur für die anderen
        if c["age_d"] < BOTS[name]["min_age_days"]:
            continue
        if a in state["open"] or state["cooldown"].get(a, 0) > now or (LOSERS.get(a, 0) > now and not P(name, "ignore_losers")):
            continue
        if c["mc"] < P(name, "min_mc") or c.get("liq", 0) < c["mc"] * P(name, "min_liq_mc"):
            continue
        if P(name, "max_top10") < 100 and (c.get("top10") is None or c["top10"] > P(name, "max_top10")):
            continue
        state["watch"][a] = {"sym": c["sym"], "age_d": round(c["age_d"], 1), "mc": c["mc"],
                             "cluster": c.get("cluster")}
        if c.get("muster"):
            state["watch"][a]["muster"] = c["muster"]


# ---------------------------------------------------------------- Handel (Papier)
def equity(state, prices):
    value = sum(p["qty"] * prices.get(a, {}).get("price", p.get("last_price", p["entry_price"]))
                for a, p in state["open"].items())
    return state["cash"] + value


def try_entries(name, state, prices):
    for a, w in list(state["watch"].items()):
        if len(state["open"]) >= P(name, "max_open"):
            return
        q = prices.get(a)
        if not q:
            continue
        if not (P(name, "entry_h1_min") <= q["h1"] <= P(name, "entry_h1_max")
                and q["m5"] >= P(name, "entry_m5_min") and q["h24"] < P(name, "entry_max_24h")):
            continue
        if P(name, "source") == "smart" and not (P(name, "min_mc") <= q["mc"] <= (P(name, "max_mc_entry") or 1e18)):
            continue
        if P(name, "momentum") and not (q.get("v6", 0) / 6 > q.get("v24", 0) / 24 and q.get("b1", 0) > q.get("s1", 0)):
            continue
        stake = min(state["cash"], equity(state, prices) * P(name, "position_pct"))
        if stake < 1:
            return
        fill = q["price"] * (1 + FEE_SLIPPAGE)
        state["cash"] -= stake
        state["open"][a] = {"sym": w["sym"], "entry_price": q["price"], "qty": stake / fill,
                            "stake": stake, "entry_time": time.time(), "entry_liq": q["liq"],
                            "entry_mc": q["mc"], "age_d": w.get("age_d"), "last_seen": time.time(),
                            "last_price": q["price"], "peak": q["price"], "trough": q["price"],
                            "realized": 0.0, "partial": False}
        if w.get("wallets"):
            state["open"][a]["trigger"] = w["wallets"]
        del state["watch"][a]
        log(f"[{name}] KAUF (Papier) ${w['sym']} für {stake:.2f} $ bei MC {funnel.fmt(q['mc'])}  CA: {a}")
        notify.push(f"🟢 KAUF · {BOTS[name]['label']} · ${w['sym']}",
                    f"Der Bot hat ${w['sym']} auf dem Papier gekauft (kein echtes Geld) bei MC {funnel.fmt(q['mc'])}.\n"
                    f"Warum: {WHY.get(name, '')}\n"
                    f"Plan: {plan_text(name)}\n"
                    f"Mitmachen nur mit kleinem Einsatz und denselben Regeln. Tippen = Chart.\nCA: {a}",
                    click=notify.dex(a), priority=4 if P(name, "source") in ("smart", "muster") else 3, tags=["moneybag"])


def close(name, state, a, price, reason):
    p = state["open"].pop(a)
    proceeds = p["qty"] * price * (1 - FEE_SLIPPAGE) + p.get("realized", 0.0)
    state["cash"] += p["qty"] * price * (1 - FEE_SLIPPAGE)
    pnl = proceeds - p["stake"]
    trade = {"sym": p["sym"], "addr": a, "entry_time": p["entry_time"], "exit_time": time.time(),
             "entry_price": p["entry_price"], "exit_price": price, "stake": round(p["stake"], 4),
             "proceeds": round(proceeds, 4), "pnl": round(pnl, 4),
             "pnl_pct": round(pnl / p["stake"] * 100, 2), "reason": reason,
             "hold_h": round((time.time() - p["entry_time"]) / 3600, 2), "age_d": p.get("age_d"),
             "entry_mc": p.get("entry_mc"),
             # bester / schlechtester Stand während des Trades (für Auswertungen anderer TP/Stop-Werte)
             "max_pct": round((p.get("peak", price) / p["entry_price"] - 1) * 100, 2),
             "min_pct": round((p.get("trough", price) / p["entry_price"] - 1) * 100, 2),
             "partial": p.get("partial", False), "after": {}}
    state["closed"].append(trade)
    cd = LOSS_COOLDOWN_H if pnl < 0 else P(name, "cooldown_h")
    state["cooldown"][a] = max(state["cooldown"].get(a, 0), time.time() + cd * 3600)
    if pnl < 0:   # Verlierer für ALLE Bots sperren
        LOSERS[a] = time.time() + LOSS_COOLDOWN_H * 3600
    write_journal(name, trade)
    log(f"[{name}] VERKAUF (Papier) ${p['sym']}: {reason} -> {pnl:+.2f} $ ({trade['pnl_pct']:+.1f} %)  CA: {a}")
    notify.push(f"{'✅' if pnl > 0 else '🔴'} VERKAUF · {BOTS[name]['label']} · ${p['sym']} {trade['pnl_pct']:+.1f} %",
                f"Der Bot hat ${p['sym']} verkauft: {trade['pnl_pct']:+.1f} % nach Gebühren ({pnl:+.2f} $ auf dem Papier).\n"
                f"Grund: {reason}\n"
                f"Wenn du mitgemacht hast: jetzt ebenfalls verkaufen.\nCA: {a}",
                click=notify.dex(a), priority=3, tags=["chart_with_upwards_trend" if pnl > 0 else "chart_with_downwards_trend"])


def manage_exits(name, state, prices):
    now = time.time()
    for a, p in list(state["open"].items()):
        q = prices.get(a)
        if not q:
            if now - p["last_seen"] > DEAD_AFTER_MIN * 60:
                close(name, state, a, 0.0, "keine Kursdaten mehr (tot/gerugged)")
            continue
        p["last_seen"], p["last_price"] = now, q["price"]
        p["peak"] = max(p.get("peak", p["entry_price"]), q["price"])
        p["trough"] = min(p.get("trough", p["entry_price"]), q["price"])
        change = q["price"] / p["entry_price"] - 1
        ptp, tdist = P(name, "partial_tp"), P(name, "trail_dist")
        if ptp and not p.get("partial") and change >= ptp and q["liq"] >= p["entry_liq"] * (1 - P(name, "liq_drop_exit")):
            sell = p["qty"] * P(name, "partial_part")
            got = sell * q["price"] * (1 - FEE_SLIPPAGE)
            p["qty"] -= sell
            p["realized"] = p.get("realized", 0.0) + got
            state["cash"] += got
            p["partial"] = True
            log(f"[{name}] TEILVERKAUF (Papier) ${p['sym']} bei +{change * 100:.0f} %  CA: {a}")
            notify.push(f"💰 TEILVERKAUF · {BOTS[name]['label']} · ${p['sym']} +{change * 100:.0f} %",
                        f"Der Bot hat {P(name, 'partial_part') * 100:.0f} % seiner Position bei +{change * 100:.0f} % verkauft "
                        f"und den Gewinn gesichert. Der Rest läuft weiter, bis der Kurs {P(name, 'trail_dist') * 100:.0f} % unter sein Hoch fällt.\n"
                        f"Wenn du mitgemacht hast: jetzt ebenfalls einen Teil verkaufen.\nCA: {a}",
                        click=notify.dex(a), priority=3, tags=["moneybag"])
            continue
        if q["liq"] < p["entry_liq"] * (1 - P(name, "liq_drop_exit")):
            close(name, state, a, q["price"], "Liquidität abgezogen (Rug-Verdacht)")
        elif p.get("trigger") and radar.sold_since(RADAR_META, a, p["trigger"], p["entry_time"] - 120):
            close(name, state, a, q["price"], f"Smart-Wallet verkauft ({change * 100:+.0f} %)")
        elif p.get("partial") and tdist and q["price"] <= p["peak"] * (1 - tdist):
            close(name, state, a, q["price"],
                  f"Trailing ({(p['peak'] / p['entry_price'] - 1) * 100:+.0f} % Hoch, raus bei {change * 100:+.0f} %)")
        elif change >= P(name, "take_profit"):
            close(name, state, a, q["price"], f"Take-Profit (+{change * 100:.0f} %)")
        elif change <= -P(name, "stop_loss"):
            close(name, state, a, q["price"], f"Stop-Loss ({change * 100:.0f} %)")
        elif now - p["entry_time"] > P(name, "max_hold_h") * 3600:
            close(name, state, a, q["price"], f"Zeitlimit {P(name, 'max_hold_h')} h ({change * 100:+.0f} %)")


def cycle(states, meta):
    """Eine komplette Runde: ggf. Trichter, Kurse, Ausstiege, Einstiege, Verlauf."""
    now = time.time()
    LOSERS.clear()
    if not meta.get("losers_seeded"):    # einmalig: Verluste von vor der Sperr-Regel nachtragen
        meta["losers_seeded"] = True
        for st in states.values():
            for t in st["closed"]:
                if t["pnl"] < 0:
                    LOSERS[t["addr"]] = max(LOSERS.get(t["addr"], 0), t["exit_time"] + LOSS_COOLDOWN_H * 3600)
    LOSERS.update({a: t for a, t in (meta.get("losers") or {}).items() if t > now})
    if time.time() - meta["last_scan"] >= SCAN_EVERY_MIN * 60:
        candidates = scan(meta)
        if candidates is not None:
            for n, s in states.items():
                update_watch(n, s, candidates)
            meta["last_scan"] = time.time()
    global RADAR_META
    RADAR_META = meta
    smart = [n for n in states if P(n, "source") == "smart"]
    if smart:
        try:
            prio = {a for n in smart for a in states[n]["open"]}
            radar.tick(meta, priority=sorted(prio))
            sig = radar.active_signals(meta)
            told = meta.setdefault("radar", {}).setdefault("notified", {})
            for a, v in sig.items():
                if now - told.get(a, 0) > 24 * 3600:
                    told[a] = now
                    mc = (meta["radar"]["pools"].get(a) or {}).get("mc") or 0
                    good_txt = f" (davon {v['good']} mit sehr guter Bilanz)" if v["good"] else ""
                    notify.push(f"👀 SIGNAL · Smart-Wallet kauft ${v['sym']} (MC ca. {funnel.fmt(mc)})",
                                f"{len(v['wallets'])} unserer Smart-Wallets haben gerade zusammen {v['usd']:.0f} $ in ${v['sym']} gekauft"
                                f"{good_txt}.\n"
                                f"Das ist erst ein Hinweis, noch kein Kauf: Der Smart-Money-Bot kauft nur, wenn auch der "
                                f"Sicherheits-Check (Contract, Holder, Insider) besteht. Dann kommt eine KAUF-Nachricht.\nCA: {a}",
                                click=notify.dex(a), priority=4, tags=["eyes"])
            meta["radar"]["notified"] = {a: t for a, t in told.items() if now - t < 48 * 3600}
            for n in smart:
                s = states[n]
                s["watch"] = {a: {"sym": v["sym"], "age_d": None, "mc": None, "wallets": v["wallets"],
                                  "good": v["good"], "usd": v["usd"]}
                              for a, v in sig.items()
                              if a not in s["open"] and s["cooldown"].get(a, 0) <= now and LOSERS.get(a, 0) <= now}
        except Exception as e:
            log(f"Radar-Fehler: {e}")
    addrs = set()
    for s in states.values():
        addrs |= set(s["open"]) | set(s["watch"])
        addrs |= {t["addr"] for t in s["closed"] if pending_after(t, now)}
    prices = get_prices(addrs) if addrs else {}
    for n in smart:      # Sicherheits-Check nur für Signal-Coins, deren MC passt (spart RugCheck-Abfragen)
        s = states[n]
        for a in list(s["watch"]):
            q = prices.get(a)
            if not q or not (P(n, "min_mc") <= q["mc"] <= (P(n, "max_mc_entry") or 1e18)):
                continue
            ok, why = radar.safety(meta, a)
            if not ok:
                del s["watch"][a]
    for n, s in states.items():
        manage_exits(n, s, prices)
        # Verlierer, die ein anderer Bot gerade gesperrt hat, nicht mehr kaufen
        if not P(n, "ignore_losers"):
            s["watch"] = {a: w for a, w in s["watch"].items() if LOSERS.get(a, 0) <= now}
        try_entries(n, s, prices)
        track_after(s, prices, now)
    meta["losers"] = dict(LOSERS)
    meta["last_run"] = time.time()
    hist = meta["history"]
    if not hist or time.time() - hist[-1][0] >= HISTORY_EVERY_MIN * 60 - 30:
        hist.append([int(time.time())] + [round(equity(states[n], prices), 4) for n in BOTS])
    meta["history"] = hist[-HISTORY_MAX:]
    return prices


def pending_after(t, now):
    """Wird für diesen Trade noch ein Kurs nach dem Verkauf gebraucht?"""
    if "after" not in t:
        return False
    return any(str(h) not in t["after"] and now - t["exit_time"] >= h * 3600 - 120 for h in AFTER_H) \
        and now - t["exit_time"] <= max(AFTER_H) * 3600 + 7200


def track_after(state, prices, now):
    """Kurs 1 / 6 / 24 h nach dem Verkauf mitschreiben: War der Ausstieg zu früh oder genau richtig?"""
    for t in state["closed"]:
        if "after" not in t or not t.get("exit_price"):
            continue
        q = prices.get(t["addr"])
        for h in AFTER_H:
            k = str(h)
            if k in t["after"] or now - t["exit_time"] < h * 3600:
                continue
            if q:
                t["after"][k] = round((q["price"] / t["exit_price"] - 1) * 100, 1)
            elif now - t["exit_time"] > h * 3600 + 7200:
                t["after"][k] = None     # keine Kursdaten mehr (Coin tot)


# ---------------------------------------------------------------- Journal
JOURNAL_FIELDS = ["Kauf", "Verkauf", "Coin", "CA", "Alter (Tage)", "Einstiegskurs", "Ausstiegskurs",
                  "Einsatz $", "Erlös $", "Gewinn $", "Gewinn %", "Grund", "Haltedauer h"]


def de(x, digits=2):
    return f"{x:.{digits}f}".replace(".", ",")


def write_journal(name, t):
    path = journal_file(name)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(JOURNAL_FIELDS)
        w.writerow([berlin(t["entry_time"]).strftime("%d.%m.%Y %H:%M"),
                    berlin(t["exit_time"]).strftime("%d.%m.%Y %H:%M"),
                    t["sym"], t["addr"], de(t["age_d"] or 0, 1),
                    f"{t['entry_price']:.10g}", f"{t['exit_price']:.10g}",
                    de(t["stake"]), de(t["proceeds"]), de(t["pnl"]), de(t["pnl_pct"], 1),
                    t["reason"], de(t["hold_h"], 1)])


# ---------------------------------------------------------------- Auswertung
def summary(state, prices):
    closed = state["closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    eq = equity(state, prices)
    return {"equity": eq, "ret": (eq / START_BALANCE - 1) * 100, "n": len(closed),
            "rate": (len(wins) / len(closed) * 100) if closed else None,
            "pnl": sum(t["pnl"] for t in closed), "open": len(state["open"]),
            "avg_win": (sum(t["pnl_pct"] for t in wins) / len(wins)) if wins else None,
            "avg_loss": (sum(t["pnl_pct"] for t in closed if t["pnl"] <= 0) /
                         max(1, len([t for t in closed if t["pnl"] <= 0]))) if len(wins) < len(closed) else None}


def report(states):
    addrs = set()
    for s in states.values():
        addrs |= set(s["open"])
    prices = get_prices(addrs) if addrs else {}
    print("\n" + "=" * 64)
    print(f"  PAPIER-VERGLEICH ({POSITION_PCT * 100:.0f} % pro Trade)")
    print("=" * 64)
    print(f"  {'Bot':<22}{'Konto':>10}{'Rendite':>10}{'Trades':>8}{'Treffer':>9}")
    for n, s in states.items():
        x = summary(s, prices)
        rate = f"{x['rate']:.0f} %" if x["rate"] is not None else "–"
        print(f"  {BOTS[n]['label']:<22}{x['equity']:>8.2f} ${x['ret']:>+8.1f} %{x['n']:>8}{rate:>9}")
        for a, p in s["open"].items():
            q = prices.get(a)
            txt = f"{(q['price'] / p['entry_price'] - 1) * 100:+.1f} %" if q else "?"
            print(f"     offen: ${p['sym']} {txt}  CA: {a}")
    print("  (bei +/-20 % und 1 % Gebühren braucht man ca. 56 % Treffer)\n")


# ---------------------------------------------------------------- Dashboard
CSS = """
:root{--bg:#f6f7f9;--card:#ffffff;--fg:#14171c;--muted:#5c6470;--line:#e2e5ea;--a:#2f6fd6;--b:#d9822b;--c:#8a4fc7;--d:#13897c;--e:#c2417a;--f:#5f7d1f;--g:#8c6d1f;
--good:#1f8a5b;--bad:#c4413b;--chip:#eef1f5;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--fg:#e8eaed;--muted:#99a1ad;--line:#2a3039;
--a:#6ea2ff;--b:#f0a35a;--c:#b98af0;--d:#3cc7b6;--e:#f07aa8;--f:#a6cf5a;--g:#e0b84f;--good:#4cc38a;--bad:#ef6b63;--chip:#222831;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:16px}
h1{font-size:22px;margin:0}h2{font-size:17px;margin:28px 0 10px}h3{font-size:15px;margin:0 0 8px}
.muted{color:var(--muted)}.small{font-size:13px}.stale{color:var(--bad);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.big{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums}
.pos{color:var(--good)}.neg{color:var(--bad)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-variant-numeric:tabular-nums;margin-top:6px}
.kv span:nth-child(odd){color:var(--muted)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:baseline}
.chart{width:100%;height:auto;display:block}
.chart text{fill:var(--muted);font-size:11px}.chart .grid line{stroke:var(--line)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px}
code{font-size:12px;word-break:break-all;color:var(--muted)}
.chip{display:inline-block;background:var(--chip);border-radius:99px;padding:1px 9px;font-size:12px;margin:2px 4px 2px 0}
.funnel div{display:flex;align-items:center;gap:10px;margin:4px 0}
.funnel b{width:80px;font-weight:600;font-size:13px}.funnel i{display:block;height:18px;border-radius:4px;background:var(--a);opacity:.85}
.run{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:0 0 16px}
.btn{font:inherit;font-weight:600;border:0;border-radius:8px;padding:8px 14px;background:var(--a);color:#fff;cursor:pointer}
.btn:focus-visible,.link:focus-visible,input:focus-visible{outline:2px solid var(--fg);outline-offset:2px}
.link{font:inherit;font-size:13px;background:none;border:0;color:var(--muted);text-decoration:underline;cursor:pointer;padding:0}
#tokform{display:flex;flex-wrap:wrap;gap:8px;align-items:center;width:100%}
#tokform[hidden]{display:none}
#tokform input{font:inherit;flex:1 1 220px;min-width:0;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
a{color:var(--a)}footer{margin-top:32px;color:var(--muted);font-size:13px}
"""


RUN_JS = """
(function(){
var box=document.getElementById('runbox');
var form=document.getElementById('tokform');
setInterval(function(){ if(!form||form.hidden){ location.reload(); } }, 60000);
if(!box) return;
var repo=box.dataset.repo, KEY='fomo_gh_token';
var btn=document.getElementById('runbtn'), msg=document.getElementById('runmsg'), inp=document.getElementById('tokinp');
function get(){try{return localStorage.getItem(KEY);}catch(e){return null;}}
function set(v){try{localStorage.setItem(KEY,v);}catch(e){}}
function run(tok){
  msg.textContent='Starte ...';
  fetch('https://api.github.com/repos/'+repo+'/actions/workflows/bot.yml/dispatches',{method:'POST',
    headers:{'Authorization':'Bearer '+tok,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'},
    body:JSON.stringify({ref:'main'})})
  .then(function(r){
    if(r.status===204){ msg.textContent='Runde gestartet. In 2 bis 3 Minuten aktualisiert sich die Seite von selbst.'; }
    else if(r.status===401||r.status===403||r.status===404){ msg.textContent='Token ungueltig oder ohne Recht "Actions: Read and write" (Fehler '+r.status+'). Bitte neuen Token eintragen.'; form.hidden=false; }
    else { msg.textContent='GitHub meldet Fehler '+r.status+'. Starte die Runde notfalls ueber "Auf GitHub oeffnen".'; }
  })
  .catch(function(){ msg.textContent='Keine Verbindung zu GitHub.'; });
}
btn.addEventListener('click',function(){ var t=get(); if(t){ run(t); } else { form.hidden=false; inp.focus(); } });
form.addEventListener('submit',function(e){ e.preventDefault(); var t=inp.value.trim(); if(!t) return; set(t); inp.value=''; form.hidden=true; run(t); });
document.getElementById('tokdel').addEventListener('click',function(){ try{localStorage.removeItem(KEY);}catch(e){} msg.textContent='Token auf diesem Geraet geloescht.'; });
})();
"""


def run_box():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return ""
    r = html.escape(repo)
    return (f'<div class="run" id="runbox" data-repo="{r}">'
            f'<button class="btn" id="runbtn" type="button">Runde jetzt starten</button>'
            f'<a class="small" href="https://github.com/{r}/actions/workflows/bot.yml">Auf GitHub öffnen</a>'
            f'<button class="link" id="tokdel" type="button">Token vergessen</button>'
            f'<span id="runmsg" class="small muted" aria-live="polite"></span>'
            f'<form id="tokform" hidden><label class="small muted" for="tokinp">GitHub-Token, wird nur auf diesem Gerät gespeichert</label>'
            f'<input id="tokinp" type="password" autocomplete="off" spellcheck="false" placeholder="github_pat_...">'
            f'<button class="btn" type="submit">Speichern und starten</button>'
            f'<a class="small" href="https://github.com/settings/personal-access-tokens/new">Token erstellen</a></form></div>')


def pct(x, digits=1):
    if x is None:
        return "–"
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f'<span class="{cls}">{x:+.{digits}f} %</span>'


def equity_chart(meta):
    hist = meta.get("history") or []
    if len(hist) < 2:
        return '<p class="muted small">Das Diagramm erscheint nach den ersten Runden.</p>'
    W, H, L, R, T, B = 640, 200, 48, 12, 12, 26
    xs = [h[0] for h in hist]
    vals = [v for h in hist for v in h[1:]] + [START_BALANCE]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1:
        lo, hi = lo - 0.5, hi + 0.5
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    x0, x1 = xs[0], max(xs[-1], xs[0] + 1)

    def X(t):
        return L + (t - x0) / (x1 - x0) * (W - L - R)

    def Y(v):
        return T + (hi - v) / (hi - lo) * (H - T - B)

    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Kontoverlauf aller Bots">']
    parts.append('<g class="grid">')
    drawn = []
    for v in (START_BALANCE, hi - pad, lo + pad):
        if any(abs(Y(v) - y) < 16 for y in drawn):
            continue
        drawn.append(Y(v))
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                     f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:.2f} $</text>')
    parts.append('</g>')
    parts.append(f'<text x="{L}" y="{H - 6}">{berlin(x0):%d.%m. %H:%M}</text>'
                 f'<text x="{W - R}" y="{H - 6}" text-anchor="end">{berlin(x1):%d.%m. %H:%M}</text>')
    colors = ["var(--a)", "var(--b)", "var(--c)", "var(--d)", "var(--e)", "var(--f)", "var(--g)"]
    for i, _ in enumerate(BOTS):
        pts = " ".join(f"{X(h[0]):.1f},{Y(h[i + 1]):.1f}" for h in hist if len(h) > i + 1)
        parts.append(f'<polyline fill="none" stroke="{colors[i % 7]}" stroke-width="2" points="{pts}"/>')
        last = hist[-1]
        parts.append(f'<circle cx="{X(last[0]):.1f}" cy="{Y(last[i + 1]):.1f}" r="3.5" fill="{colors[i % 7]}"/>')
    parts.append('</svg>')
    return "".join(parts)


def coin_links(a):
    return (f'<a href="https://dexscreener.com/solana/{a}">Chart</a> · '
            f'<a href="https://rugcheck.xyz/tokens/{a}">RugCheck</a> · '
            f'<a href="https://trench.bot/bundles/{a}">Trench</a> · '
            f'<a href="https://app.bubblemaps.io/sol/token/{a}">Bubblemaps</a>')


def trade_extra(t):
    """Hoch/Tief während des Trades und Kurs nach dem Verkauf (nur bei neuen Trades vorhanden)."""
    bits = []
    if "max_pct" in t:
        bits.append(f'Hoch {t["max_pct"]:+.0f} % · Tief {t["min_pct"]:+.0f} %')
    aft = [f'{h} h danach {t["after"][str(h)]:+.0f} %' for h in AFTER_H
           if t.get("after", {}).get(str(h)) is not None]
    if aft:
        bits.append(" · ".join(aft))
    return "".join(f'<br><span class="muted">{b}</span>' for b in bits)


def plan_text(n):
    """Ausstiegsregeln eines Bots in einem Satz."""
    stop = f"Stop bei -{P(n, 'stop_loss') * 100:.0f} %"
    if P(n, "partial_tp"):
        tp = (f"Hälfte bei +{P(n, 'partial_tp') * 100:.0f} % verkaufen, Rest bis {P(n, 'trail_dist') * 100:.0f} % "
              f"unter dem Hoch laufen lassen")
    else:
        tp = f"verkaufen bei +{P(n, 'take_profit') * 100:.0f} %"
    extra = ", oder sobald die Smart-Wallet verkauft" if P(n, "source") == "smart" else ""
    return f"{tp}, {stop}{extra}, spätestens nach {P(n, 'max_hold_h')} h."


def exit_rule(n):
    if P(n, "partial_tp"):
        return (f'{P(n, "partial_part") * 100:.0f} % bei +{P(n, "partial_tp") * 100:.0f} %, '
                f'Rest bis {P(n, "trail_dist") * 100:.0f} % unter Hoch')
    return f'TP +{P(n, "take_profit") * 100:.0f} %'


def bot_filters(n):
    bits = []
    if P(n, "min_mc"):
        bits.append(f'MC ab {funnel.fmt(P(n, "min_mc"))}')
    if P(n, "min_liq_mc"):
        bits.append(f'Liq. ab {P(n, "min_liq_mc") * 100:.0f} % der MC')
    if P(n, "max_top10") < 100:
        bits.append(f'Top-10 unter {P(n, "max_top10")} %')
    if P(n, "momentum"):
        bits.append('Volumen steigt + mehr Käufe als Verkäufe')
    return "".join(" · " + b for b in bits)


def write_dashboard(states, meta, prices):
    now = time.time()
    colors = ["var(--a)", "var(--b)", "var(--c)", "var(--d)", "var(--e)", "var(--f)", "var(--g)"]
    cards = []
    for i, (n, s) in enumerate(states.items()):
        x = summary(s, prices)
        rate = f"{x['rate']:.0f} %" if x["rate"] is not None else "–"
        cards.append(
            f'<div class="card"><h3><span class="dot" style="background:{colors[i % 7]}"></span>{html.escape(BOTS[n]["label"])}</h3>'
            f'<div class="big">{x["equity"]:.2f} $</div><div>{pct(x["ret"])} seit Start ({START_BALANCE:.0f} $)</div>'
            f'<div class="kv"><span>Trades</span><span>{x["n"]}</span>'
            f'<span>Trefferquote</span><span>{rate} <span class="muted small">(Ziel über 56 %)</span></span>'
            f'<span>Ø Gewinner</span><span>{pct(x["avg_win"])}</span>'
            f'<span>Ø Verlierer</span><span>{pct(x["avg_loss"])}</span>'
            f'<span>Offen</span><span>{x["open"]} von {P(n, "max_open")}</span></div>'
            f'<p class="small muted">{exit_rule(n)} · Stop -{P(n, "stop_loss") * 100:.0f} % · '
            f'max. {P(n, "max_hold_h")} h · ab {BOTS[n]["min_age_days"]} Tagen{bot_filters(n)}</p></div>')

    open_rows, closed_rows, watch_html = [], [], []
    for n, s in states.items():
        for a, p in s["open"].items():
            q = prices.get(a)
            cur = (q["price"] / p["entry_price"] - 1) * 100 if q else None
            open_rows.append(
                f'<tr><td>{html.escape(BOTS[n]["label"])}</td><td><b>${html.escape(p["sym"])}</b><br><code>{a}</code><br>'
                f'<span class="small">{coin_links(a)}</span></td><td>{berlin(p["entry_time"]):%d.%m. %H:%M}</td>'
                f'<td>{funnel.fmt(p.get("entry_mc") or 0)}</td><td>{p["stake"]:.2f} $</td><td>{pct(cur)}</td></tr>')
        for t in s["closed"]:
            closed_rows.append((t["exit_time"],
                f'<tr><td>{html.escape(BOTS[n]["label"])}</td><td><b>${html.escape(t["sym"])}</b><br><code>{t["addr"]}</code></td>'
                f'<td>{berlin(t["exit_time"]):%d.%m. %H:%M}</td><td>{pct(t["pnl_pct"])}<br>'
                f'<span class="small muted">{t["pnl"]:+.2f} $</span></td><td class="small">{html.escape(t["reason"])}{trade_extra(t)}</td></tr>'))
        chips = "".join(f'<div><span class="chip">${html.escape(w["sym"])}</span>'
                        f'<span class="small muted">'
                        f'{(str(len(w["wallets"])) + " Smart-Wallets · " + str(w.get("usd", 0)) + " $ gekauft") if w.get("wallets") else (str(w.get("age_d", "?")) + " Tage · MC " + funnel.fmt(w.get("mc") or 0))}'
                        f'{" · Cluster " + format(w["cluster"], ".1f") + " %" if w.get("cluster") is not None else ""}</span>'
                        f'<br><code>{a}</code><br><span class="small">{coin_links(a)}</span></div>'
                        for a, w in s["watch"].items())
        watch_html.append(f'<div class="card"><h3>{html.escape(BOTS[n]["label"])}</h3>'
                          f'{chips or "<p class=muted>Gerade nichts, was alle Regeln besteht.</p>"}</div>')
    closed_rows = [r for _, r in sorted(closed_rows, reverse=True)[:20]]

    radar_rows = []
    rs = (meta.get("radar") or {})
    for m, sg in sorted((rs.get("signals") or {}).items(), key=lambda x: -x[1].get("last", 0))[:15]:
        buys, sells = sg.get("buys", []), sg.get("sells", [])
        good = len({b["w"] for b in buys if b.get("good")})
        radar_rows.append(
            f'<tr><td><b>${html.escape(sg.get("sym", "?"))}</b><br><code>{m}</code><br><span class="small">{coin_links(m)}</span></td>'
            f'<td>{len({b["w"] for b in buys})} Wallets ({good} gute)<br><span class="small muted">{sum(b["usd"] for b in buys):.0f} $</span></td>'
            f'<td>{len({x["w"] for x in sells})}</td><td>{funnel.fmt(sg.get("mc_first") or 0)}</td>'
            f'<td>{berlin(sg.get("last", now)):%d.%m. %H:%M}</td></tr>')
    radar_html = (f'<p class="small muted">{len(rs.get("pools") or {})} kleine Coins unter Beobachtung · '
                  f'{len(radar.wallets())} Pool-Wallets</p>')
    radar_html += ('<table><tr><th>Coin</th><th>Gekauft von</th><th>Verkauft von</th><th>MC beim 1. Kauf</th><th>Zuletzt</th></tr>'
                   + "".join(radar_rows) + '</table>') if radar_rows else '<p class="muted">Noch keine Käufe unserer Wallets gesehen.</p>'

    counts = meta.get("counts") or {}
    funnel_html = ""
    if counts:
        top = max(counts.get("Eingang", 1), 1)
        for k in funnel.STAGES:
            v = counts.get(k, 0)
            funnel_html += f'<div><b>{k}</b><i style="width:{max(2, 70 * v / top):.0f}%"></i><span>{v}</span></div>'

    last_run = meta.get("last_run") or now
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fomo-Bot Dashboard</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>Fomo-Bot · Papier-Test</h1>
<span class="small muted" id="upd" data-ts="{int(last_run)}">Letzte Runde: {berlin(last_run):%d.%m.%Y %H:%M} Uhr</span>
<span><a class="small" href="radar.html"><b>Einstiegs-Radar →</b></a> · <a class="small" href="wallets.html">Smart-Wallets →</a></span></header>
{run_box()}
<div class="grid">{''.join(cards)}</div>
<h2>Kontoverlauf</h2><div class="card">{equity_chart(meta)}</div>
<h2>Offene Positionen ({len(open_rows)})</h2>
<div class="card scroll">{'<table><tr><th>Bot</th><th>Coin</th><th>Kauf</th><th>MC beim Kauf</th><th>Einsatz</th><th>Jetzt</th></tr>' + ''.join(open_rows) + '</table>' if open_rows else '<p class="muted">Keine offenen Positionen.</p>'}</div>
<h2>Letzte Trades</h2>
<div class="card scroll">{'<table><tr><th>Bot</th><th>Coin</th><th>Verkauf</th><th>Ergebnis</th><th>Grund</th></tr>' + ''.join(closed_rows) + '</table>' if closed_rows else '<p class="muted">Noch keine abgeschlossenen Trades.</p>'}</div>
<h2>Beobachtungsliste</h2><p class="small muted">Diese Coins haben alle Regeln bestanden. Gekauft wird erst beim stabilisierten Rücksetzer.</p>
<div class="grid">{''.join(watch_html)}</div>
<h2>Smart-Money-Radar</h2><p class="small muted">Käufe und Verkäufe unserer Pool-Wallets in kleinen Coins (letzte 72 h).</p>
<div class="card scroll">{radar_html}</div>
<h2>Letzter Trichter</h2><div class="card funnel">{funnel_html or '<p class="muted">Noch kein Durchlauf.</p>'}
<p class="small muted">Stand {berlin(meta.get("last_scan") or now):%H:%M} Uhr</p></div>
<footer>Nur Papier-Handel mit Live-Kursen, kein echtes Geld. {POSITION_PCT * 100:.0f} % pro Trade ·
{FEE_SLIPPAGE * 100:.0f} % Gebühren je Seite. Regeln je Bot stehen in der jeweiligen Karte. Die Seite lädt sich jede Minute neu.
Kein Kaufsignal.</footer></div>
<script>(function(){{var e=document.getElementById('upd');var age=(Date.now()/1000-Number(e.dataset.ts))/60;
if(age>40){{e.className='small stale';e.textContent+=' – seit '+Math.round(age)+' Min. keine neue Runde';}}}})();</script>
<script>{RUN_JS}</script>
</body></html>"""
    os.makedirs(os.path.dirname(DASHBOARD), exist_ok=True)
    with open(DASHBOARD, "w", encoding="utf-8") as f:
        f.write(doc)


# ---------------------------------------------------------------- Start
def run_loop():
    states, meta = load_all()
    log(f"Fomo-Bots gestartet ({', '.join(BOTS)}). Dashboard: {DASHBOARD}. Stoppen mit Strg+C.")
    try:
        while True:
            prices = cycle(states, meta)
            save_all(states, meta)
            write_dashboard(states, meta, prices)
            log("Konten: " + " | ".join(f"[{n}] {equity(s, prices):.2f} $ ({len(s['open'])} offen)"
                                        for n, s in states.items()))
            time.sleep(CHECK_EVERY_SEC)
    except KeyboardInterrupt:
        save_all(states, meta)
        log("Bots gestoppt. Stand ist gespeichert.")
        report(states)


def run_once():
    states, meta = load_all()
    prices = cycle(states, meta)
    save_all(states, meta)
    write_dashboard(states, meta, prices)
    log("Runde fertig: " + " | ".join(f"[{n}] {equity(s, prices):.2f} $" for n, s in states.items()))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "once":
        run_once()
    elif cmd == "report":
        report(load_all()[0])
    elif cmd == "reset":
        if input("Beide Papier-Konten wirklich zurücksetzen? (ja/nein) ").strip().lower() == "ja":
            save_all({n: new_state() for n in BOTS}, new_meta())
            print("Zurückgesetzt. Die Journale bleiben erhalten.")
    else:
        run_loop()
