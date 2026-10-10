"""
Fomo-Bot – TESTMODUS (Paper Trading), zwei Bots im Vergleich, mit Live-Dashboard
Handelt mit echten Live-Kursen, aber nur auf dem Papier. Kein Geld, keine Wallet, kein Schlüssel.

Zwei Bots mit getrennten Papier-Konten (je 13 % pro Trade):
  "alle"     – jeder Coin, der den Trichter besteht (ab 6 h alt)
  "14tage"   – nur Coins ab 14 Tagen Alter

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

# ======================= EINSTELLUNGEN =======================
BOTS = {
    "alle":   {"label": "Alle Coins",      "min_age_days": 0},
    "14tage": {"label": "Nur ab 14 Tagen", "min_age_days": 14},
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
HISTORY_MAX = 3000          # so viele Konto-Stände fürs Diagramm merken
# =============================================================

HERE = os.path.dirname(os.path.abspath(__file__))
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
                out[a] = {"price": price, "liq": liq, "m5": ch.get("m5") or 0,
                          "h1": ch.get("h1") or 0, "h24": ch.get("h24") or 0,
                          "mc": p.get("marketCap") or p.get("fdv") or 0}
    return out


# ---------------------------------------------------------------- Trichter
def scan(meta):
    log("Trichter läuft ...")
    try:
        src = funnel.collect_candidates()
        coins = funnel.market_data(src)
        counts, final, watch, _ = funnel.run_funnel(coins, funnel.rugcheck_live)
    except Exception as e:
        log(f"Trichter-Fehler: {e}")
        return None
    meta["counts"] = counts
    log(f"Trichter: {counts['Eingang']} rein, {len(final)} durch, {len(watch)} Watchlist")
    return [{"addr": c["addr"], "sym": c["sym"], "age_d": c["age_h"] / 24, "mc": c["mc"]}
            for c in final + watch]


def update_watch(name, state, candidates):
    now = time.time()
    state["watch"] = {}
    for c in candidates:
        a = c["addr"]
        if c["age_d"] < BOTS[name]["min_age_days"]:
            continue
        if a in state["open"] or state["cooldown"].get(a, 0) > now:
            continue
        state["watch"][a] = {"sym": c["sym"], "age_d": round(c["age_d"], 1), "mc": c["mc"]}


# ---------------------------------------------------------------- Handel (Papier)
def equity(state, prices):
    value = sum(p["qty"] * prices.get(a, {}).get("price", p.get("last_price", p["entry_price"]))
                for a, p in state["open"].items())
    return state["cash"] + value


def try_entries(name, state, prices):
    for a, w in list(state["watch"].items()):
        if len(state["open"]) >= MAX_OPEN:
            return
        q = prices.get(a)
        if not q:
            continue
        if not (ENTRY_H1_MIN <= q["h1"] <= ENTRY_H1_MAX and q["m5"] >= ENTRY_M5_MIN
                and q["h24"] < ENTRY_MAX_24H):
            continue
        stake = min(state["cash"], equity(state, prices) * POSITION_PCT)
        if stake < 1:
            return
        fill = q["price"] * (1 + FEE_SLIPPAGE)
        state["cash"] -= stake
        state["open"][a] = {"sym": w["sym"], "entry_price": q["price"], "qty": stake / fill,
                            "stake": stake, "entry_time": time.time(), "entry_liq": q["liq"],
                            "entry_mc": q["mc"], "age_d": w.get("age_d"), "last_seen": time.time(),
                            "last_price": q["price"]}
        del state["watch"][a]
        log(f"[{name}] KAUF (Papier) ${w['sym']} für {stake:.2f} $ bei MC {funnel.fmt(q['mc'])}  CA: {a}")


def close(name, state, a, price, reason):
    p = state["open"].pop(a)
    proceeds = p["qty"] * price * (1 - FEE_SLIPPAGE)
    state["cash"] += proceeds
    pnl = proceeds - p["stake"]
    trade = {"sym": p["sym"], "addr": a, "entry_time": p["entry_time"], "exit_time": time.time(),
             "entry_price": p["entry_price"], "exit_price": price, "stake": round(p["stake"], 4),
             "proceeds": round(proceeds, 4), "pnl": round(pnl, 4),
             "pnl_pct": round(pnl / p["stake"] * 100, 2), "reason": reason,
             "hold_h": round((time.time() - p["entry_time"]) / 3600, 2), "age_d": p.get("age_d"),
             "entry_mc": p.get("entry_mc")}
    state["closed"].append(trade)
    state["cooldown"][a] = time.time() + COOLDOWN_H * 3600
    write_journal(name, trade)
    log(f"[{name}] VERKAUF (Papier) ${p['sym']}: {reason} -> {pnl:+.2f} $ ({trade['pnl_pct']:+.1f} %)  CA: {a}")


def manage_exits(name, state, prices):
    now = time.time()
    for a, p in list(state["open"].items()):
        q = prices.get(a)
        if not q:
            if now - p["last_seen"] > DEAD_AFTER_MIN * 60:
                close(name, state, a, 0.0, "keine Kursdaten mehr (tot/gerugged)")
            continue
        p["last_seen"], p["last_price"] = now, q["price"]
        change = q["price"] / p["entry_price"] - 1
        if q["liq"] < p["entry_liq"] * (1 - LIQ_DROP_EXIT):
            close(name, state, a, q["price"], "Liquidität abgezogen (Rug-Verdacht)")
        elif change >= TAKE_PROFIT:
            close(name, state, a, q["price"], f"Take-Profit (+{change * 100:.0f} %)")
        elif change <= -STOP_LOSS:
            close(name, state, a, q["price"], f"Stop-Loss ({change * 100:.0f} %)")
        elif now - p["entry_time"] > MAX_HOLD_H * 3600:
            close(name, state, a, q["price"], f"Zeitlimit {MAX_HOLD_H} h ({change * 100:+.0f} %)")


def cycle(states, meta):
    """Eine komplette Runde: ggf. Trichter, Kurse, Ausstiege, Einstiege, Verlauf."""
    if time.time() - meta["last_scan"] >= SCAN_EVERY_MIN * 60:
        candidates = scan(meta)
        if candidates is not None:
            for n, s in states.items():
                update_watch(n, s, candidates)
            meta["last_scan"] = time.time()
    addrs = set()
    for s in states.values():
        addrs |= set(s["open"]) | set(s["watch"])
    prices = get_prices(addrs) if addrs else {}
    for n, s in states.items():
        manage_exits(n, s, prices)
        try_entries(n, s, prices)
    meta["last_run"] = time.time()
    meta["history"].append([int(time.time())] + [round(equity(states[n], prices), 4) for n in BOTS])
    meta["history"] = meta["history"][-HISTORY_MAX:]
    return prices


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
:root{--bg:#f6f7f9;--card:#ffffff;--fg:#14171c;--muted:#5c6470;--line:#e2e5ea;--a:#2f6fd6;--b:#d9822b;
--good:#1f8a5b;--bad:#c4413b;--chip:#eef1f5;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--fg:#e8eaed;--muted:#99a1ad;--line:#2a3039;
--a:#6ea2ff;--b:#f0a35a;--good:#4cc38a;--bad:#ef6b63;--chip:#222831;color-scheme:dark}}
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
a{color:var(--a)}footer{margin-top:32px;color:var(--muted);font-size:13px}
"""


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

    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Kontoverlauf beider Bots">']
    parts.append('<g class="grid">')
    for v in (lo + pad, START_BALANCE, hi - pad):
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                     f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:.2f} $</text>')
    parts.append('</g>')
    parts.append(f'<text x="{L}" y="{H - 6}">{berlin(x0):%d.%m. %H:%M}</text>'
                 f'<text x="{W - R}" y="{H - 6}" text-anchor="end">{berlin(x1):%d.%m. %H:%M}</text>')
    colors = ["var(--a)", "var(--b)"]
    for i, _ in enumerate(BOTS):
        pts = " ".join(f"{X(h[0]):.1f},{Y(h[i + 1]):.1f}" for h in hist if len(h) > i + 1)
        parts.append(f'<polyline fill="none" stroke="{colors[i % 2]}" stroke-width="2" points="{pts}"/>')
        last = hist[-1]
        parts.append(f'<circle cx="{X(last[0]):.1f}" cy="{Y(last[i + 1]):.1f}" r="3.5" fill="{colors[i % 2]}"/>')
    parts.append('</svg>')
    return "".join(parts)


def coin_links(a):
    return (f'<a href="https://dexscreener.com/solana/{a}">Chart</a> · '
            f'<a href="https://rugcheck.xyz/tokens/{a}">RugCheck</a> · '
            f'<a href="https://trench.bot/bundles/{a}">Trench</a>')


def write_dashboard(states, meta, prices):
    now = time.time()
    colors = ["var(--a)", "var(--b)"]
    cards = []
    for i, (n, s) in enumerate(states.items()):
        x = summary(s, prices)
        rate = f"{x['rate']:.0f} %" if x["rate"] is not None else "–"
        cards.append(
            f'<div class="card"><h3><span class="dot" style="background:{colors[i % 2]}"></span>{html.escape(BOTS[n]["label"])}</h3>'
            f'<div class="big">{x["equity"]:.2f} $</div><div>{pct(x["ret"])} seit Start ({START_BALANCE:.0f} $)</div>'
            f'<div class="kv"><span>Trades</span><span>{x["n"]}</span>'
            f'<span>Trefferquote</span><span>{rate} <span class="muted small">(Ziel über 56 %)</span></span>'
            f'<span>Ø Gewinner</span><span>{pct(x["avg_win"])}</span>'
            f'<span>Ø Verlierer</span><span>{pct(x["avg_loss"])}</span>'
            f'<span>Offen</span><span>{x["open"]} von {MAX_OPEN}</span></div></div>')

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
                f'<span class="small muted">{t["pnl"]:+.2f} $</span></td><td class="small">{html.escape(t["reason"])}</td></tr>'))
        chips = "".join(f'<div><span class="chip">${html.escape(w["sym"])}</span>'
                        f'<span class="small muted">{w.get("age_d", "?")} Tage · MC {funnel.fmt(w.get("mc") or 0)}</span>'
                        f'<br><code>{a}</code></div>' for a, w in s["watch"].items())
        watch_html.append(f'<div class="card"><h3>{html.escape(BOTS[n]["label"])}</h3>'
                          f'{chips or "<p class=muted>Gerade nichts, was alle Regeln besteht.</p>"}</div>')
    closed_rows = [r for _, r in sorted(closed_rows, reverse=True)[:20]]

    counts = meta.get("counts") or {}
    funnel_html = ""
    if counts:
        top = max(counts.get("Eingang", 1), 1)
        for k in funnel.STAGES:
            v = counts.get(k, 0)
            funnel_html += f'<div><b>{k}</b><i style="width:{max(2, 70 * v / top):.0f}%"></i><span>{v}</span></div>'

    last_run = meta.get("last_run") or now
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<title>Fomo-Bot Dashboard</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>Fomo-Bot · Papier-Test</h1>
<span class="small muted" id="upd" data-ts="{int(last_run)}">Letzte Runde: {berlin(last_run):%d.%m.%Y %H:%M} Uhr</span></header>
<div class="grid">{''.join(cards)}</div>
<h2>Kontoverlauf</h2><div class="card">{equity_chart(meta)}</div>
<h2>Offene Positionen ({len(open_rows)})</h2>
<div class="card scroll">{'<table><tr><th>Bot</th><th>Coin</th><th>Kauf</th><th>MC beim Kauf</th><th>Einsatz</th><th>Jetzt</th></tr>' + ''.join(open_rows) + '</table>' if open_rows else '<p class="muted">Keine offenen Positionen.</p>'}</div>
<h2>Letzte Trades</h2>
<div class="card scroll">{'<table><tr><th>Bot</th><th>Coin</th><th>Verkauf</th><th>Ergebnis</th><th>Grund</th></tr>' + ''.join(closed_rows) + '</table>' if closed_rows else '<p class="muted">Noch keine abgeschlossenen Trades.</p>'}</div>
<h2>Beobachtungsliste</h2><p class="small muted">Diese Coins haben alle Regeln bestanden. Gekauft wird erst beim stabilisierten Rücksetzer.</p>
<div class="grid">{''.join(watch_html)}</div>
<h2>Letzter Trichter</h2><div class="card funnel">{funnel_html or '<p class="muted">Noch kein Durchlauf.</p>'}
<p class="small muted">Stand {berlin(meta.get("last_scan") or now):%H:%M} Uhr</p></div>
<footer>Nur Papier-Handel mit Live-Kursen, kein echtes Geld. {POSITION_PCT * 100:.0f} % pro Trade · Take-Profit +{TAKE_PROFIT * 100:.0f} % ·
Stop -{STOP_LOSS * 100:.0f} % · max. {MAX_HOLD_H} h · {FEE_SLIPPAGE * 100:.0f} % Gebühren je Seite. Die Seite lädt sich jede Minute neu.
Kein Kaufsignal.</footer></div>
<script>(function(){{var e=document.getElementById('upd');var age=(Date.now()/1000-Number(e.dataset.ts))/60;
if(age>40){{e.className='small stale';e.textContent+=' – seit '+Math.round(age)+' Min. keine neue Runde';}}}})();</script>
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
