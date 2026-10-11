"""
Rückblick, Schritt 1: Kursverläufe von ein paar Hundert Solana-Memecoins sammeln.

Quellen (möglichst breit, damit nicht nur Gewinner drin sind):
  - GeckoTerminal: Pools mit dem meisten Volumen (alle DEXe und PumpSwap), Trending, neue Pools
  - eigene Historie: alle Coins aus Bots, Radar, Muster-Scan, Coin-Check, Wallet-Sammler
Pro Coin: Haupt-Pool und Supply (DexScreener), stündliche Kerzen bis 41 Tage zurück (GeckoTerminal).

Ergebnis: rueckblick/coins.json.gz (wird auf den Branch "rueckblick-daten" gespeichert, nicht auf main).
Nur Daten lesen.
"""

import glob
import gzip
import json
import os
import time

import memecoin_funnel as funnel

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "rueckblick")
OUT = os.path.join(OUT_DIR, "coins.json.gz")
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
GT_PAUSE = 2.6            # GeckoTerminal: 30 Abfragen pro Minute (etwas Luft lassen)
MAX_COINS = 650
MC_MIN, MC_MAX = 20_000, 300_000_000
MIN_LIQ = 3_000


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


STATS = {"ok": 0, "fehler": 0, "warten": 0}


def gt(path):
    err = None
    for attempt in range(5):
        try:
            res = funnel.get(f"{GT}{path}", timeout=30)
            time.sleep(GT_PAUSE)
            STATS["ok"] += 1
            return res
        except Exception as e:
            err = e
            STATS["warten"] += 1
            time.sleep(20 * (attempt + 1) if "429" in str(e) else GT_PAUSE * (attempt + 2))
    STATS["fehler"] += 1
    log(f"  GeckoTerminal {path[:60]}: {err}")
    return {}


def universe():
    src = {}

    def add(mint, tag):
        if mint and mint not in funnel.SKIP:
            src.setdefault(mint, set()).add(tag)

    def pools(path, tag, pages):
        for page in range(1, pages + 1):
            data = gt(f"{path}{'&' if '?' in path else '?'}page={page}")
            rows = data.get("data") or []
            for p in rows:
                add(p["relationships"]["base_token"]["data"]["id"].replace("solana_", ""), tag)
            if not rows:
                break

    pools("/pools?sort=h24_volume_usd_desc", "volumen", 10)
    pools("/dexes/pumpswap/pools?sort=h24_volume_usd_desc", "pumpswap", 10)
    pools("/dexes/pumpswap/pools?sort=h24_tx_count_desc", "pumpswap-tx", 5)
    pools("/dexes/meteora/pools?sort=h24_volume_usd_desc", "meteora", 3)
    pools("/dexes/raydium/pools?sort=h24_volume_usd_desc", "raydium", 3)
    for d in ("1h", "6h", "24h"):
        pools(f"/trending_pools?duration={d}", "trending", 1)
    pools("/new_pools", "neu", 10)
    log(f"GeckoTerminal: {len(src)} Coins")

    # eigene Historie
    def own(path, fn):
        try:
            with open(os.path.join(HERE, path), encoding="utf-8") as f:
                for m in fn(json.load(f)):
                    add(m, "historie")
        except Exception:
            pass
    for path in glob.glob(os.path.join(HERE, "bot_state_*.json")):
        own(os.path.basename(path), lambda s: [t["addr"] for t in s.get("closed", [])] + list(s.get("open", {})))
    own("bot_meta.json", lambda m: list(((m.get("radar") or {}).get("pools") or {})))
    own("pattern_scan.json", lambda p: [r["addr"] for k in ("passed", "others", "claudia") for r in p.get(k, [])])
    own("coin_check.json", lambda c: list((c.get("coins") or {})) if isinstance(c.get("coins"), dict)
        else [x.get("addr") for x in c.get("coins") or []])
    own("wallets.json", lambda w: list(w.get("winners", {})))
    own("rueckblick/gesehen.json", lambda g: list(g))
    log(f"mit eigener Historie: {len(src)} Coins")
    return src


def dex_info(mints):
    out = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        try:
            pairs = funnel.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
        except Exception as e:
            log(f"  DexScreener: {e}")
            continue
        for p in pairs or []:
            a = (p.get("baseToken") or {}).get("address")
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if a not in mints or (a in out and out[a]["liq"] >= liq):
                continue
            try:
                price = float(p.get("priceUsd") or 0)
            except ValueError:
                price = 0
            mc = p.get("marketCap") or p.get("fdv") or 0
            out[a] = {"pair": p.get("pairAddress"), "liq": liq, "mc": mc, "price": price,
                      "supply": mc / price if price else 0, "sym": p["baseToken"].get("symbol", "?"),
                      "name": p["baseToken"].get("name", ""), "dex": p.get("dexId"),
                      "created": (p.get("pairCreatedAt") or 0) / 1000,
                      "created_min": min(out.get(a, {}).get("created_min") or 9e12, (p.get("pairCreatedAt") or 9e15) / 1000)}
        time.sleep(0.4)
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    old = {}
    if os.path.exists(OUT):       # vorhandene Daten weiterverwenden, nur fehlende/alte Coins neu laden
        with gzip.open(OUT, "rt", encoding="utf-8") as f:
            old = json.load(f).get("coins", {})
    src = universe()
    info = dex_info(src)
    coins = [(m, i) for m, i in info.items()
             if MC_MIN <= i["mc"] <= MC_MAX and i["liq"] >= MIN_LIQ and i["supply"] > 0 and i["pair"]]
    # Historie zuerst (sonst fehlen die gestorbenen Coins), dann neue Coins, dann der Rest
    coins.sort(key=lambda x: ("historie" not in src[x[0]], x[0] in old, -x[1]["liq"]))
    coins = coins[:MAX_COINS]
    log(f"{len(coins)} Coins mit Marktdaten ({sum(m in old for m, _ in coins)} schon vorhanden), lade stündliche Kerzen ...")
    data = dict(old)
    deadline = time.time() + 70 * 60
    for n, (m, i) in enumerate(coins, 1):
        if time.time() > deadline:
            log("Zeitlimit erreicht, speichere den Stand.")
            break
        res = gt(f"/pools/{i['pair']}/ohlcv/hour?aggregate=1&limit=1000&currency=usd&token={m}")
        rows = ((res.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if len(rows) < 12:
            continue
        rows.sort(key=lambda r: r[0])
        data[m] = {**{k: i[k] for k in ("sym", "name", "pair", "supply", "liq", "mc", "dex", "created_min")},
                   "src": sorted(src[m]), "ohlcv": [[int(r[0])] + [float(x) for x in r[1:6]] for r in rows]}
        if n % 50 == 0:
            log(f"  {n}/{len(coins)}  {STATS}")
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump({"t": time.time(), "coins": data, "stats": STATS}, f)
    log(f"Fertig: {len(data)} Coins gespeichert ({os.path.getsize(OUT) / 1e6:.1f} MB), {STATS}")


if __name__ == "__main__":
    main()
