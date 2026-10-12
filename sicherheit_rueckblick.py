"""
Sicherheits-Rückblick: Wie viele Coins wären an unserem Sicherheitscheck gescheitert –
und haben trotzdem lange über 1 Mio. Market Cap gelegen?

Eingabe:  rueckblick/coins.json.gz (Kursverläufe, Branch "rueckblick-daten")
Je Coin:  RugCheck heute -> Contract- und Holder-Stufe des Trichters (wie im Bot)
          Kursverlauf    -> 1 Mio. MC erreicht? 48 h am Stück über 1 Mio. gehalten? größter 3x-Lauf?
Ergebnis: sicherheit_rueckblick.json

Achtung: RugCheck-Daten sind von HEUTE, nicht vom Zeitpunkt des Laufs (Insider können inzwischen verkauft haben).
"""

import gzip
import json
import os
import sys
import time

import memecoin_funnel as funnel

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sicherheit_rueckblick.json")
LEVEL = 1_000_000
HOLD_H = 48


def outcome(c):
    sup = c["supply"]
    rows = sorted(c["ohlcv"], key=lambda r: r[0])
    mcs = [r[4] * sup for r in rows]
    run, best_run = 0, 0
    for i, m in enumerate(mcs):
        # Lücken im Stundenverlauf (kein Handel) zählen als "Kurs unverändert"
        if i and rows[i][0] - rows[i - 1][0] > 3600:
            gap = (rows[i][0] - rows[i - 1][0]) // 3600 - 1
            run = run + gap if mcs[i - 1] >= LEVEL else 0
        run = run + 1 if m >= LEVEL else 0
        best_run = max(best_run, run)
    big = 0.0
    for i in range(len(rows)):
        lo = rows[i][4]
        if lo <= 0 or lo * sup < 20_000:
            continue
        hi = max(r[4] for r in rows[i:i + 72])
        big = max(big, hi / lo)
    return {"max_mc": round(max(mcs)) if mcs else 0, "now_mc": round(mcs[-1]) if mcs else 0,
            "reached_1m": max(mcs, default=0) >= LEVEL, "held_1m_48h": best_run >= HOLD_H,
            "best_hours_1m": best_run, "best_3d_mult": round(big, 1)}


def check(addr, c):
    rc = funnel.rugcheck_live(addr)
    if not rc or not rc.get("token"):
        return "keine Daten", ["RugCheck ohne Daten"], {}
    cc = {"addr": addr, "main_pair": c.get("pair"), "pairs": {c.get("pair")}, "warns": [], "mc": c.get("mc") or 0}
    why = funnel.stage_contract(cc, rc)
    if why:
        return "Contract", why, cc
    why = funnel.stage_holder(cc, rc)
    if why:
        return "Holder", why, cc
    return "bestanden", [], cc


def category(why):
    w = " ".join(why).lower()
    for key, lab in (("mint", "Mint aktiv"), ("freeze", "Freeze aktiv"), ("lp", "LP nicht gelockt"),
                     ("insider", "Insider-Anteil"), ("top-10", "Top-10-Anteil"), ("dev hält", "Dev-Anteil"),
                     ("bundle", "Bundle-Muster"), ("rugcheck", "RugCheck-Warnung"), ("gerugged", "als gerugged markiert")):
        if key in w:
            return lab
    return "sonstiges"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "rueckblick", "coins.json.gz")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        coins = json.load(f)["coins"]
    res = []
    for i, (addr, c) in enumerate(coins.items()):
        if not c.get("ohlcv"):
            continue
        stage, why, cc = check(addr, c)
        o = outcome(c)
        res.append({"addr": addr, "sym": c.get("sym"), "stage": stage, "why": why, "cat": category(why) if why else None,
                    "top10": cc.get("top10"), "insider": cc.get("insider"), **o})
        if i % 25 == 0:
            print(f"{i}/{len(coins)} ...", flush=True)
    summary = {}
    for grp, sel in (("bestanden", [r for r in res if r["stage"] == "bestanden"]),
                     ("durchgefallen", [r for r in res if r["stage"] in ("Contract", "Holder")])):
        summary[grp] = {"n": len(sel), "reached_1m": sum(r["reached_1m"] for r in sel),
                        "held_1m_48h": sum(r["held_1m_48h"] for r in sel),
                        "lauf_3x": sum(r["best_3d_mult"] >= 3 for r in sel)}
    cats = {}
    for r in res:
        if r["stage"] in ("Contract", "Holder"):
            k = cats.setdefault(r["cat"], {"n": 0, "held_1m_48h": 0, "reached_1m": 0, "lauf_3x": 0})
            k["n"] += 1
            k["held_1m_48h"] += r["held_1m_48h"]
            k["reached_1m"] += r["reached_1m"]
            k["lauf_3x"] += r["best_3d_mult"] >= 3
    out = {"t": time.time(), "coins": len(res), "summary": summary, "kategorien": cats,
           "ohne_daten": sum(r["stage"] == "keine Daten" for r in res), "rows": res}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
