"""
Rückblick, Schritt 3: Wer hat bei den Gewinner-Coins VOR dem Lauf gekauft?

Für jeden Gewinner aus rueckblick/analyse.json (3x+ vom Tief):
  - Transaktionen des Haupt-Pools im Zeitfenster vor dem Laufbeginn lesen (Helius)
  - Käufer = Unterzeichner, deren Token-Bestand steigt und die dafür SOL/USDC zahlen
Danach: Wallets, die bei mehreren Gewinnern früh dabei waren, werden mit den Regeln des
Wallet-Sammlers bewertet (Bot? Sniper? letzte 10 Trades im Plus?) und in wallets.json übernommen.
Die Größe der Wallet ist egal – es zählt nur, ob sie wiederholt früh richtig liegt.

Start:   HELIUS_API_KEY=... python rueckblick_wallets.py
Ergebnis: rueckblick/frueh_kaeufer.json, Wallets in wallets.json (Quelle "Früh-Gewinner")
"""

import gzip
import json
import os
import random
import time

import wallet_collector as wc

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYSE = os.path.join(HERE, "rueckblick", "analyse.json")
COINS = os.path.join(HERE, "rueckblick", "coins.json.gz")
OUT = os.path.join(HERE, "rueckblick", "frueh_kaeufer.json")

MAX_WINNERS = 30              # so viele Gewinner auswerten (die größten Läufe zuerst)
MAX_START_MC = 3_000_000      # Lauf muss unter dieser MC begonnen haben
WINDOW_BEFORE_H = 12          # Käufe ab 12 h vor dem Tief ...
WINDOW_AFTER_H = 2            # ... bis 2 h danach zählen als "früh"
MAX_SIG_PAGES = 300           # höchstens so viele Seiten à 1000 Signaturen zurückblättern (1 Credit je Seite)
TX_PER_WINNER = 250           # so viele Transaktionen aus dem Zeitfenster lesen
MIN_BUY_SOL = 0.05            # kleinere Käufe = Staub/Bots
SCORE_MAX = 120               # so viele Kandidaten bewerten (je ~110 Credits)
CREDIT_RESERVE = 60_000       # so viele Credits im Monat übrig lassen (Trichter-Cluster-Check)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def window_sigs(rpc, pool, t0, t1):
    """Signaturen des Pools zwischen t0 und t1 (rückwärts blättern bis vor t0)."""
    out, before, pages = [], None, 0
    while pages < MAX_SIG_PAGES:
        params = {"limit": 1000}
        if before:
            params["before"] = before
        page = rpc.call("getSignaturesForAddress", [pool, params]) or []
        pages += 1
        if not page:
            break
        for s in page:
            bt = s.get("blockTime") or 0
            if t0 <= bt <= t1 and not s.get("err"):
                out.append(s)
        before = page[-1]["signature"]
        if (page[-1].get("blockTime") or 0) < t0:
            break
    return out, pages


def early_buyers(rpc, w, pool):
    t0 = w["start_t"] - WINDOW_BEFORE_H * 3600
    t1 = w["start_t"] + WINDOW_AFTER_H * 3600
    sigs, pages = window_sigs(rpc, pool, t0, t1)
    if len(sigs) > TX_PER_WINNER:
        sigs = random.Random(1).sample(sigs, TX_PER_WINNER)
    buyers = {}
    for s in sigs:
        try:
            tx = rpc.call("getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1}])
        except RuntimeError:
            continue
        if not tx:
            continue
        keys = wc.account_keys(tx)
        if not keys:
            continue
        payer = keys[0]
        e = wc.parse_tx(tx, payer, rpc.sol_usd)
        if e["kind"] == "buy" and e.get("mint") == w["addr"] and -e["sol"] >= MIN_BUY_SOL:
            b = buyers.setdefault(payer, {"sol": 0.0, "t": e["t"]})
            b["sol"] += -e["sol"]
            b["t"] = min(b["t"], e["t"])
    log(f"  ${w['sym']}: {pages} Seiten, {len(sigs)} Transaktionen im Fenster, {len(buyers)} Früh-Käufer")
    return buyers


def main():
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        raise SystemExit("HELIUS_API_KEY fehlt.")
    with open(ANALYSE, encoding="utf-8") as f:
        winners = json.load(f)["winners"]
    with gzip.open(COINS, "rt", encoding="utf-8") as f:
        coins = json.load(f)["coins"]
    data = wc.load()
    rpc = wc.Rpc(key, data)
    rpc.gtfa = data.get("gtfa", True)
    cut = time.time() - 35 * 86400
    todo = [w for w in winners if w["start_mc"] <= MAX_START_MC and w["start_t"] >= cut and w["addr"] in coins]
    todo = todo[:MAX_WINNERS]
    log(f"{len(todo)} Gewinner auswerten (Credits übrig: {rpc.credits_left():,})")

    per_wallet = {}
    report = []
    for w in todo:
        if rpc.credits_left() < CREDIT_RESERVE + 20_000:
            log("Credit-Grenze erreicht, höre mit den Gewinnern auf.")
            break
        try:
            buyers = early_buyers(rpc, w, coins[w["addr"]]["pair"])
        except Exception as e:
            log(f"  ${w['sym']}: Fehler {e}")
            continue
        report.append({"addr": w["addr"], "sym": w["sym"], "mult": w["mult"], "start_mc": w["start_mc"],
                       "start_t": w["start_t"], "buyers": len(buyers)})
        for a, b in buyers.items():
            pw = per_wallet.setdefault(a, {"coins": {}, "sol": 0.0})
            pw["coins"][w["addr"]] = {"sym": w["sym"], "sol": round(b["sol"], 3), "mult": w["mult"]}
            pw["sol"] += b["sol"]

    # Kandidaten: zuerst die, die bei mehreren Gewinnern früh waren
    cands = sorted(per_wallet.items(), key=lambda x: (-len(x[1]["coins"]), -x[1]["sol"]))
    log(f"{len(per_wallet)} Früh-Käufer insgesamt, {sum(1 for _, v in cands if len(v['coins']) >= 2)} bei 2+ Gewinnern")
    scored = 0
    for a, v in cands:
        if scored >= SCORE_MAX or rpc.credits_left() < CREDIT_RESERVE:
            break
        w = data["wallets"].setdefault(a, {"addr": a, "first_seen": time.time(), "coins": {}, "status": "Neu"})
        for m in v["coins"]:
            w["coins"][m] = "Früh-Gewinner"
        if w.get("checked") and time.time() - w["checked"] < 3 * 86400:
            continue
        try:
            wc.evaluate(rpc, w, data)
        except SystemExit:
            raise
        except Exception as e:
            w.update(status="Keine Daten", why=f"Fehler: {str(e)[:100]}", checked=time.time())
        scored += 1
        v["status"], v["why"] = w.get("status"), w.get("why")
        log(f"  {a[:4]}…{a[-4:]} ({len(v['coins'])} Gewinner früh): {w.get('status')} – {w.get('why', '')}")
    data["gtfa"] = rpc.gtfa
    wc.save(data)
    wc.write_page(data)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"t": time.time(), "winners": report,
                   "wallets": {a: v for a, v in cands[:500]}, "credits_used": rpc.used}, f, indent=1)
    pool = [a for a, v in per_wallet.items() if data["wallets"].get(a, {}).get("status") == "Pool"]
    log(f"Fertig: {scored} bewertet, {len(pool)} davon im Pool, {rpc.used:,} Credits verbraucht.")


if __name__ == "__main__":
    main()
