"""
Memecoin-Funnel (Solana)
Oben kommen alle Coins mit Traction rein, jede Stufe siebt aus,
unten bleiben nur die übrig, die ALLE Regeln bestehen.

  Stufe 0  Eingang      GeckoTerminal Trending + DexScreener Profile/Boosts/Takeovers
  Stufe 1  Markt        Market Cap, Volumen, Alter, Liquidität im Verhältnis zur MC
  Stufe 2  Contract     Mint/Freeze revoked, LP gelockt, keine RugCheck-Gefahren
  Stufe 3  Holder       Top-10, Insider, Dev, Bundle-Muster (gleich große Wallets)
  Stufe 4  Handel       echtes Volumen statt Bot-Spam, Kauf/Verkauf-Verhältnis
  Stufe 5  Timing       kein laufender Pump  -> sonst Watchlist statt Ergebnis

Start:   python memecoin_funnel.py      (nur Python 3.9+, keine Zusatz-Pakete)
Ergebnis: Konsole + funnel_ergebnis.html (öffnet sich im Browser)

Bundles über Trench/Bubblemaps prüft das Script NICHT – vor jedem Kauf selbst checken.
Kein Kaufsignal. Nur ein Filter nach deinen eigenen Regeln.
"""

import html
import json
import os
import time
import urllib.request
import webbrowser
from datetime import datetime

# ======================= REGELN (hier anpassen) =======================
MIN_MC = 100_000           # Market Cap mindestens ($)
MAX_MC = 50_000_000        # darüber kein Memecoin im Sinne der Liste
MIN_VOL_24H = 100_000      # Volumen 24h mindestens ($)
MIN_AGE_H = 6              # Coin mindestens so alt (Stunden)
LIQ_MC_FAIL = 0.05         # Liquidität unter 5 % der MC = raus (Tsuchi/MrBeast)
LIQ_MC_WARN = 0.10         # unter 10 % = Warnung

MIN_LP_LOCKED = 90         # LP im Hauptpool mindestens % gelockt/verbrannt

TOP10_FAIL = 50            # Top-10 (ohne Pool) ab % = raus
TOP10_WARN = 30            # ab % = Warnung
INSIDER_FAIL = 10          # Insider-Netzwerke ab % = raus
INSIDER_WARN = 5
DEV_FAIL = 5               # Dev hält ab % = raus
BUNDLE_MIN_WALLETS = 5     # so viele Top-Wallets fast gleich groß = Bundle-Verdacht (MogCat)
BUNDLE_TOLERANCE = 0.10    # "fast gleich" = ±10 %
BUNDLE_MIN_PCT = 0.5       # nur Wallets ab 0,5 % des Supplys zählen

MIN_AVG_TRADE_USD = 15     # Ø-Tradegröße darunter = Bot-Volumen (MrBeast: ~3 $)
BUY_SELL_MAX = 2.5         # fast nur Käufe = Honeypot-/Pump-Verdacht
BUY_SELL_MIN = 0.4         # starker Verkaufsdruck

PUMP_24H = 400             # +% in 24h = Timing (Watchlist)
PUMP_1H = 30               # +% in 1h  = Timing (Watchlist)

CLUSTER_FAIL = 10          # verbundene Top-Holder (gleiche Geldquelle) halten ab % = raus
CLUSTER_WARN = 5           # ab % = Warnung

REIFE_TAGE = 14            # ab so vielen Tagen gilt ein Coin als "reif" (nur Markierung, kein Filter)
# =====================================================================

UA = {"User-Agent": "Mozilla/5.0 memecoin-funnel"}
SKIP = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
STAGES = ["Eingang", "Markt", "Contract", "Holder", "Cluster", "Handel", "Timing"]


def get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- Stufe 0
DESCRIPTIONS = {}   # Beschreibung aus DexScreener-Profilen, für die Narrativ-Notiz


def collect_candidates():
    src = {}

    def add(addr, tag, desc=None):
        if addr and addr not in SKIP:
            src.setdefault(addr, set()).add(tag)
            if desc and addr not in DESCRIPTIONS:
                DESCRIPTIONS[addr] = " ".join(desc.split())

    for dur in ("1h", "6h", "24h"):
        try:
            data = get(f"https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?duration={dur}")
            for p in data.get("data", []):
                add(p["relationships"]["base_token"]["data"]["id"].replace("solana_", ""), "trending")
        except Exception as e:
            print(f"  GeckoTerminal {dur} nicht erreichbar: {e}")
    for url, tag in (
        ("https://api.dexscreener.com/token-profiles/latest/v1", "profil"),
        ("https://api.dexscreener.com/token-boosts/top/v1", "boost"),
        ("https://api.dexscreener.com/token-boosts/latest/v1", "boost"),
        ("https://api.dexscreener.com/community-takeovers/latest/v1", "cto"),
    ):
        try:
            for x in get(url):
                if x.get("chainId") == "solana":
                    add(x.get("tokenAddress"), tag, x.get("description"))
        except Exception as e:
            print(f"  DexScreener ({tag}) nicht erreichbar: {e}")
    return src


def market_data(src):
    addrs, pairs = list(src), []
    for i in range(0, len(addrs), 30):
        try:
            pairs += get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(addrs[i:i + 30]))
        except Exception as e:
            print(f"  DexScreener Marktdaten Fehler: {e}")
    by_tok = {}
    for p in pairs:
        a = (p.get("baseToken") or {}).get("address")
        if a in src:
            by_tok.setdefault(a, []).append(p)

    now = time.time() * 1000
    coins = []
    for a, ps in by_tok.items():
        ps.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
        m = ps[0]
        tx = (m.get("txns") or {}).get("h24") or {}
        ch = m.get("priceChange") or {}
        info = m.get("info") or {}
        coins.append({
            "addr": a,
            "sym": m["baseToken"].get("symbol", "?"),
            "name": m["baseToken"].get("name", ""),
            "desc": DESCRIPTIONS.get(a, ""),
            "socials": [s.get("type") or "link" for s in info.get("socials") or []],
            "website": bool(info.get("websites")),
            "mc": m.get("marketCap") or m.get("fdv") or 0,
            "liq": sum((p.get("liquidity") or {}).get("usd") or 0 for p in ps),
            "vol24": sum((p.get("volume") or {}).get("h24") or 0 for p in ps),
            "ch1": ch.get("h1") or 0,
            "ch24": ch.get("h24") or 0,
            "buys": tx.get("buys") or 0,
            "sells": tx.get("sells") or 0,
            "age_h": (now - min((p.get("pairCreatedAt") or now) for p in ps)) / 3.6e6,
            "boosts": (m.get("boosts") or {}).get("active") or 0,
            "pairs": {p.get("pairAddress") for p in ps},
            "main_pair": m.get("pairAddress"),
            "src": "+".join(sorted(src[a])),
            "reasons": [], "warns": [], "out_at": None,
        })
    return coins


# ---------------------------------------------------------------- Stufen 1-5
def stage_market(c):
    r = []
    if c["mc"] < MIN_MC:
        r.append(f"MC nur {fmt(c['mc'])}")
    if c["mc"] > MAX_MC:
        r.append(f"MC {fmt(c['mc'])} (kein Memecoin mehr)")
    if c["vol24"] < MIN_VOL_24H:
        r.append(f"Volumen nur {fmt(c['vol24'])}")
    if c["age_h"] < MIN_AGE_H:
        r.append(f"erst {c['age_h']:.1f} h alt")
    ratio = c["liq"] / c["mc"] if c["mc"] else 0
    if ratio < LIQ_MC_FAIL:
        r.append(f"Liquidität nur {ratio * 100:.1f} % der MC")
    elif ratio < LIQ_MC_WARN:
        c["warns"].append(f"Liquidität {ratio * 100:.0f} % der MC")
    return r


def stage_contract(c, rc):
    r, tok = [], rc.get("token") or {}
    if not tok:
        return ["keine RugCheck-Daten"]
    if tok.get("mintAuthority"):
        r.append("Mint Authority aktiv")
    if tok.get("freezeAuthority"):
        r.append("Freeze Authority aktiv")
    markets = rc.get("markets") or []
    main = next((m for m in markets if m.get("pubkey") == c["main_pair"]), None)
    if main is None and markets:
        main = max(markets, key=lambda m: (m.get("lp") or {}).get("quoteUSD") or 0)
    lp = ((main or {}).get("lp") or {}).get("lpLockedPct")
    if lp is None:
        r.append("LP-Lock unbekannt")
    elif lp < MIN_LP_LOCKED:
        r.append(f"LP nur {lp:.0f} % gelockt")
    for risk in rc.get("risks") or []:
        if risk.get("level") == "danger":
            r.append("RugCheck: " + risk.get("name", "?"))
    if rc.get("rugged"):
        r.append("als gerugged markiert")
    return r


def bundle_pattern(pcts):
    """True, wenn die Top-Wallets verdächtig gleich groß sind (MogCat: 0,91/0,91/0,90/0,86/0,85 %).
    Normale Verteilungen fallen von Wallet zu Wallet deutlich ab (ABU: 3,3/3,1/2,5/2,3/2,3 %)."""
    top = sorted(pcts, reverse=True)[:BUNDLE_MIN_WALLETS]
    if len(top) < BUNDLE_MIN_WALLETS or top[-1] < BUNDLE_MIN_PCT:
        return False
    return (top[0] - top[-1]) / top[0] <= BUNDLE_TOLERANCE


def stage_holder(c, rc):
    r, tok = [], rc.get("token") or {}
    sup = tok.get("supply") or 1
    pools = {m.get("pubkey") for m in rc.get("markets") or []} | c["pairs"]
    holders = [h for h in rc.get("topHolders") or [] if h.get("owner") not in pools]
    top = [h.get("pct") or 0 for h in holders[:10]]
    top10 = sum(top)
    c["top10"] = top10
    if top10 > TOP10_FAIL:
        r.append(f"Top-10 halten {top10:.0f} %")
    elif top10 > TOP10_WARN:
        c["warns"].append(f"Top-10 halten {top10:.0f} %")

    ins = sum(n.get("currentHolding") or 0 for n in rc.get("insiderNetworks") or []) / sup * 100
    c["insider"] = ins
    if ins > 100:
        r.append("Insider-Daten unplausibel")
    elif ins > INSIDER_FAIL:
        r.append(f"Insider halten {ins:.1f} %")
    elif ins > INSIDER_WARN:
        c["warns"].append(f"Insider halten {ins:.1f} %")

    dev = (rc.get("creatorBalance") or 0) / sup * 100
    if dev > DEV_FAIL:
        r.append(f"Dev hält {dev:.1f} %")

    if bundle_pattern(top):
        r.append(f"Bundle-Muster: Top-{BUNDLE_MIN_WALLETS}-Wallets fast gleich groß")
    return r


def stage_cluster(c, rc, checker):
    """Eigene Mini-Bubblemap: hängen die größten Holder über ihre Geldquelle zusammen?"""
    pools = {m.get("pubkey") for m in rc.get("markets") or []} | c["pairs"]
    for m in rc.get("markets") or []:
        for k in ("liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            if isinstance(m.get(k), str):
                pools.add(m[k])
    known = set((rc.get("knownAccounts") or {}).keys())
    holders = [(h["owner"], h.get("pct") or 0) for h in rc.get("topHolders") or []
               if h.get("owner") and h["owner"] not in pools and h["owner"] not in known]
    res = checker.check(c["addr"], holders)
    if res["status"] == "off":
        c["warns"].append(f"Cluster-Check übersprungen ({res.get('why', '')})")
        return []
    if res["status"] == "pending":
        return [f"Cluster-Check läuft noch ({res.get('why', '')})"]
    c["cluster"] = res["cluster_pct"]
    n = sum(len(g) for g in res["groups"])
    if res["cluster_pct"] > CLUSTER_FAIL:
        return [f"Cluster: {n} verbundene Top-Holder halten {res['cluster_pct']:.1f} %"]
    if res["cluster_pct"] > CLUSTER_WARN:
        c["warns"].append(f"Cluster: {n} verbundene Top-Holder halten {res['cluster_pct']:.1f} %")
    return []


def stage_trading(c):
    r = []
    tx = c["buys"] + c["sells"]
    avg = c["vol24"] / tx if tx else 0
    c["avg_trade"] = avg
    if tx > 2000 and avg < MIN_AVG_TRADE_USD:
        r.append(f"Bot-Volumen (Ø Trade nur {avg:.0f} $)")
    ratio = c["buys"] / max(1, c["sells"])
    if ratio > BUY_SELL_MAX:
        r.append(f"fast nur Käufe ({c['buys']}/{c['sells']})")
    if ratio < BUY_SELL_MIN:
        r.append(f"starker Verkaufsdruck ({c['buys']}/{c['sells']})")
    if c["boosts"]:
        c["warns"].append(f"{c['boosts']} bezahlte Boosts")
    return r


def stage_timing(c):
    r = []
    if c["ch24"] >= PUMP_24H:
        r.append(f"+{c['ch24']:.0f} % in 24 h")
    if c["ch1"] >= PUMP_1H:
        r.append(f"+{c['ch1']:.0f} % in 1 h")
    return r


# ---------------------------------------------------------------- Narrativ (KEIN Filter)
# Nur eine Notiz zum Selbst-Checken: Ist die Idee in einem Satz klar? Ist sie neu/aktuell?
# Markennamen/Promis werden markiert, weil das oft Coins ohne echten Bezug sind (MrBeast, TikTok).
BRAND_WORDS = [
    "trump", "elon", "musk", "mrbeast", "beast", "tiktok", "tesla", "apple", "nike", "google",
    "openai", "chatgpt", "nvidia", "netflix", "disney", "kanye", "drake", "taylor", "ronaldo",
    "messi", "biden", "obama", "binance", "cz", "robinhood", "spacex", "amazon", "meta",
]


def reife(c):
    return "reif" if c["age_h"] >= REIFE_TAGE * 24 else "jung"


def narrative_note(c):
    name = c.get("name") or c["sym"]
    desc = c.get("desc") or ""
    if len(desc) > 180:
        desc = desc[:177].rsplit(" ", 1)[0] + " …"
    hints = []
    low = f"{name} {c['sym']}".lower()
    hit = next((w for w in BRAND_WORDS if w in low.replace(" ", "")), None)
    if hit:
        hints.append(f"Promi-/Markenname ({hit}) – offiziell oder nur draufgesetzt?")
    if not c.get("socials") and not c.get("website"):
        hints.append("keine Socials/Website hinterlegt")
    elif "twitter" not in c.get("socials", []):
        hints.append("kein X-Account hinterlegt")
    if not desc:
        hints.append("keine Beschreibung – Idee selbst auf X nachlesen")
    socials = ", ".join(sorted(set(c.get("socials") or []))) + (", Website" if c.get("website") else "")
    return {
        "title": f"{name} (${c['sym']})",
        "desc": desc or "–",
        "socials": socials.strip(", ") or "keine",
        "hints": hints,
        "age": f"{c['age_h'] / 24:.1f} Tage alt – {reife(c)}",
        "x_search": f"https://x.com/search?q=%24{c['sym']}&f=live",
    }


# ---------------------------------------------------------------- Ablauf
def fmt(x):
    if x >= 1e6:
        return f"{x / 1e6:.1f} Mio."
    if x >= 1e3:
        return f"{x / 1e3:.0f}k"
    return f"{x:.0f}"


def run_funnel(coins, rugcheck_fn):
    counts = {"Eingang": len(coins)}
    alive = coins

    def sieve(name, check):
        nonlocal alive
        survivors = []
        for c in alive:
            reasons = check(c)
            if reasons:
                c["reasons"], c["out_at"] = reasons, name
            else:
                survivors.append(c)
        alive = survivors
        counts[name] = len(alive)

    sieve("Markt", stage_market)

    reports = {}
    print(f"  RugCheck für {len(alive)} Coins (dauert etwas) ...")
    for c in alive:
        reports[c["addr"]] = rugcheck_fn(c["addr"])
    sieve("Contract", lambda c: stage_contract(c, reports[c["addr"]]))
    sieve("Holder", lambda c: stage_holder(c, reports[c["addr"]]))
    import cluster_check
    checker = cluster_check.Checker()
    try:
        sieve("Cluster", lambda c: stage_cluster(c, reports[c["addr"]], checker))
    finally:
        checker.save()
    if checker.on:
        print(f"  Cluster-Check: {checker.new} neue Wallets nachgeschlagen, "
              f"{checker.credits_today()} Credits heute")
    sieve("Handel", stage_trading)

    final, watch = [], []
    for c in alive:
        t = stage_timing(c)
        if t:
            c["reasons"], c["out_at"] = t, "Timing"
            watch.append(c)
        else:
            final.append(c)
    counts["Timing"] = len(final)
    return counts, final, watch, coins


def rugcheck_live(addr):
    try:
        rc = get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report")
    except Exception:
        rc = {}
    time.sleep(0.7)
    return rc


def print_funnel(counts, final, watch, coins):
    top = max(counts["Eingang"], 1)
    print("\n" + "=" * 64)
    print("  DER TRICHTER")
    print("=" * 64)
    for i, s in enumerate(STAGES):
        n = counts.get(s, 0)
        bar = "█" * max(1, round(40 * n / top)) if n else ""
        pad = " " * (i * 2)
        print(f"  {s:<9}{pad}{bar} {n}")
    print()
    for s in STAGES[1:]:
        out = [c for c in coins if c["out_at"] == s]
        if out and s != "Timing":
            print(f"  Raus bei {s}: {len(out)}  (z. B. ${out[0]['sym']}: {out[0]['reasons'][0]})")

    print("\n" + "=" * 64)
    print(f"  DURCHGEKOMMEN (alle Regeln bestanden): {len(final)} Coins")
    print("=" * 64)
    for c in sorted(final, key=lambda c: -c["vol24"]):
        show(c)
    if watch:
        print("\n  WATCHLIST (alles ok, aber pumpt gerade – nicht reinjagen):")
        for c in watch:
            n = narrative_note(c)
            print(f"   ${c['sym']:<12} MC {fmt(c['mc']):>9} | {reife(c)} | {', '.join(c['reasons'])}")
            print(f"      CA: {c['addr']}")
            print(f"      Narrativ: {n['desc']}" + (f"  ({'; '.join(n['hints'])})" if n["hints"] else ""))
    print("\nKein Kaufsignal. Vor jedem Kauf: trench.bot + Bubblemaps + X-Account checken.")


def show(c):
    a = c["addr"]
    print(f"\n  ${c['sym']}  MC {fmt(c['mc'])} | Liq {fmt(c['liq'])} | Vol24 {fmt(c['vol24'])} | "
          f"24h {c['ch24']:+.0f} % | {c['age_h'] / 24:.1f} Tage | {reife(c).upper()}")
    print(f"    Top-10 {c.get('top10', 0):.0f} % | Insider {c.get('insider', 0):.1f} % | Ø Trade {c.get('avg_trade', 0):.0f} $")
    for w in c["warns"]:
        print(f"    ! {w}")
    n = narrative_note(c)
    print(f"    Narrativ: {n['title']} – {n['desc']}")
    print(f"      Socials: {n['socials']} | {n['age']}")
    for h in n["hints"]:
        print(f"      ? {h}")
    print(f"      Selbst checken: {n['x_search']}")
    print(f"    CA: {a}")
    print(f"    https://dexscreener.com/solana/{a}")
    print(f"    https://trench.bot/bundles/{a}")
    print(f"    https://app.bubblemaps.io/sol/token/{a}")


def write_html(counts, final, watch, coins, path):
    top = max(counts["Eingang"], 1)
    bars = []
    for s in STAGES:
        n = counts.get(s, 0)
        w = max(4, 100 * n / top) if n else 2
        out = [c for c in coins if c["out_at"] == s and s != "Timing"]
        why = ""
        if out:
            reasons = {}
            for c in out:
                key = c["reasons"][0].split(" ")[0]
                reasons[key] = reasons.get(key, 0) + 1
            why = " · ".join(f"{k} ({v})" for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:3])
        bars.append(
            f'<div class="row"><div class="lbl">{s}</div>'
            f'<div class="track"><div class="bar" style="width:{w:.0f}%">{n}</div></div>'
            f'<div class="why">{html.escape(why)}</div></div>')

    def card(c, kind):
        a = c["addr"]
        extra = "".join(f"<li>{html.escape(x)}</li>" for x in (c["warns"] if kind == "ok" else c["reasons"]))
        n = narrative_note(c)
        hints = "".join(f"<li>{html.escape(h)}</li>" for h in n["hints"])
        narr = (f'<div class="narr"><b>Narrativ (selbst prüfen):</b> {html.escape(n["title"])} – {html.escape(n["desc"])}'
                f'<br><small>Socials: {html.escape(n["socials"])} · {n["age"]} · '
                f'<a href="{n["x_search"]}">auf X suchen</a></small>'
                f'{"<ul class=hint>" + hints + "</ul>" if hints else ""}</div>')
        return (f'<div class="card {kind}"><h3>${html.escape(c["sym"])} <span class="tag">{reife(c)}</span></h3>'
                f'<p>MC {fmt(c["mc"])} · Liq {fmt(c["liq"])} · Vol24 {fmt(c["vol24"])} · 24h {c["ch24"]:+.0f} %</p>'
                f'<ul>{extra}</ul>{narr}<code>{a}</code><p>'
                f'<a href="https://dexscreener.com/solana/{a}">DexScreener</a> · '
                f'<a href="https://rugcheck.xyz/tokens/{a}">RugCheck</a> · '
                f'<a href="https://trench.bot/bundles/{a}">Trench</a> · '
                f'<a href="https://app.bubblemaps.io/sol/token/{a}">Bubblemaps</a></p></div>')

    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Memecoin-Funnel</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f1115;color:#e8e6e3;margin:0;padding:24px 16px;max-width:860px;margin:auto}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#9aa0a6;margin:0 0 24px}}
.row{{display:grid;grid-template-columns:80px 1fr;gap:4px 12px;align-items:center;margin:8px 0}}
.lbl{{font-weight:600}} .track{{display:flex;justify-content:center}}
.bar{{background:linear-gradient(90deg,#3a7bd5,#5ec4a8);color:#08110e;font-weight:700;text-align:center;border-radius:6px;padding:6px 0;min-width:28px}}
.why{{grid-column:2;color:#9aa0a6;font-size:12px;text-align:center}}
h2{{margin-top:32px;font-size:18px}} .card{{border:1px solid #2a2f37;border-radius:10px;padding:12px 14px;margin:10px 0}}
.card.ok{{border-color:#2f7d5f}} .card.watch{{border-color:#a07b2a}} .card h3{{margin:0 0 4px}}
.card p{{margin:4px 0;color:#c7c9cc}} code{{font-size:12px;word-break:break-all;color:#9aa0a6}}

.tag{{font-size:12px;font-weight:600;border:1px solid #3a4250;border-radius:99px;padding:1px 8px;color:#c7c9cc;vertical-align:middle}}
.narr{{background:#171b22;border-radius:8px;padding:8px 10px;margin:8px 0;font-size:14px}}
.narr small{{color:#9aa0a6}} .hint{{margin:6px 0 0;padding-left:18px;color:#e0b45c;font-size:13px}}
a{{color:#7fb3ff}} .note{{color:#9aa0a6;font-size:13px;margin-top:28px}}
</style></head><body>
<h1>Memecoin-Funnel</h1><p class="sub">{datetime.now():%d.%m.%Y %H:%M} Uhr · {counts["Eingang"]} rein, {len(final)} durchgekommen</p>
{''.join(bars)}
<h2>Durchgekommen – alle Regeln bestanden ({len(final)})</h2>{''.join(card(c, 'ok') for c in sorted(final, key=lambda c: -c['vol24'])) or '<p>Diesmal keiner. Lieber kein Trade als ein schlechter.</p>'}
<h2>Watchlist – pumpt gerade ({len(watch)})</h2>{''.join(card(c, 'watch') for c in watch) or '<p>Keiner.</p>'}
<p class="note">Kein Kaufsignal. Bundles (Trench), verbundene Wallets (Bubblemaps) und X-Account vor jedem Kauf selbst prüfen.</p>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    print(f"Memecoin-Funnel – {datetime.now():%d.%m.%Y %H:%M}\n")
    print("Stufe 0: sammle Coins mit Traction ...")
    src = collect_candidates()
    coins = market_data(src)
    print(f"  {len(coins)} Coins im Trichter")
    counts, final, watch, coins = run_funnel(coins, rugcheck_live)
    print_funnel(counts, final, watch, coins)
    path = os.path.abspath("funnel_ergebnis.html")
    write_html(counts, final, watch, coins, path)
    print(f"\nBericht: {path}")
    try:
        webbrowser.open("file://" + path)
    except Exception:
        pass


if __name__ == "__main__":
    main()
    input("\nEnter drücken zum Schließen ...")
