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

# CLAUDIA-Profil: Start-Pump, dann großer Absturz ÜBERLEBT, Boden gebildet, Volumen kommt zurück.
# CLAUDIA: Start 7.10. bis 1,28 Mio., dann -84 % auf 209k, 1,5 Tage Boden bei 250-550k,
# am 9.10. Ausbruch mit Volumen -> 11 Mio.
CL_AGE_MIN_D, CL_AGE_MAX_D = 1.0, 6.0
CL_MC_MAX = 1_000_000
CL_DRAWDOWN = 0.5        # nach dem ersten Hoch mindestens -50 % gefallen ...
CL_BASE_H = 12           # ... und in den letzten 12 h kein neues Tief (Boden hält)
CL_VOL_UP = 1.5          # Volumen der letzten 6 h mind. 1,5x so hoch wie im Schnitt der 18 h davor


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


def extra_sources(src):
    """Zusätzlich zu Trending/Boosts: die volumenstärksten Solana-Pools (da tauchen Boden-Coins auf)."""
    for page in (1, 2, 3):
        try:
            data = funnel.get("https://api.geckoterminal.com/api/v2/networks/solana/pools"
                              f"?page={page}&sort=h24_volume_usd_desc")
            for p in data.get("data", []):
                a = p["relationships"]["base_token"]["data"]["id"].replace("solana_", "")
                if a not in funnel.SKIP:
                    src.setdefault(a, set()).add("volumen")
        except Exception as e:
            print(f"  GeckoTerminal Volumen-Seite {page}: {e}")
        time.sleep(2.5)


def claudia_profile(c):
    """Prüft anhand der Stundenkerzen, ob der Coin dem CLAUDIA-Verlauf ähnelt."""
    from coin_check import candles
    try:
        rows = candles(c["main_pair"], c["addr"])
    except Exception as e:
        return {"ok": False, "why": f"keine Kerzen ({e})"}
    time.sleep(3)
    if len(rows) < 24:
        return {"ok": False, "why": "zu wenig Kursgeschichte"}
    sup = c["mc"] / rows[-1][4] if rows[-1][4] else 0
    hi_i = max(range(len(rows)), key=lambda i: rows[i][2])
    ath = rows[hi_i][2]
    after = rows[hi_i:]
    low = min(r[3] for r in after)
    low_i = hi_i + min(range(len(after)), key=lambda i: after[i][3])
    dd = 1 - low / ath if ath else 0
    hours_since_low = (rows[-1][0] - rows[low_i][0]) / 3600
    now = rows[-1][4]
    vol6 = sum(r[5] for r in rows[-6:]) / 6
    vol18 = sum(r[5] for r in rows[-24:-6]) / 18 or 1e-9
    res = {
        "ath_mc": round(ath * sup), "low_mc": round(low * sup), "drawdown": round(dd * 100),
        "h_since_low": round(hours_since_low), "now_vs_ath": round(now / ath * 100),
        "now_vs_low": round((now / low - 1) * 100) if low else None, "vol_up": round(vol6 / vol18, 2),
    }
    res["checks"] = {
        f"Erstes Hoch {funnel.fmt(res['ath_mc'])}, danach -{res['drawdown']} % (≥50 %)": dd >= CL_DRAWDOWN,
        f"Tief vor {res['h_since_low']} h (Boden hält ≥{CL_BASE_H} h)": hours_since_low >= CL_BASE_H,
        f"Jetzt {res['now_vs_ath']} % vom Hoch (noch Luft nach oben)": now < ath * 0.95,
        f"Volumen letzte 6 h {res['vol_up']}x (≥{CL_VOL_UP}x)": res["vol_up"] >= CL_VOL_UP,
    }
    res["score"] = sum(res["checks"].values())
    res["ok"] = res["score"] == 4
    return res


def main():
    src = funnel.collect_candidates()
    extra_sources(src)
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
    # CLAUDIA-Profil für alle sicheren Coins (Timing/Handel dürfen hier durchfallen: Ausbrüche sehen wie Pumps aus)
    safe = [c for c in allc if (c["out_at"] in (None, "Timing", "Handel"))
            and c["mc"] <= CL_MC_MAX and CL_AGE_MIN_D <= c["age_h"] / 24 <= CL_AGE_MAX_D]
    print(f"CLAUDIA-Profil für {len(safe)} sichere Coins ...")
    by = {r["addr"]: r for r in rows}
    for c in safe:
        by[c["addr"]]["claudia"] = claudia_profile(c)
    passed = [r for r in rows if r["funnel"] == "bestanden"]
    passed.sort(key=lambda r: (r["score"], r["volmc"]), reverse=True)
    others = sorted([r for r in rows if r["funnel"] != "bestanden"], key=lambda r: -r["score"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"t": time.time(), "counts": counts, "passed": passed, "others": others,
                   "claudia": sorted([r for r in rows if r.get("claudia")],
                                     key=lambda r: -(r["claudia"].get("score") or 0))}, f, indent=1)
    write_page(passed, counts, sorted([r for r in rows if r.get("claudia")],
                                      key=lambda r: -(r["claudia"].get("score") or 0)))
    for r in passed[:10]:
        print(f"${r['sym']} {r['score']}/6  MC {funnel.fmt(r['mc'])}  {r['addr']}")


def write_page(passed, counts, claudia=()):
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
    cl = []
    for r in claudia:
        p = r["claudia"]
        li = "".join(f'<li>{"✅" if ok else "❌"} {html.escape(k)}</li>' for k, ok in (p.get("checks") or {}).items())
        a = r["addr"]
        cl.append(f'<div class="card"><h3>${html.escape(r["sym"])} · {p.get("score", 0)}/4</h3><code>{a}</code>'
                  f'<p class="small">MC {funnel.fmt(r["mc"])} · {r["age_d"]} Tage · Trichter: {html.escape(r["funnel"])}</p>'
                  f'<ul class="small">{li or "<li>" + html.escape(p.get("why", "")) + "</li>"}</ul>'
                  f'<span class="small"><a href="https://dexscreener.com/solana/{a}">Chart</a> · '
                  f'<a href="https://app.bubblemaps.io/sol/token/{a}">Bubblemaps</a></span></div>')
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Muster-Scan</title><style>{CSS}</style></head>
<body><div class="wrap"><header><h1>Muster-Scan</h1><a class="small" href="index.html">← zum Bot-Dashboard</a></header>
<p class="small muted">Stand {berlin(time.time()):%d.%m.%Y %H:%M} Uhr · Coins unter 1 Mio. MC, die alle Trichter-Regeln bestehen,
sortiert danach, wie gut sie zum Muster der bisherigen Gewinner passen. Kein Kaufsignal.</p>
<div class="grid">{''.join(cards) or '<p class="muted">Gerade nichts.</p>'}</div>
<h2>CLAUDIA-Profil</h2><p class="small muted">Sichere Coins (1–6 Tage, unter 1 Mio.), deren Verlauf CLAUDIA vor dem Ausbruch ähnelt:
erster Pump, dann mindestens -50 %, Boden hält, Volumen kommt zurück. Kein Kaufsignal.</p>
<div class="grid">{''.join(cl) or '<p class="muted">Gerade keiner.</p>'}</div></div></body></html>"""
    os.makedirs(os.path.dirname(PAGE), exist_ok=True)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(doc)


if __name__ == "__main__":
    main()
