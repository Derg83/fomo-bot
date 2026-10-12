"""
Einstiegs-Radar: alles, was wir haben, zu einer Liste "wo nach unseren Regeln ein Einstieg passt".

Quellen
  Live        Trichter (Trending, Boosts, Volumen-Pools) inkl. RugCheck + Cluster-Check
  Smart-Money aktive Kauf-Signale unserer Pool-Wallets (smart_radar) + aktuelle Bestände (wallet_holdings)
  Muster      Rückblick-Muster (60-92 % unter dem ersten Hoch, 1-14 Tage) + CLAUDIA-Profil + Basis-Muster (6 Checks)
  Bots        Historie je Coin (Gewinne/Verluste, Verlust-Sperre), Hoch/Tief aller Trades -> Stop/Ziel aus Daten
  Vergangenes Coin-Check (wie liefen frühere Coins seit dem ersten Auftauchen)

Punkte (max. 100, nur für sichere Coins = Contract/Holder/Cluster bestanden)
  Smart-Money      bis 30   gute Pool-Wallet kauft gerade +15, weitere Wallet +8, Wallet hält den Coin +5
  Rückblick/CLAUDIA bis 25  Rückblick-Muster erfüllt 25, sonst CLAUDIA-Profil 5 je Check
  Basis-Muster     bis 20   die 6 Checks aus dem Muster-Scan
  Markt-Qualität   bis 15   Liquidität ≥10 % MC, Käufe/Verkäufe ausgewogen, Volumen ≥50 % MC
  Bot-Historie     +5 / -20 Bot hat damit gewonnen / Coin steht auf der Verlust-Sperre
Ampel
  GRÜN  "Einstieg nach Regeln"   ≥55 Punkte, nicht mitten im Pump, nicht gesperrt
  GELB  "Rücksetzer abwarten"    ≥40 Punkte, aber pumpt gerade
  GRAU  "Beobachten"             25-54 Punkte

Ergebnis: radar.json + docs/radar.html. Kein Handel, keine Anlageberatung – nur unsere eigenen Regeln.
"""

import glob
import html
import json
import os
import statistics
import time

import memecoin_funnel as funnel

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "radar.json")
PAGE = os.path.join(HERE, "docs", "radar.html")
GT = "https://api.geckoterminal.com/api/v2/networks/solana"

MC_MAX = 3_000_000          # darüber kaum Luft (Coin-Check: über 3 Mio. 0 von 8 Coins mit 1,5x)
MAX_CANDLES = 22            # so viele sichere Coins pro Lauf mit Kursverlauf prüfen (GeckoTerminal-Limit)
OK_STAGES = (None, "Handel", "Timing")
GREEN, YELLOW, GREY = 55, 40, 25
PUMP_H1 = 10                # 1 h über +10 % = gerade im Pump
STOP_MIN, STOP_MAX = 0.15, 0.30
HOLDINGS_MAX_AGE_H = 6      # ältere Bestandsdaten zählen nicht
TP1 = 0.20                  # erste Hälfte raus: +20 % (erreichen ca. 15 % der Bot-Trades innerhalb ihrer Haltedauer)
PUSH_AGAIN_H = 12           # denselben Coin frühestens nach 12 h wieder melden


def now():
    return time.time()


def load(path, default):
    try:
        with open(os.path.join(HERE, path), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def fmt(x):
    return funnel.fmt(x or 0)


# ---------------------------------------------------------------- Lernen aus der Vergangenheit
def learn():
    """Was sagen unsere bisherigen Daten über Ziele und Stops?"""
    out = {}
    trades = []
    bots = {}
    for path in glob.glob(os.path.join(HERE, "bot_state_*.json")):
        name = os.path.basename(path)[10:-5]
        st = load(os.path.basename(path), {})
        cl = st.get("closed") or []
        trades += [t for t in cl if "max_pct" in t]
        wins = [t for t in cl if t.get("pnl", 0) > 0]
        bots[name] = {"n": len(cl), "win": round(len(wins) / len(cl) * 100) if cl else None,
                      "pnl": round(sum(t.get("pnl", 0) for t in cl), 2), "open": len(st.get("open") or {}),
                      "cash": round(st.get("cash", 0), 2)}
    out["bots"] = bots
    if trades:
        mx = [t["max_pct"] for t in trades]
        mn = [t["min_pct"] for t in trades]
        out["trades"] = len(trades)
        out["max_median"] = round(statistics.median(mx), 1)
        out["reach"] = {k: round(sum(m >= k for m in mx) / len(mx) * 100) for k in (10, 20, 50, 100)}
        # Wie tief fielen Trades, die irgendwann +20 % erreicht haben, vorher? -> Stop darf nicht enger sein
        good = [t["min_pct"] for t in trades if t["max_pct"] >= 20]
        out["winner_dip_median"] = round(statistics.median(good), 1) if good else None
        out["winner_dip_p75"] = round(sorted(good)[int(len(good) * 0.25)], 1) if len(good) >= 4 else None
        out["min_median"] = round(statistics.median(mn), 1)
        aft = [t["after"].get("24") for t in trades if (t.get("after") or {}).get("24") is not None]
        out["after24_median"] = round(statistics.median(aft), 1) if aft else None
    cc = (load("coin_check.json", {}) or {}).get("coins") or []
    small = [c for c in cc if (c.get("mc_first") or 0) < 1e6 and c.get("x_peak")]
    big = [c for c in cc if (c.get("mc_first") or 0) >= 3e6 and c.get("x_peak")]
    if small:
        out["small_peak_median"] = round(statistics.median(c["x_peak"] for c in small), 2)
        out["small_hit15"] = f"{sum(c['x_peak'] >= 1.5 for c in small)} von {len(small)}"
    if big:
        out["big_hit15"] = f"{sum(c['x_peak'] >= 1.5 for c in big)} von {len(big)}"
    out["top_past"] = sorted([c for c in cc if c.get("x_peak")], key=lambda c: -c["x_peak"])[:6]
    return out


def plan_levels(L):
    """Stop und Ziele aus den Daten, mit vernünftigen Grenzen."""
    dip = abs(L.get("winner_dip_p75") or L.get("winner_dip_median") or -20) / 100
    stop = min(max(dip + 0.03, STOP_MIN), STOP_MAX)
    tp1 = max(0.3, min((L.get("small_peak_median") or 1.47) - 1, 0.8))
    return stop, tp1


# ---------------------------------------------------------------- Bausteine je Coin
def bot_history():
    hist = {}
    for path in glob.glob(os.path.join(HERE, "bot_state_*.json")):
        name = os.path.basename(path)[10:-5]
        st = load(os.path.basename(path), {})
        for t in st.get("closed") or []:
            h = hist.setdefault(t["addr"], {"n": 0, "pnl_pct": [], "bots": set(), "last": 0})
            h["n"] += 1
            h["pnl_pct"].append(t.get("pnl_pct", 0))
            h["bots"].add(name)
            h["last"] = max(h["last"], t.get("exit_time", 0))
        for a in st.get("open") or {}:
            hist.setdefault(a, {"n": 0, "pnl_pct": [], "bots": set(), "last": 0})["open"] = True
    return hist


def smart_sources():
    meta = load("bot_meta.json", {})
    signals = {}
    try:
        import smart_radar
        signals = smart_radar.active_signals(meta)
    except Exception as e:
        print(f"Smart-Signale nicht lesbar: {e}")
    hold = {}
    h = load("wallet_holdings.json", {})
    if h and now() - h.get("t", 0) < HOLDINGS_MAX_AGE_H * 3600:
        for c in h.get("coins") or []:
            hold[c["addr"]] = {"n": c.get("n_wallets", 0), "wallets": c.get("wallets") or []}
    losers = {a: t for a, t in (meta.get("losers") or {}).items() if t > now()}
    return signals, hold, losers, h.get("t")


def candles(c):
    url = f"{GT}/pools/{c['main_pair']}/ohlcv/hour?aggregate=1&limit=400&currency=usd&token={c['addr']}"
    for wait in (0, 15, 30):
        time.sleep(wait)
        try:
            res = funnel.get(url, timeout=30)
            return sorted(((res.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [],
                          key=lambda r: r[0])
        except Exception as e:
            if "429" not in str(e):
                return []
    return []


def shape(c, rows):
    """Rückblick-Muster, CLAUDIA-Profil und Boden aus den Stundenkerzen."""
    if len(rows) < 24 or not rows[-1][4]:
        return {}
    sup = c["mc"] / float(rows[-1][4])
    hi_i = max(range(len(rows)), key=lambda i: rows[i][2])
    ath = rows[hi_i][2]
    after = rows[hi_i:]
    low_i = hi_i + min(range(len(after)), key=lambda i: after[i][3])
    low = rows[low_i][3]
    now_p = rows[-1][4]
    vol6 = sum(r[5] for r in rows[-6:]) / 6
    vol18 = (sum(r[5] for r in rows[-24:-6]) / 18) or 1e-9
    low12 = min(r[3] for r in rows[-12:])
    h_low = (rows[-1][0] - rows[low_i][0]) / 3600
    dd_now = now_p / ath if ath else 1
    age_h = c["age_h"]
    vol_mc = (c.get("vol24") or 0) / c["mc"] if c["mc"] else 0
    s = {
        "ath_mc": round(ath * sup), "low_mc": round(low * sup), "low12_mc": round(low12 * sup),
        "now_vs_ath": round(dd_now * 100), "h_since_low": round(h_low), "vol_up": round(vol6 / vol18, 2),
    }
    # Rückblick-Muster (muster.py): 60-92 % unter dem Hoch, 1-14 Tage, MC 50k-1,5 Mio., Volumen ≥50 % MC
    s["rueckblick"] = (0.08 <= dd_now <= 0.40 and 24 <= age_h <= 336 and 50_000 <= c["mc"] <= 1_500_000
                       and vol_mc >= 0.5)
    s["claudia"] = {
        f"erstes Hoch {fmt(s['ath_mc'])}, danach -{round((1 - low / ath) * 100)} %": (1 - low / ath) >= 0.5 if ath else False,
        f"Boden hält seit {s['h_since_low']} h (≥12 h)": h_low >= 12,
        f"jetzt {s['now_vs_ath']} % vom Hoch (Luft nach oben)": dd_now < 0.95,
        f"Volumen 6 h {s['vol_up']}x (≥1,5x)": s["vol_up"] >= 1.5,
    }
    return s


def base_checks(c):
    age = c["age_h"] / 24
    liq = c["liq"] / c["mc"] if c["mc"] else 0
    volmc = c["vol24"] / c["mc"] if c["mc"] else 0
    return {
        f"MC {fmt(c['mc'])} (150k–1 Mio.)": 150_000 <= c["mc"] <= 1_000_000,
        f"Alter {age:.1f} Tage (1,5–14)": 1.5 <= age <= 14,
        f"Liquidität {liq * 100:.0f} % der MC (≥10 %)": liq >= 0.10,
        f"24 h {c['ch24']:+.0f} % (kein Pump)": -35 <= c["ch24"] <= 120,
        f"1 h {c['ch1']:+.0f} % (ruhig)": -12 <= c["ch1"] <= 10,
        f"Volumen {volmc * 100:.0f} % der MC (≥30 %)": volmc >= 0.3,
    }


def score(c, sig, hold, hist, locked, shp):
    parts, notes = {}, []
    # Smart-Money
    sm = 0
    if sig:
        sm += 15 * sig.get("good", 0) + 8 * max(0, len(sig.get("wallets", [])) - sig.get("good", 0))
        notes.append(f"{len(sig['wallets'])} Pool-Wallet(s) kaufen gerade ({sig['usd']:.0f} $)")
    if hold:
        sm += 5 * hold["n"]
        notes.append(f"{hold['n']} Pool-Wallet(s) halten den Coin")
    parts["Smart-Money"] = min(sm, 30)
    # Rückblick / CLAUDIA
    if shp.get("rueckblick"):
        parts["Rückblick/CLAUDIA"] = 25
        notes.append("Rückblick-Muster erfüllt (60–92 % unter dem Hoch, Boden)")
    elif shp.get("claudia"):
        parts["Rückblick/CLAUDIA"] = 5 * sum(shp["claudia"].values())
    else:
        parts["Rückblick/CLAUDIA"] = 0
    # Basis-Muster
    bc = base_checks(c)
    parts["Basis-Muster"] = round(sum(bc.values()) / len(bc) * 20)
    # Markt-Qualität
    liq = c["liq"] / c["mc"] if c["mc"] else 0
    bs = c["buys"] / c["sells"] if c["sells"] else 2
    volmc = c["vol24"] / c["mc"] if c["mc"] else 0
    parts["Markt-Qualität"] = 5 * (liq >= 0.10) + 5 * (0.8 <= bs <= 2.0) + 5 * (volmc >= 0.5)
    # Bot-Historie
    bh = 0
    if locked:
        bh -= 20
        notes.append("auf der Verlust-Sperre (ein Bot hat damit verloren)")
    if hist and any(p > 0 for p in hist["pnl_pct"]):
        bh += 5
        notes.append(f"Bots: {sum(p > 0 for p in hist['pnl_pct'])} von {len(hist['pnl_pct'])} Trades im Plus")
    parts["Bot-Historie"] = bh
    total = max(0, sum(parts.values()))
    return total, parts, bc, notes


# ---------------------------------------------------------------- Ablauf
RADAR_CLUSTER_CREDITS_DAY = 2000    # eigenes Helius-Tagesbudget für den Cluster-Check des Radars


def use_own_cluster_cache():
    """Eigener Cluster-Cache (der Bot schreibt cluster_cache.json alle 10 Min. – so gibt es keine Konflikte).
    Was der Bot schon nachgeschlagen hat, wird übernommen und kostet nichts."""
    try:
        import cluster_check as cc
    except Exception:
        return
    bot = cc._load()
    cc.CACHE_FILE = os.path.join(HERE, "cluster_cache_radar.json")
    own = cc._load()
    for k in ("funders", "services", "coins"):
        merged = dict(bot.get(k) or {})
        merged.update(own.get(k) or {})
        own[k] = merged
    own.setdefault("credits", {})
    with open(cc.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(own, f)
    cc.CREDITS_PER_DAY = RADAR_CLUSTER_CREDITS_DAY
    cc.NEW_WALLETS_PER_SCAN = 30


def main():
    t0 = now()
    L = learn()
    stop, tp1 = plan_levels(L)
    signals, hold, losers, hold_t = smart_sources()
    hist = bot_history()

    src = funnel.collect_candidates()
    try:
        import pattern_scan
        pattern_scan.extra_sources(src)
    except Exception as e:
        print(f"Volumen-Pools: {e}")
    for a in list(signals) + list(hold):
        src.setdefault(a, set()).add("smart")
    coins = [c for c in funnel.market_data(src) if c["mc"] <= MC_MAX]
    print(f"{len(coins)} Coins bis {fmt(MC_MAX)} MC im Eingang")
    use_own_cluster_cache()
    counts, final, watch, allc = funnel.run_funnel(coins, funnel.rugcheck_live)

    safe = [c for c in allc if c.get("out_at") in OK_STAGES]
    # Smart-Money-Coins, die an echten Risiken scheitern (nicht an "zu klein/zu wenig Volumen" = Staub-Reste)
    unsafe_smart = [c for c in allc if c.get("out_at") not in OK_STAGES
                    and (c["addr"] in signals or (c["addr"] in hold and c.get("out_at") != "Markt"))]
    # Kursverläufe: zuerst Coins mit Smart-Money, dann nach Volumen
    safe.sort(key=lambda c: (c["addr"] not in signals and c["addr"] not in hold, -c.get("vol24", 0)))
    rows = []
    for i, c in enumerate(safe):
        shp = {}
        if i < MAX_CANDLES and c.get("main_pair"):
            shp = shape(c, candles(c))
            time.sleep(2.5)
        a = c["addr"]
        total, parts, bc, notes = score(c, signals.get(a), hold.get(a), hist.get(a), a in losers, shp)
        pumping = c["ch1"] > PUMP_H1 or c.get("out_at") == "Timing"
        if a in losers:
            light = "gesperrt"
        elif total >= GREEN and not pumping:
            light = "grün"
        elif total >= YELLOW and pumping:
            light = "gelb"
        elif total >= GREY:
            light = "grau"
        else:
            light = "kein Signal"
        # Stop: das Tief der letzten 12 h, aber nicht weiter als STOP_MAX und nicht enger als STOP_MIN
        st_mc = c["mc"] * (1 - stop)
        if shp.get("low12_mc"):
            st_mc = min(max(shp["low12_mc"] * 0.97, c["mc"] * (1 - STOP_MAX)), c["mc"] * (1 - STOP_MIN))
        tp1_mc = c["mc"] * (1 + TP1)
        tp_mc = c["mc"] * (1 + (1.0 if shp.get("rueckblick") else tp1))
        rows.append({
            "addr": a, "sym": c["sym"], "name": c["name"], "mc": c["mc"], "liq": c["liq"], "vol24": c["vol24"],
            "ch1": c["ch1"], "ch24": c["ch24"], "age_d": round(c["age_h"] / 24, 1), "buys": c["buys"],
            "sells": c["sells"], "top10": c.get("top10"), "insider": c.get("insider"), "cluster": c.get("cluster"),
            "funnel": c.get("out_at") or "bestanden", "warns": c["warns"], "src": c.get("src"),
            "score": total, "parts": parts, "checks": bc, "shape": shp, "notes": notes, "light": light,
            "smart": signals.get(a), "hold": hold.get(a),
            "plan": {"stop_mc": round(st_mc), "stop_pct": round((st_mc / c["mc"] - 1) * 100),
                     "tp1_mc": round(tp1_mc), "tp1_pct": round(TP1 * 100),
                     "tp_mc": round(tp_mc), "tp_pct": round((tp_mc / c["mc"] - 1) * 100),
                     "tp_note": "Rückblick-Ziel" if shp.get("rueckblick") else "Median-Peak kleiner Coins über 1–3 Tage"},
        })
    rows.sort(key=lambda r: -r["score"])
    running = sorted([c for c in allc if c.get("out_at") in OK_STAGES and (c["ch1"] >= 20 or c["ch24"] >= 100)],
                     key=lambda c: -c["ch24"])
    res = {
        "t": now(), "dauer_s": round(now() - t0), "counts": counts, "learn": L,
        "stop_default": round(stop * 100), "tp_default": round(tp1 * 100), "holdings_t": hold_t,
        "rows": rows,
        "running": [{"addr": c["addr"], "sym": c["sym"], "mc": c["mc"], "ch1": c["ch1"], "ch24": c["ch24"],
                     "age_d": round(c["age_h"] / 24, 1)} for c in running[:10]],
        "unsafe_smart": [{"addr": c["addr"], "sym": c["sym"], "mc": c["mc"], "why": c["reasons"][:2],
                          "stage": c.get("out_at")} for c in unsafe_smart],
    }
    old = load("radar.json", {})
    res["notified"] = notify_new(rows, old.get("notified") or {})
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=list)
    write_page(res)
    for r in rows[:8]:
        print(f"{r['light']:>11} {r['score']:>3}  ${r['sym']}  MC {fmt(r['mc'])}  {r['addr']}")


def notify_new(rows, notified):
    """Push aufs Handy, wenn ein Coin neu auf Grün springt."""
    t = now()
    notified = {a: v for a, v in notified.items() if t - v < 48 * 3600}
    try:
        import notify
    except Exception:
        return notified
    for r in rows:
        if r["light"] != "grün" or t - notified.get(r["addr"], 0) < PUSH_AGAIN_H * 3600:
            continue
        p = r["plan"]
        notify.push(f"Radar GRÜN: ${r['sym']} ({r['score']} Pkt.)",
                    f"MC {fmt(r['mc'])} · {r['age_d']} Tage · {'; '.join(r['notes'][:2]) or 'Muster + Sicherheit ok'}\n"
                    f"Plan: Stop {fmt(p['stop_mc'])} ({p['stop_pct']} %), Hälfte bei {fmt(p['tp1_mc'])} (+{p['tp1_pct']} %), "
                    f"Rest bis {fmt(p['tp_mc'])} (+{p['tp_pct']} %)\n"
                    f"CA {r['addr']}\nKein Kaufsignal, nur unsere Regeln.",
                    click=f"https://dexscreener.com/solana/{r['addr']}", priority=4, tags=["green_circle"])
        notified[r["addr"]] = t
    return notified


# ---------------------------------------------------------------- Seite
def links(a):
    return (f'<a href="https://dexscreener.com/solana/{a}">Chart</a> · <a href="https://rugcheck.xyz/tokens/{a}">RugCheck</a> · '
            f'<a href="https://trench.bot/bundles/{a}">Trench</a> · <a href="https://app.bubblemaps.io/sol/token/{a}">Bubblemaps</a>')


def ul(d):
    return "".join(f'<li>{"✅" if ok else "❌"} {html.escape(k)}</li>' for k, ok in d.items())


def card(r):
    a, p = r["addr"], r["plan"]
    bars = "".join(f'<div class="bar"><span>{html.escape(k)}</span><i style="width:{max(2, v / MAXP[k] * 100):.0f}%"></i><b>{v}</b></div>'
                   for k, v in r["parts"].items() if k in MAXP)
    bh = r["parts"].get("Bot-Historie", 0)
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in r["notes"])
    warns = "".join(f"<li>⚠️ {html.escape(w)}</li>" for w in r["warns"])
    claud = ul((r["shape"] or {}).get("claudia") or {})
    smart_w = ""
    if r.get("smart"):
        smart_w = "".join(f'<code>{w[:4]}…{w[-4:]}</code> ' for w in r["smart"]["wallets"])
    return f"""<div class="card c-{r['light'].replace(' ', '-')}"><div class="head"><h3>${html.escape(r['sym'])}</h3>
<span class="pill">{html.escape(r['light'].upper())} · {r['score']} Pkt.</span></div>
<div class="small muted">{html.escape(r['name'] or '')} · {r['age_d']} Tage · Trichter: {html.escape(r['funnel'])}</div>
<code>{a}</code>
<div class="kv"><span>MC</span><span>{fmt(r['mc'])}</span><span>Liquidität</span><span>{fmt(r['liq'])} ({r['liq'] / r['mc'] * 100 if r['mc'] else 0:.0f} %)</span>
<span>1 h / 24 h</span><span>{r['ch1']:+.0f} % / {r['ch24']:+.0f} %</span><span>Käufe/Verk. 24 h</span><span>{r['buys']} / {r['sells']}</span>
<span>Top-10 / Insider / Cluster</span><span>{r['top10'] or 0:.0f} % / {r['insider'] or 0:.1f} % / {r['cluster'] if r['cluster'] is not None else '–'} %</span></div>
<div class="bars">{bars}{f'<div class="bar"><span>Bot-Historie</span><b>{bh:+d}</b></div>' if bh else ''}</div>
<div class="plan"><b>Plan nach unseren Daten</b> (MC jetzt {fmt(r['mc'])}):<br>Stop bei <b>{fmt(p['stop_mc'])}</b> ({p['stop_pct']} %) ·
Hälfte raus bei <b>{fmt(p['tp1_mc'])}</b> (+{p['tp1_pct']} %) · Rest bis <b>{fmt(p['tp_mc'])}</b> (+{p['tp_pct']} %, {html.escape(p['tp_note'])}) oder Trailing</div>
<details><summary>Details</summary><ul class="small">{notes}{warns}</ul>
<p class="small"><b>Basis-Muster</b></p><ul class="small">{ul(r['checks'])}</ul>
{f'<p class="small"><b>CLAUDIA-Profil</b></p><ul class="small">{claud}</ul>' if claud else ''}
{f'<p class="small"><b>Smart-Wallets:</b> {smart_w}</p>' if smart_w else ''}</details>
<div class="small">{links(a)}</div></div>"""


MAXP = {"Smart-Money": 30, "Rückblick/CLAUDIA": 25, "Basis-Muster": 20, "Markt-Qualität": 15}

CSS_EXTRA = """
.head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.pill{font-size:12px;font-weight:700;border-radius:99px;padding:2px 10px;background:var(--chip)}
.c-grün{border-left:5px solid var(--good)}.c-grün .pill{background:var(--good);color:#fff}
.c-gelb{border-left:5px solid #d9a21b}.c-gelb .pill{background:#d9a21b;color:#111}
.c-grau{border-left:5px solid var(--muted)}.c-gesperrt{border-left:5px solid var(--bad);opacity:.8}
.bars{margin:8px 0}.bar{display:grid;grid-template-columns:120px 1fr 28px;gap:8px;align-items:center;font-size:12px}
.bar i{display:block;height:8px;border-radius:4px;background:var(--a)}.bar b{text-align:right}
.plan{background:var(--chip);border-radius:8px;padding:8px 10px;font-size:13px;margin:8px 0}
details summary{cursor:pointer;font-size:13px;color:var(--muted)}
.stat{font-size:24px;font-weight:700}.note{border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:13px}
"""


def write_page(res):
    from fomo_bot import CSS, berlin
    rows, L = res["rows"], res["learn"]
    hold_txt = (f" · Bestände der Smart-Wallets vom {berlin(res['holdings_t']):%d.%m. %H:%M} Uhr"
                if res.get("holdings_t") else " · keine aktuellen Bestandsdaten der Smart-Wallets")
    by = {k: [r for r in rows if r["light"] == k] for k in ("grün", "gelb", "grau", "gesperrt")}
    sec = lambda lst, empty: "".join(card(r) for r in lst) or f'<p class="muted">{empty}</p>'
    running = "".join(f'<tr><td><b>${html.escape(c["sym"])}</b><br><code>{c["addr"]}</code></td><td>{fmt(c["mc"])}</td>'
                      f'<td>{c["ch1"]:+.0f} %</td><td>{c["ch24"]:+.0f} %</td><td>{c["age_d"]} T</td>'
                      f'<td class="small">{links(c["addr"])}</td></tr>' for c in res["running"])
    unsafe = "".join(f'<tr><td><b>${html.escape(c["sym"])}</b><br><code>{c["addr"]}</code></td><td>{fmt(c["mc"])}</td>'
                     f'<td class="small">{html.escape(c["stage"] or "")}: {html.escape("; ".join(c["why"]))}</td></tr>'
                     for c in res["unsafe_smart"])
    past = "".join(f'<tr><td><b>${html.escape(c.get("sym") or "?")}</b><br><code>{c["addr"]}</code></td>'
                   f'<td>{fmt(c.get("mc_first"))}</td><td><b>{c["x_peak"]:.1f}x</b></td><td>{(c.get("x_now") or 0):.1f}x</td></tr>'
                   for c in L.get("top_past") or [])
    bots = "".join(f'<tr><td>{html.escape(n)}</td><td>{b["n"]}</td><td>{b["win"] if b["win"] is not None else "–"} %</td>'
                   f'<td>{b["pnl"]:+.2f} $</td><td>{b["open"]}</td></tr>'
                   for n, b in sorted((L.get("bots") or {}).items(), key=lambda x: -x[1]["pnl"]))
    reach = L.get("reach") or {}
    learn_html = f"""<div class="grid">
<div class="card"><h3>Bot-Trades mit Hoch/Tief ({L.get('trades', 0)})</h3>
<div class="kv"><span>Hoch im Median</span><span>{L.get('max_median', '–')} %</span>
<span>erreichten +10 / +20 / +50 / +100 %</span><span>{reach.get(10, '–')} / {reach.get(20, '–')} / {reach.get(50, '–')} / {reach.get(100, '–')} %</span>
<span>Gewinner fielen vorher bis</span><span>{L.get('winner_dip_median', '–')} % (Median)</span>
<span>24 h nach Verkauf</span><span>{L.get('after24_median', '–')} % (Median)</span></div>
<p class="small muted">Daraus: Stop mindestens {res['stop_default']} % bzw. unter dem Tief der letzten 12 h (max. 30 %),
Hälfte bei +{round(TP1 * 100)} %, Rest bis +{res['tp_default']} % (Coin-Check) bzw. +100 % beim Rückblick-Muster.</p></div>
<div class="card"><h3>Frühere Coins (Coin-Check)</h3>
<div class="kv"><span>unter 1 Mio. MC: 1,5x+</span><span>{L.get('small_hit15', '–')}</span>
<span>Peak im Median (unter 1 Mio.)</span><span>{L.get('small_peak_median', '–')}x</span>
<span>über 3 Mio. MC: 1,5x+</span><span>{L.get('big_hit15', '–')}</span></div></div></div>"""
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Einstiegs-Radar</title>
<style>{CSS}{CSS_EXTRA}</style></head><body><div class="wrap">
<header><h1>Einstiegs-Radar</h1><span><a class="small" href="index.html">Bots</a> · <a class="small" href="wallets.html">Smart-Wallets</a> · <a class="small" href="muster.html">Muster</a></span></header>
<p class="small muted">Stand {berlin(res['t']):%d.%m.%Y %H:%M} Uhr · Lauf {res['dauer_s']} s · {res['counts'].get('Eingang', 0)} Coins geprüft,
{len(rows)} sicher{hold_txt}</p>
<div class="grid"><div class="card"><div class="stat" style="color:var(--good)">{len(by['grün'])}</div>Einstieg nach Regeln</div>
<div class="card"><div class="stat" style="color:#d9a21b">{len(by['gelb'])}</div>Rücksetzer abwarten</div>
<div class="card"><div class="stat">{len(by['grau'])}</div>Beobachten</div>
<div class="card"><div class="stat" style="color:var(--bad)">{len(by['gesperrt'])}</div>gesperrt (Bot-Verlust)</div></div>
<h2>🟢 Einstieg nach unseren Regeln</h2><div class="grid">{sec(by['grün'], 'Gerade nichts. Nichtstun ist auch eine Entscheidung.')}</div>
<h2>🟡 Rücksetzer abwarten (pumpt gerade)</h2><div class="grid">{sec(by['gelb'], 'Nichts.')}</div>
<h2>⚪ Beobachten</h2><div class="grid">{sec(by['grau'][:12], 'Nichts.')}</div>
<h2>🔒 Gesperrt (ein Bot hat damit in den letzten 48 h verloren)</h2>
<div class="card scroll">{'<table><tr><th>Coin</th><th>Punkte</th><th>MC</th><th>1 h / 24 h</th></tr>' + "".join(f'<tr><td><b>${html.escape(r["sym"])}</b><br><code>{r["addr"]}</code></td><td>{r["score"]}</td><td>{fmt(r["mc"])}</td><td>{r["ch1"]:+.0f} % / {r["ch24"]:+.0f} %</td></tr>' for r in by['gesperrt']) + '</table>' if by['gesperrt'] else '<p class="muted">Nichts.</p>'}</div>
<h2>🚀 Läuft gerade (sicher, aber nicht reinjagen)</h2>
<div class="card scroll">{'<table><tr><th>Coin</th><th>MC</th><th>1 h</th><th>24 h</th><th>Alter</th><th></th></tr>' + running + '</table>' if running else '<p class="muted">Nichts.</p>'}</div>
<h2>⛔ Smart-Money kauft, aber unsicher</h2>
<div class="card scroll">{'<table><tr><th>Coin</th><th>MC</th><th>Warum raus</th></tr>' + unsafe + '</table>' if unsafe else '<p class="muted">Nichts.</p>'}</div>
<h2>📚 Was unsere Daten sagen</h2>{learn_html}
<h2>Frühere Top-Läufe</h2>
<div class="card scroll">{'<table><tr><th>Coin</th><th>MC beim ersten Sehen</th><th>Peak</th><th>Jetzt</th></tr>' + past + '</table>' if past else '<p class="muted">Noch kein Coin-Check.</p>'}</div>
<h2>Bots im Vergleich</h2>
<div class="card scroll"><table><tr><th>Bot</th><th>Trades</th><th>Treffer</th><th>Gewinn</th><th>offen</th></tr>{bots}</table></div>
<h2>So entsteht die Ampel</h2><div class="note">Nur Coins, die Contract-, Holder- und Cluster-Check bestehen, bekommen Punkte (max. 100):
Smart-Money bis 30 (gute Pool-Wallet kauft +15, weitere +8, hält +5) · Rückblick-Muster 25 bzw. CLAUDIA-Profil 5 je Check ·
Basis-Muster bis 20 · Markt-Qualität bis 15 · Bot-Historie +5 / −20 (Verlust-Sperre).
<b>Grün</b> ab {GREEN} Punkten und nicht mitten im Pump (1 h ≤ +{PUMP_H1} %), <b>Gelb</b> ab {YELLOW} Punkten, aber pumpt gerade, <b>Grau</b> ab {GREY}.
Stop und Ziel kommen aus Hoch/Tief aller Bot-Trades und dem Coin-Check. Das ist kein Kaufsignal und keine Anlageberatung,
sondern die Auswertung unserer eigenen Regeln. Kleine gleiche Einsätze, Stop direkt als Order, nie im Fall nachkaufen.</div>
<footer>Aktualisiert sich etwa alle 30 Minuten. Bei neuem Grün kommt eine Push-Nachricht (wenn ntfy eingerichtet ist).</footer>
</div><script>setTimeout(function(){{location.reload()}},300000)</script></body></html>"""
    os.makedirs(os.path.dirname(PAGE), exist_ok=True)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(doc)


if __name__ == "__main__":
    main()
