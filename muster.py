"""
Muster aus dem Rückblick (Reverse Engineering der Gewinner, z. B. CLAUDIA, HOOKED, COCKROACH):
erster Pump, Absturz um 60-92 % vom Hoch, Coin 1-14 Tage alt, MC 50k-1,5 Mio., noch Handel
(Tagesvolumen mindestens 50 % der MC), in der letzten Stunde nicht schon am Steigen (prüft der Bot beim Kauf).
Rückblick 12.10.: 128 Trades in 39 Coins, Ø +19 % pro Trade (Stop 30 / Ziel +100) gegenüber +2 % bei
beliebigem Einstieg – aber in Daten mit Überlebens-Verzerrung, daher jetzt der Live-Test.

Prüft nach jedem Trichter-Lauf die sicheren Coins (Contract-, Holder- und Cluster-Check bestanden)
mit stündlichen Kerzen von GeckoTerminal. Nur Daten lesen.
"""

import time

import memecoin_funnel as funnel
import rueckblick_analyse as ra

MC_MIN, MC_MAX = 50_000, 1_500_000
AGE_MIN_H, AGE_MAX_H = 24, 336
DD_MIN, DD_MAX = 0.08, 0.40       # Kurs bei 8-40 % des bisherigen Hochs (= 60-92 % gefallen)
MIN_VOL_MC = 0.5                  # Tagesvolumen mindestens 50 % der MC
MAX_CHECKS = 15                   # so viele Coins pro Trichter-Lauf prüfen (GeckoTerminal-Limit)
OK_STAGES = (None, "Handel", "Timing")   # durchgefallen nur wegen Handel/Timing = trotzdem sicher
GT = "https://api.geckoterminal.com/api/v2/networks/solana"


def rule(ft):
    return (MC_MIN <= ft["mc"] <= MC_MAX and AGE_MIN_H <= ft["age_h"] <= AGE_MAX_H
            and DD_MIN <= ft["dd_ath"] <= DD_MAX and ft["vol24_mc"] >= MIN_VOL_MC)


def candidates(allc, log=print):
    safe = [c for c in allc if c.get("out_at") in OK_STAGES and MC_MIN <= c["mc"] <= MC_MAX
            and AGE_MIN_H <= c["age_h"] <= AGE_MAX_H and c.get("main_pair")]
    safe.sort(key=lambda c: -c.get("vol24", 0))
    out = []
    for c in safe[:MAX_CHECKS]:
        try:
            res = funnel.get(f"{GT}/pools/{c['main_pair']}/ohlcv/hour?aggregate=1&limit=400&currency=usd&token={c['addr']}")
            rows = sorted(((res.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [], key=lambda r: r[0])
        except Exception as e:
            log(f"Muster ${c['sym']}: {e}")
            continue
        finally:
            time.sleep(2.2)
        if len(rows) < 30 or not rows[-1][4]:
            continue
        coin = {"supply": c["mc"] / float(rows[-1][4]), "created_min": None,
                "ohlcv": [[int(r[0])] + [float(x) for x in r[1:6]] for r in rows]}
        s = ra.series(coin)
        f = ra.features(coin, s)
        ft = f[-1] if f else None
        if not ft:
            continue
        ft["age_h"] = c["age_h"]          # Alter aus DexScreener (Kerzen reichen evtl. nicht bis zum Start)
        ft["mc"] = c["mc"]
        ft["vol24_mc"] = (c.get("vol24") or 0) / c["mc"] if c["mc"] else 0
        if rule(ft):
            out.append({"addr": c["addr"], "sym": c["sym"], "age_d": c["age_h"] / 24, "mc": c["mc"],
                        "liq": c.get("liq") or 0, "top10": c.get("top10"), "cluster": c.get("cluster"),
                        "muster": {"dd_ath": round(ft["dd_ath"], 2), "h_since_low": ft["h_since_low"],
                                   "ath_mc": round(ft["ath_mc"])}})
    log(f"Muster: {len(safe)} sichere Coins im Bereich, {min(len(safe), MAX_CHECKS)} geprüft, {len(out)} passen")
    return out
