"""
Muster-Scan: Welche Coins passen JETZT zum Muster der bisherigen Gewinner?
Muster (aus Coin-Check vom 11.10., 35 Coins):
  - Market Cap unter 1 Mio. (7 von 16 Coins machten 1,5x+, über 3 Mio.: 0 von 8)
  - Alter 1,5 bis 14 Tage (ganz neue Pumper sind meist gestorben, alte Coins bewegen sich kaum)
  - alle Trichter-Regeln bestanden, inkl. Cluster-Check
  - nicht mitten im Pump (24 h und 1 h ruhig), Liquidität gesund
Ergebnis: pattern_scan.json + docs/muster.html. Kein Kaufsignal.
"""

import html
import json
import os
import time

import memecoin_funnel as funnel

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pattern_scan.json")
PAGE = os.path.join(HERE, "docs", "muster.html")

MC_MIN, MC_MAX = 150_000, 1_000_000
AGE_MIN_D, AGE_MAX_D = 1.5, 14
LIQ_MIN = 0.10           # Liquidität mindestens 10 % der MC
H24_MIN, H24_MAX = -35, 120
H1_MIN, H1_MAX = -12, 10
VOL_MC_MIN = 0.3         # Tagesvolumen mindestens 30 % der MC (es muss sich was bewegen)


def pattern(c):
    age = c["age_h"] / 24
    liq = c["liq"] / c["mc"] if c["mc"] else 0
    volmc = c["vol24"] / c["mc"] if c["mc"] else 0
    checks = {
        f"MC {funnel.fmt(c['mc'])} (150k–1 Mio.)": MC_MIN <= c["mc"] <= MC_MAX,
        f"Alter {age:.1f} Tage (1,5–14)": AGE_MIN_D <= age <= AGE_MAX_D,
        f"Liquidität {liq * 100:.0f} % der MC (≥10 %)": liq >= LIQ_MIN,
        f"24 h {c['ch24']:+.0f} % (kein Pump)": H24_MIN <= c["ch24"] <= H24_MAX,
        f"1 h {c['ch1']:+.0f} % (ruhig)": H1_MIN <= c["ch1"] <= H1_MAX,
        f"Volumen {volmc * 100:.0f} % der MC (≥30 %)": volmc >= VOL_MC_MIN,
    }
    return checks, age, liq, volmc


def main():
    src = funnel.collect_candidates()
    coins = [c for c in funnel.market_data(src) if c["mc"] <= MC_MAX]
    print(f"{len(coins)} Coins unter 1 Mio. MC im Eingang")
    counts, final, watch, allc = funnel.run_funnel(coins, funnel.rugcheck_live)
    rows = []
    for c in allc:
        checks, age, liq, volmc = pattern(c)
        rows.append({
            "addr": c["addr"], "sym": c["sym"], "name": c["name"], "mc": c["mc"], "liq": c["liq"],
            "vol24": c["vol24"], "ch1": c["ch1"], "ch24": c["ch24"], "age_d": round(age, 1),
            "buys": c["buys"], "sells": c["sells"], "cluster": c.get("cluster"),
            "funnel": "bestanden" if c in final else ("Timing" if c in watch else f"raus: {c['out_at']}"),
            "funnel_why": c["reasons"], "warns": c["warns"],
            "checks": checks, "score": sum(checks.values()), "volmc": round(volmc, 2), "liqmc": round(liq, 3),
        })
    passed = [r for r in rows if r["funnel"] == "bestanden"]
    passed.sort(key=lambda r: (r["score"], r["volmc"]), reverse=True)
    others = sorted([r for r in rows if r["funnel"] != "bestanden"], key=lambda r: -r["score"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"t": time.time(), "counts": counts, "passed": passed, "others": others}, f, indent=1)
    write_page(passed, counts)
    for r in passed[:10]:
        print(f"${r['sym']} {r['score']}/6  MC {funnel.fmt(r['mc'])}  {r['addr']}")


def write_page(passed, counts):
    from fomo_bot import CSS, berlin
    cards = []
    for r in passed:
        a = r["addr"]
        li = "".join(f'<li>{"✅" if ok else "❌"} {html.escape(k)}</li>' for k, ok in r["checks"].items())
        warns = "".join(f"<li>⚠️ {html.escape(w)}</li>" for w in r["warns"])
        cards.append(
            f'<div class="card"><h3>${html.escape(r["sym"])} · {r["score"]}/6</h3><code>{a}</code>'
            f'<ul class="small">{li}{warns}</ul><span class="small">'
            f'<a href="https://dexscreener.com/solana/{a}">Chart</a> · <a href="https://rugcheck.xyz/tokens/{a}">RugCheck</a> · '
            f'<a href="https://trench.bot/bundles/{a}">Trench</a> · <a href="https://app.bubblemaps.io/sol/token/{a}">Bubblemaps</a></span></div>')
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Muster-Scan</title><style>{CSS}</style></head>
<body><div class="wrap"><header><h1>Muster-Scan</h1><a class="small" href="index.html">← zum Bot-Dashboard</a></header>
<p class="small muted">Stand {berlin(time.time()):%d.%m.%Y %H:%M} Uhr · Coins unter 1 Mio. MC, die alle Trichter-Regeln bestehen,
sortiert danach, wie gut sie zum Muster der bisherigen Gewinner passen. Kein Kaufsignal.</p>
<div class="grid">{''.join(cards) or '<p class="muted">Gerade nichts.</p>'}</div></div></body></html>"""
    os.makedirs(os.path.dirname(PAGE), exist_ok=True)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(doc)


if __name__ == "__main__":
    main()
