"""
Coin-Check: Wie haben sich Coins seit dem ersten Auftauchen entwickelt?
Für jeden Coin aus coin_check_input.json:
  - Kurs beim ersten Auftauchen (Stundenkerze von GeckoTerminal)
  - Höchster Kurs seitdem  -> "Peak x"  (was maximal drin gewesen wäre)
  - Kurs jetzt             -> "Jetzt x"
Ergebnis: coin_check.json und docs/coin_check.html

Start: python coin_check.py            (Eingabe: coin_check_input.json)
       python coin_check.py bots       (alle Coins aus den Bot-Daten + Eingabe-Datei)
"""

import glob
import html
import json
import os
import sys
import time

import memecoin_funnel as funnel

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_LIQ = 1000      # unter 1.000 $ Liquidität ist ein Coin praktisch tot
IN_FILE = os.path.join(HERE, "coin_check_input.json")
OUT_FILE = os.path.join(HERE, "coin_check.json")
PAGE = os.path.join(HERE, "docs", "coin_check.html")


def from_bots():
    """Erstes Auftauchen jedes Coins in Bot-Käufen, Watchlists und Gewinner-Listen."""
    seen = {}

    def add(a, sym, mc, t, src):
        if a and t and (a not in seen or t < seen[a]["t"]):
            seen[a] = {"addr": a, "sym": sym, "t": t, "mc_first": mc, "src": src}

    for path in glob.glob(os.path.join(HERE, "bot_state_*.json")):
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        name = os.path.basename(path)[10:-5]
        for t in s.get("closed") or []:
            add(t["addr"], t["sym"], t.get("entry_mc"), t["entry_time"], f"Bot {name}")
        for a, p in (s.get("open") or {}).items():
            add(a, p["sym"], p.get("entry_mc"), p["entry_time"], f"Bot {name}")
    try:
        with open(os.path.join(HERE, "wallets.json"), encoding="utf-8") as f:
            for a, v in json.load(f).get("winners", {}).items():
                add(a, v["sym"], v.get("mc"), v.get("found"), "Gewinner-Coin")
    except FileNotFoundError:
        pass
    return seen


def main_pair(addr):
    pairs = funnel.get("https://api.dexscreener.com/tokens/v1/solana/" + addr) or []
    pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == addr]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def candles(pool, token):
    url = (f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour"
           f"?aggregate=1&limit=1000&currency=usd&token={token}")
    for wait in (0, 15, 30, 60):      # GeckoTerminal drosselt geteilte IPs (GitHub) schnell
        time.sleep(wait)
        try:
            data = funnel.get(url, timeout=30)
            break
        except Exception as e:
            if "429" not in str(e) or wait == 60:
                raise
    rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted(rows)   # [ts, open, high, low, close, volume], alt -> neu


def check(c):
    out = dict(c)
    try:
        p = main_pair(c["addr"])
    except Exception as e:
        out["error"] = f"DexScreener: {e}"
        return out
    time.sleep(0.3)
    if not p:
        out["error"] = "keine Handelspaare mehr (tot)"
        out["x_now"] = 0.0
        return out
    out["sym"] = out.get("sym") or p["baseToken"].get("symbol")
    out["mc_now"] = p.get("marketCap") or p.get("fdv") or 0
    out["liq_now"] = (p.get("liquidity") or {}).get("usd") or 0
    price_now = float(p.get("priceUsd") or 0)
    if out["liq_now"] < MIN_LIQ:
        out["error"] = f"keine Liquidität mehr ({out['liq_now']:.0f} $) – Kurs nur rechnerisch, nicht verkaufbar"
        out["x_now"] = 0.0
        return out
    try:
        rows = candles(p["pairAddress"], c["addr"])
    except Exception as e:
        rows = []
        out["note"] = f"keine Kursgeschichte ({e})"
    time.sleep(4)      # GeckoTerminal: max. 30 Abfragen pro Minute, auf GitHub lieber deutlich weniger
    t0 = c["t"] - c["t"] % 3600
    after = [r for r in rows if r[0] >= t0]
    if after:
        first = after[0][1] if after[0][0] > t0 else after[0][4]   # Kerze des ersten Auftauchens: Schluss
        peak_row = max(after, key=lambda r: r[2])
        out["price_first"] = first
        out["x_peak"] = round(peak_row[2] / first, 2) if first else None
        out["peak_time"] = peak_row[0]
        out["x_now"] = round(price_now / first, 2) if first and price_now else None
        if not out.get("mc_first") and out["x_now"]:
            out["mc_first"] = out["mc_now"] / out["x_now"]
    elif out.get("mc_first"):
        out["x_now"] = round(out["mc_now"] / out["mc_first"], 2)
    # Plausibilität: Kursverlauf muss zur Market-Cap-Entwicklung passen, sonst passt die Kerze nicht zum Coin
    if c.get("mc_first") and out.get("mc_now") and out.get("x_peak"):
        x_mc = out["mc_now"] / c["mc_first"]
        if out.get("x_now") and not (0.6 < out["x_now"] / x_mc < 1.6):
            out["note"] = f"Kursverlauf unplausibel (Kurs {out['x_now']}x vs. MC {x_mc:.2f}x) – nur MC-Wert genommen"
            out["x_now"], out["x_peak"] = round(x_mc, 2), None
    return out


def write_page(res):
    from fomo_bot import CSS, berlin
    rows = []
    for r in res:
        a = r["addr"]
        peak = f'{r["x_peak"]:.2f}x' if r.get("x_peak") else "–"
        now = f'{r["x_now"]:.2f}x' if r.get("x_now") is not None else "–"
        rows.append(
            f'<tr><td><b>${html.escape(r.get("sym") or "?")}</b><br><code>{a}</code><br>'
            f'<span class="small"><a href="https://dexscreener.com/solana/{a}">Chart</a> · '
            f'<a href="https://app.bubblemaps.io/sol/token/{a}">Bubblemaps</a></span></td>'
            f'<td>{berlin(r["t"]):%d.%m. %H:%M}<br><span class="small muted">{html.escape(r.get("src", ""))}</span></td>'
            f'<td>{funnel.fmt(r.get("mc_first") or 0)}</td><td><b>{peak}</b></td>'
            f'<td>{now}<br><span class="small muted">MC {funnel.fmt(r.get("mc_now") or 0)}</span></td>'
            f'<td class="small">{html.escape(r.get("error") or r.get("note") or "")}</td></tr>')
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Coin-Check</title><style>{CSS}</style></head>
<body><div class="wrap"><header><h1>Coin-Check</h1><a class="small" href="index.html">← zum Bot-Dashboard</a></header>
<p class="small muted">Stand {berlin(time.time()):%d.%m.%Y %H:%M} Uhr. Peak = höchster Stundenkurs seit dem ersten Auftauchen,
geteilt durch den Kurs beim ersten Auftauchen. Jetzt = aktueller Kurs geteilt durch denselben Startkurs.</p>
<div class="card scroll"><table><tr><th>Coin</th><th>Erstes Auftauchen</th><th>MC damals</th><th>Peak</th><th>Jetzt</th><th></th></tr>
{''.join(rows)}</table></div></div></body></html>"""
    os.makedirs(os.path.dirname(PAGE), exist_ok=True)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    coins = {}
    if len(sys.argv) > 1 and sys.argv[1] == "bots":
        coins.update(from_bots())
    if os.path.exists(IN_FILE):
        with open(IN_FILE, encoding="utf-8") as f:
            for c in json.load(f):
                old = coins.get(c["addr"])
                if not old or c["t"] < old["t"]:
                    coins[c["addr"]] = c
    res = []
    for c in sorted(coins.values(), key=lambda c: c["t"]):
        r = check(c)
        res.append(r)
        print(f"${r.get('sym')}: Peak {r.get('x_peak')}x, jetzt {r.get('x_now')}x {r.get('error') or ''}", flush=True)
    res.sort(key=lambda r: -(r.get("x_peak") or r.get("x_now") or 0))
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"t": time.time(), "coins": res}, f, indent=1)
    write_page(res)


if __name__ == "__main__":
    main()
