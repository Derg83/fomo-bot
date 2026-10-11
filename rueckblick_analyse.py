"""
Rückblick, Schritt 2: Was passierte VOR den großen Läufen – und wie oft sah es genauso aus, ohne dass etwas passierte?

Eingabe: rueckblick/coins.json.gz (aus rueckblick_sammeln.py)
  1. Gewinner: Coins, die vom Tief aus in höchstens 72 h mindestens 3x gemacht haben, mit Laufbeginn
  2. Merkmale zu jeder Stunde jedes Coins (nur mit Daten bis zu dieser Stunde, kein Blick in die Zukunft)
  3. Ergebnis jeder Stunde: Was wäre ein Kauf in der nächsten Stunde geworden (48 h, Stop/Ziel, 1 % Gebühr je Seite)?
  4. Trefferquoten je Merkmal und Regel-Suche mit Gegenprobe auf einem späteren Zeitraum

Start: python rueckblick_analyse.py [pfad/zu/coins.json.gz]
"""

import gzip
import itertools
import json
import math
import os
import statistics
import sys

FEE = 0.01
HOLD_H = 48


def load(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)["coins"]


def series(c):
    """Stündliche Reihe ohne Lücken (fehlende Stunden = kein Handel: Kurs bleibt, Volumen 0)."""
    rows = c["ohlcv"]
    out = []
    for r in rows:
        if out:
            last = out[-1]
            gap = (r[0] - last[0]) // 3600
            for k in range(1, min(gap, 72)):
                out.append([last[0] + k * 3600, last[4], last[4], last[4], last[4], 0.0])
            if gap > 72:      # sehr lange tot -> Reihe neu anfangen wäre sauberer; wir hängen trotzdem an
                pass
        out.append(r)
    return out


def features(c, s):
    """Merkmale je Stunde, nur aus Vergangenheit. Liefert Liste von dicts (Index = Stunde)."""
    sup = c["supply"]
    first_t = s[0][0]
    created = c.get("created_min") or first_t
    if created > first_t or created < first_t - 400 * 86400:
        created = first_t
    feats = []
    ath = 0.0
    ath_i = 0
    low = math.inf
    low_i = 0
    first_peak = None
    for i, (t, o, h, l, cl, v) in enumerate(s):
        if h > ath:
            ath, ath_i = h, i
        if l < low:
            low, low_i = l, i
        if i < 24:
            feats.append(None)
            continue
        mc = cl * sup
        v24 = sum(x[5] for x in s[i - 23:i + 1])
        v6 = sum(x[5] for x in s[i - 5:i + 1])
        v_prev18 = sum(x[5] for x in s[i - 23:i - 5])
        feats.append({
            "t": t, "mc": mc, "age_h": (t - created) / 3600,
            "r1": cl / s[i - 1][4] - 1 if s[i - 1][4] else 0,
            "r6": cl / s[i - 6][4] - 1 if s[i - 6][4] else 0,
            "r24": cl / s[i - 24][4] - 1 if s[i - 24][4] else 0,
            "dd_ath": cl / ath if ath else 1,            # 1 = am Hoch, 0.2 = -80 % vom Hoch
            "h_since_ath": i - ath_i,
            "h_since_low": i - low_i,                    # wie lange hält der Boden
            "up_from_low": cl / low if low else 1,
            "vol24_mc": v24 / mc if mc else 0,
            "vol_accel": (v6 / 6) / (v_prev18 / 18) if v_prev18 > 0 else (9.9 if v6 > 0 else 0),
            "ath_mc": ath * sup,
        })
    return feats


def outcome(s, i, stop, tp, trail=None):
    """Kauf zum Eröffnungskurs der nächsten Stunde. Stop zuerst, wenn beides in einer Kerze passiert.
    trail=(ab, abstand): nach +ab raus, sobald abstand unter dem Hoch."""
    if i + 1 >= len(s):
        return None
    entry = s[i + 1][1]
    if entry <= 0:
        return None
    peak = entry
    end = min(len(s), i + 1 + HOLD_H)
    for j in range(i + 1, end):
        h, l, cl = s[j][2], s[j][3], s[j][4]
        if l <= entry * (1 - stop):
            return (1 - stop) * (1 - FEE) / (1 + FEE) - 1
        if trail:
            peak = max(peak, h)
            if peak >= entry * (1 + trail[0]) and l <= peak * (1 - trail[1]):
                return peak * (1 - trail[1]) / entry * (1 - FEE) / (1 + FEE) - 1
        elif h >= entry * (1 + tp):
            return (1 + tp) * (1 - FEE) / (1 + FEE) - 1
    if end - (i + 1) < HOLD_H and end == len(s):
        return None      # noch nicht genug Zukunft vorhanden
    return s[end - 1][4] / entry * (1 - FEE) / (1 + FEE) - 1


def max_mult(s, i, hours=HOLD_H):
    if i + 1 >= len(s) or s[i + 1][1] <= 0:
        return None
    entry = s[i + 1][1]
    return max(x[2] for x in s[i + 1:i + 1 + hours]) / entry


def winners(coins, data):
    """Läufe: vom Tief mindestens 3x innerhalb von 72 h. Pro Coin der größte Lauf."""
    out = []
    for m, (c, s, f) in data.items():
        best = None
        for i in range(24, len(s) - 1):
            lo = s[i][4]
            if lo <= 0 or lo * c["supply"] < 20_000 or s[i][5] <= 0:
                continue
            hi_j = max(range(i, min(len(s), i + 72)), key=lambda j: s[j][4])
            mult = s[hi_j][4] / lo
            if mult >= 3 and (best is None or mult > best[0]):
                best = (mult, i, hi_j)
        if best:
            mult, i, j = best
            ft = f[i] or {}
            out.append({"addr": m, "sym": c["sym"], "mult": round(mult, 1), "start_t": s[i][0],
                        "start_mc": s[i][4] * c["supply"], "peak_mc": s[j][4] * c["supply"],
                        "hours": j - i, "now_mc": s[-1][4] * c["supply"], "feat": ft})
    return sorted(out, key=lambda w: -w["mult"])


def bucket_table(rows, key, edges, label):
    print(f"\n{label}")
    print(f"  {'Bereich':<18}{'Fälle':>7}{'Coins':>7}{'2x in 48h':>11}{'Ø Ergebnis*':>13}")
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [r for r in rows if lo <= r[key] < hi]
        if len(sel) < 20:
            continue
        p2 = sum(r["mm"] >= 2 for r in sel) / len(sel)
        ev = statistics.mean(r["ev"] for r in sel)
        print(f"  {fmt_edge(lo)}–{fmt_edge(hi):<10}{len(sel):>7}{len({r['m'] for r in sel}):>7}{p2 * 100:>10.1f}%{ev * 100:>12.1f}%")


def fmt_edge(x):
    if x == math.inf:
        return "∞"
    if x == -math.inf:
        return "-∞"
    if abs(x) >= 1e6:
        return f"{x / 1e6:g}M"
    if abs(x) >= 1e3:
        return f"{x / 1e3:g}k"
    return f"{x:g}"


def simulate(data, rule, stop, tp, trail=None, t_from=0, t_to=9e12, cooldown_h=24):
    """Bot-artig: pro Coin der Reihe nach, nach einem Trade Pause. Liefert Liste (m, t, ergebnis)."""
    trades = []
    for m, (c, s, f) in data.items():
        nxt = 0
        for i, ft in enumerate(f):
            if ft is None or i < nxt or not (t_from <= ft["t"] < t_to):
                continue
            if rule(ft):
                r = outcome(s, i, stop, tp, trail)
                if r is None:
                    continue
                trades.append((m, ft["t"], r))
                nxt = i + cooldown_h
    return trades


def summary(trades):
    if not trades:
        return None
    rs = [r for _, _, r in trades]
    return {"n": len(rs), "coins": len({m for m, _, _ in trades}), "ev": statistics.mean(rs),
            "win": sum(r > 0 for r in rs) / len(rs), "med": statistics.median(rs),
            "ev_ohne_top3": statistics.mean(sorted(rs)[:-3]) if len(rs) > 6 else None}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else os.path.join(os.path.dirname(__file__), "rueckblick", "coins.json.gz")
    coins = load(path)
    seen_path = os.path.join(os.path.dirname(path), "gesehen.json")
    seen = {}
    if os.path.exists(seen_path):
        with open(seen_path, encoding="utf-8") as f:
            seen = json.load(f)
    only_seen = "--alle" not in sys.argv
    data = {}
    for m, c in coins.items():
        s = series(c)
        if len(s) < 48:
            continue
        data[m] = (c, s, features(c, s))
    ts = sorted(ft["t"] for _, _, f in data.values() for ft in f if ft)
    t_split = ts[int(len(ts) * 0.6)]
    print(f"{len(data)} Coins, {len(ts)} Coin-Stunden, Zeitraum bis Split: 60 % / 40 %")

    W = winners(coins, data)
    n_all = len(data)
    # Ab hier nur noch "gesehene" Coins ab dem Zeitpunkt, an dem unsere Tools sie gesehen haben:
    # sonst wären nur Coins drin, die HEUTE noch Volumen haben (= Überlebende), und alles sähe zu gut aus.
    if only_seen and seen:
        for m in list(data):
            if m not in seen:
                del data[m]
                continue
            c, s_, f = data[m]
            t0 = seen[m]["t"]
            data[m] = (c, s_, [ft if (ft and ft["t"] >= t0) else None for ft in f])
        ts = sorted(ft["t"] for _, _, f in data.values() for ft in f if ft)
        t_split = ts[int(len(ts) * 0.6)] if ts else 0
        print(f"\nKohorte ohne Überlebens-Verzerrung: {len(data)} Coins, {len(ts)} Coin-Stunden ab dem ersten Sehen")
    print(f"\n=== Gewinner (3x+ vom Tief in ≤72 h, alle Daten): {len(W)} von {n_all} Coins ===")
    for w in W[:40]:
        ft = w["feat"]
        print(f"  ${w['sym']:<10} {w['mult']:>5}x  Start-MC {w['start_mc'] / 1e3:>7.0f}k -> {w['peak_mc'] / 1e3:>8.0f}k in {w['hours']:>2} h | "
              f"Alter {ft.get('age_h', 0) / 24:>5.1f} T, vom Hoch {ft.get('dd_ath', 0) * 100:>4.0f} %, Boden seit {ft.get('h_since_low', 0):>3} h, "
              f"Vol-Beschl. {ft.get('vol_accel', 0):>4.1f} | {w['addr']}")

    prof = [w["feat"] for w in W if w["feat"]]
    if prof:
        print("\n=== Profil beim Laufbeginn (Median der Gewinner) ===")
        for k, lab in (("mc", "MC"), ("age_h", "Alter (h)"), ("dd_ath", "Kurs/Hoch"), ("h_since_low", "Std. seit Tief"),
                       ("r24", "24 h"), ("r1", "1 h"), ("vol24_mc", "Vol24/MC"), ("vol_accel", "Vol-Beschl.")):
            vals = sorted(p[k] for p in prof)
            q = lambda x: vals[int(x * (len(vals) - 1))]
            print(f"  {lab:<15} 25 %: {q(.25):>10.2f}   Median: {q(.5):>10.2f}   75 %: {q(.75):>10.2f}")

    # Grundgesamtheit: alle Coin-Stunden mit MC 30k-30M, Ergebnis für Stop 30 / Ziel +100 %
    rows = []
    for m, (c, s, f) in data.items():
        for i, ft in enumerate(f):
            if ft is None or not (30_000 <= ft["mc"] <= 30_000_000):
                continue
            mm = max_mult(s, i)
            ev = outcome(s, i, 0.30, 1.0)
            if mm is None or ev is None:
                continue
            rows.append({**ft, "m": m, "mm": mm, "ev": ev})
    base2 = sum(r["mm"] >= 2 for r in rows) / len(rows)
    print(f"\n=== Grundwahrscheinlichkeit: {len(rows)} Coin-Stunden, 2x in 48 h: {base2 * 100:.1f} %, "
          f"Ø Ergebnis (Stop 30 / Ziel +100): {statistics.mean(r['ev'] for r in rows) * 100:.1f} % ===")
    inf = math.inf
    bucket_table(rows, "mc", [30e3, 100e3, 200e3, 400e3, 700e3, 1e6, 2e6, 5e6, 30e6], "Market Cap")
    bucket_table(rows, "age_h", [0, 6, 24, 48, 96, 168, 336, 720, inf], "Alter (Stunden)")
    bucket_table(rows, "dd_ath", [0, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.01], "Kurs im Verhältnis zum bisherigen Hoch")
    bucket_table(rows, "h_since_low", [0, 1, 3, 6, 12, 24, 48, 96, inf], "Stunden seit dem Tief (Boden hält)")
    bucket_table(rows, "r24", [-1, -0.5, -0.3, -0.1, 0.1, 0.3, 1, 3, inf], "Veränderung 24 h")
    bucket_table(rows, "r1", [-1, -0.2, -0.1, -0.03, 0.03, 0.1, 0.2, 0.5, inf], "Veränderung 1 h")
    bucket_table(rows, "vol24_mc", [0, 0.1, 0.3, 0.6, 1, 2, 5, inf], "Volumen 24 h im Verhältnis zur MC")
    bucket_table(rows, "vol_accel", [0, 0.5, 1, 1.5, 2, 3, 5, inf], "Volumen letzte 6 h vs. 18 h davor")
    bucket_table(rows, "up_from_low", [1, 1.2, 1.5, 2, 3, 5, inf], "Kurs im Verhältnis zum Tief")

    # Regel-Suche: Kombinationen aus wenigen Schwellen, verschiedene Ausstiege, Gegenprobe im späteren Zeitraum
    import numpy as np
    exits = [("Stop 30 / Ziel +100", 0.30, 1.0, None), ("Stop 30 / Ziel +50", 0.30, 0.5, None),
             ("Stop 20 / Ziel +40", 0.20, 0.4, None), ("Stop 40 / Ziel +200", 0.40, 2.0, None),
             ("Stop 30 / Trailing ab +50, 25 % unter Hoch", 0.30, 0, (0.5, 0.25))]
    cols = {k: [] for k in ("cid", "i", "t", "mc", "age_h", "dd_ath", "h_since_low", "vol_accel", "r1", "r24", "vol24_mc")}
    outs = [[] for _ in exits]
    for cid, (m, (c, s_, f)) in enumerate(data.items()):
        for i, ft in enumerate(f):
            if ft is None or not (20_000 <= ft["mc"] <= 50_000_000):
                continue
            res = [outcome(s_, i, st, tp, tr) for _, st, tp, tr in exits]
            if res[0] is None:
                continue
            cols["cid"].append(cid)
            cols["i"].append(i)
            for k in list(cols)[2:]:
                cols[k].append(ft[k])
            for k, r in enumerate(res):
                outs[k].append(r if r is not None else 0.0)
    A = {k: np.array(v) for k, v in cols.items()}
    O = [np.array(o) for o in outs]
    mids = list(data)
    print(f"\nRegel-Suche über {len(A['t'])} Coin-Stunden ...")

    def run(mask, k, period):
        sel = np.nonzero(mask & period)[0]
        trades, last = [], {}
        for j in sel:       # Daten sind je Coin zeitlich sortiert: nach einem Trade 24 h Pause
            cid = A["cid"][j]
            if A["i"][j] < last.get(cid, -1):
                continue
            last[cid] = A["i"][j] + 24
            trades.append(j)
        if not trades:
            return None
        rs = O[k][trades]
        coins_ = len({A["cid"][j] for j in trades})
        srt = np.sort(rs)
        return {"n": len(rs), "coins": coins_, "ev": float(rs.mean()), "win": float((rs > 0).mean()),
                "med": float(np.median(rs)), "ev_ohne_top3": float(srt[:-3].mean()) if len(rs) > 6 else None,
                "examples": [mids[A["cid"][j]] for j in trades[:5]]}

    train, test = A["t"] < t_split, A["t"] >= t_split
    grid = {
        "mc": [(30e3, 300e3), (100e3, 600e3), (200e3, 1e6), (300e3, 2e6), (1e6, 10e6), (20e3, 50e6)],
        "age_h": [(0, 48), (24, 168), (48, 336), (0, 1e9)],
        "dd_ath": [(0, 0.3), (0.1, 0.5), (0.3, 1.01), (0, 1.01)],
        "h_since_low": [(0, 1e9), (12, 1e9), (24, 1e9)],
        "vol_accel": [(0, 1e9), (1.5, 1e9), (2.5, 1e9)],
        "r1": [(-1, 1e9), (-0.05, 0.15), (0, 0.5)],
    }
    keys = list(grid)
    masks = {k: [((A[k] >= lo) & (A[k] < hi)) for lo, hi in grid[k]] for k in keys}
    results = []
    for combo in itertools.product(*[range(len(grid[k])) for k in keys]):
        mask = np.ones(len(A["t"]), bool)
        for k, ci in zip(keys, combo):
            mask &= masks[k][ci]
        if mask.sum() < 30:
            continue
        for k, (name, *_x) in enumerate(exits):
            tr = run(mask, k, train)
            if not tr or tr["n"] < 30 or tr["coins"] < 12 or tr["ev"] <= 0:
                continue
            te = run(mask, k, test)
            results.append({"rule": {kk: grid[kk][ci] for kk, ci in zip(keys, combo)},
                            "exit": name, "train": tr, "test": te})
    results.sort(key=lambda r: (r["test"] or {}).get("ev", -9) if (r["test"] or {}).get("n", 0) >= 15 else -9, reverse=True)
    print(f"\n=== Regel-Suche: {len(results)} Regeln im ersten Zeitraum positiv; beste nach Gegenprobe ===")
    for r in results[:25]:
        ru, tr, te = r["rule"], r["train"], r["test"] or {}
        print(f"  MC {fmt_edge(ru['mc'][0])}-{fmt_edge(ru['mc'][1])} | Alter {ru['age_h']} h | vom Hoch {ru['dd_ath']} | "
              f"Boden ≥{ru['h_since_low'][0]} h | Vol-Beschl. ≥{ru['vol_accel'][0]} | 1h {ru['r1']} | {r['exit']}")
        print(f"      Lernzeitraum: {tr['n']} Trades/{tr['coins']} Coins, Ø {tr['ev'] * 100:+.1f} %, Treffer {tr['win'] * 100:.0f} %"
              f" | Gegenprobe: {te.get('n', 0)} Trades/{te.get('coins', 0)} Coins, Ø {te.get('ev', 0) * 100:+.1f} %, Treffer {te.get('win', 0) * 100:.0f} %"
              f", ohne Top-3 {((te.get('ev_ohne_top3') or 0) * 100):+.1f} %")
    out = os.path.join(os.path.dirname(path), "analyse.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"winners": W, "rules": results[:200], "base2": base2, "n_rows": len(rows)}, f, indent=1, default=str)
    print(f"\nGespeichert: {out}")


if __name__ == "__main__":
    main()
