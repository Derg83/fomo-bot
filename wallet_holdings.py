"""
Smart-Wallet-Bestände: Welche Coins halten unsere Pool-Wallets gerade, und wie schneiden die nach unseren Regeln ab?

  1. Pool-Wallets aus wallet_pool.csv
  2. aktuelle Token-Bestände über Helius (getTokenAccountsByOwner, Token + Token-2022)
  3. Kurs/MC/Liquidität über DexScreener, nur Bestände ab MIN_USD
  4. jeder Coin durch den Trichter (inkl. RugCheck, Cluster) und den Muster-Score (0-6)

Start:   HELIUS_API_KEY=... python wallet_holdings.py
Ergebnis: wallet_holdings.json (für Auswertungen). Nur Daten lesen, kein Handel.
"""

import csv
import json
import os
import time
import urllib.request

import memecoin_funnel as funnel
import pattern_scan

HERE = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.join(HERE, "wallet_pool.csv")
OUT = os.path.join(HERE, "wallet_holdings.json")
PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
MIN_USD = 100        # kleinere Bestände = Staub/Reste, zählen nicht
MIN_LIQ = 10_000     # Coins mit weniger Liquidität sind praktisch tot


def num(x):
    try:
        return float(str(x).replace(".", "").replace(",", ".")) if "," in str(x) else float(x)
    except ValueError:
        return 0.0


def pool_wallets():
    with open(POOL, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    return {r["Wallet"]: {"pnl10": num(r["Gewinn letzte 10 (SOL)"]), "ohne_top": num(r["Ohne größten Treffer (SOL)"]),
                          "win": num(r["Treffer %"]), "wal": r.get("Wal") == "ja"} for r in rows if r.get("Wallet")}


def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                res = json.loads(r.read().decode())
            if "error" in res:
                raise RuntimeError(res["error"])
            return res["result"]
        except Exception as e:
            if attempt == 3:
                print(f"  {method} fehlgeschlagen: {e}")
                return None
            time.sleep(2 + attempt * 2)


def holdings(url, wallet):
    out = {}
    for prog in PROGRAMS:
        res = rpc(url, "getTokenAccountsByOwner", [wallet, {"programId": prog}, {"encoding": "jsonParsed"}])
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"].get("uiAmount") or 0
            if amt > 0 and info["mint"] not in funnel.SKIP:
                out[info["mint"]] = out.get(info["mint"], 0) + amt
        time.sleep(0.3)
    return out


def prices(mints):
    out = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        try:
            for p in funnel.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30])):
                a = (p.get("baseToken") or {}).get("address")
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if a in out and out[a]["liq"] >= liq:
                    continue
                out[a] = {"price": float(p.get("priceUsd") or 0), "liq": liq}
        except Exception as e:
            print(f"  Kurse fehlgeschlagen: {e}")
        time.sleep(0.5)
    return out


def main():
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        raise SystemExit("HELIUS_API_KEY fehlt.")
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    wallets = pool_wallets()
    print(f"{len(wallets)} Pool-Wallets")
    held = {w: holdings(url, w) for w in wallets}
    all_mints = {m for h in held.values() for m in h}
    print(f"{len(all_mints)} verschiedene Tokens in den Wallets")
    px = prices(all_mints)

    by_coin = {}
    for w, h in held.items():
        for m, amt in h.items():
            q = px.get(m)
            if not q or q["liq"] < MIN_LIQ:
                continue
            usd = amt * q["price"]
            if usd >= MIN_USD:
                by_coin.setdefault(m, []).append({"wallet": w, "usd": round(usd, 2), **wallets[w]})
    print(f"{len(by_coin)} Coins mit Beständen ab {MIN_USD} $")

    coins = funnel.market_data({m: {"smart"} for m in by_coin})
    counts, final, watch, allc = funnel.run_funnel(coins, funnel.rugcheck_live)
    rows = []
    for c in allc:
        checks, age, liq, volmc = pattern_scan.pattern(c)
        ws = sorted(by_coin.get(c["addr"], []), key=lambda x: -x["usd"])
        rows.append({
            "addr": c["addr"], "sym": c["sym"], "name": c["name"], "mc": c["mc"], "liq": c["liq"],
            "vol24": c["vol24"], "ch1": c["ch1"], "ch24": c["ch24"], "age_d": round(age, 1),
            "funnel": "bestanden" if c in final else ("Timing" if c in watch else f"raus: {c['out_at']}"),
            "funnel_why": c["reasons"], "warns": c["warns"], "checks": checks, "score": sum(checks.values()),
            "wallets": ws, "n_wallets": len(ws), "n_good": sum(1 for x in ws if x["ohne_top"] > 0),
            "usd_total": round(sum(x["usd"] for x in ws), 2),
        })
    rows.sort(key=lambda r: (r["funnel"] == "bestanden", r["n_good"], r["score"], r["usd_total"]), reverse=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"t": time.time(), "counts": counts, "wallets": len(wallets), "coins": rows}, f, indent=1)
    for r in rows[:15]:
        print(f"${r['sym']:<10} {r['funnel']:<14} Muster {r['score']}/6  Wallets {r['n_wallets']} "
              f"(gut {r['n_good']})  {r['usd_total']:.0f} $  MC {funnel.fmt(r['mc'])}  {r['addr']}")


if __name__ == "__main__":
    main()
