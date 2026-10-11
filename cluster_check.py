"""
Cluster-Check – eine kleine eigene Bubblemap
Prüft für einen Coin, ob seine größten Holder zusammenhängen:
  - Wurden mehrere Top-Holder von derselben Wallet mit SOL versorgt?  -> ein Cluster
  - Hat ein Top-Holder einen anderen Top-Holder versorgt?              -> ein Cluster
Börsen und Dienste (Wallets mit riesiger Aktivität, die viele fremde Wallets versorgen)
zählen nicht als Verbindung, sonst wäre jeder Binance-Kunde mit jedem verbunden.

Ergebnis: Anteil am Supply, den verbundene Top-Holder zusammen halten.
Braucht HELIUS_API_KEY. Ohne Key wird der Check übersprungen (nur Hinweis, kein Ausschluss).
Gecacht in cluster_cache.json: Die Geldquelle einer Wallet ändert sich nie, also wird jede Wallet nur einmal gefragt.
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "cluster_cache.json")

# ======================= REGELN (hier anpassen) =======================
TOP_N = 15                    # so viele Top-Holder (ohne Pools/Börsen) prüfen
NEW_WALLETS_PER_SCAN = 45     # höchstens so viele neue Wallets pro Trichter-Lauf nachschlagen (Zeitlimit)
CREDITS_PER_DAY = 4000        # Helius-Budget pro Tag für diesen Check (~120.000 im Monat)
RECHECK_H = 6                 # Coin frühestens nach 6 h neu bewerten (Holder ändern sich)
SERVICE_TX = 1000             # Geldquelle mit 1000 Transaktionen in ...
SERVICE_SPAN_H = 48           # ... weniger als 48 h = Börse/Dienst, keine echte Verbindung
SERVICE_SPREAD = 4            # Geldquelle, die Holder in 4+ verschiedenen Coins versorgt = Dienst
# =====================================================================


def _load():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"funders": {}, "services": {}, "coins": {}, "credits": {}, "gtfa": False}


class Checker:
    def __init__(self):
        self.cache = _load()
        for k in ("funders", "services", "coins", "credits"):
            self.cache.setdefault(k, {})
        self.new = 0
        self.today = time.strftime("%Y-%m-%d")
        key = os.environ.get("HELIUS_API_KEY", "").strip()
        self.on = bool(key)
        self.rpc = None
        if self.on:
            import wallet_collector as wc
            self.wc = wc
            cache = self.cache

            class _Rpc(wc.Rpc):
                def __init__(self, k):        # ohne SOL-Kurs-Abfrage, Credits pro TAG zählen
                    self.url = f"https://mainnet.helius-rpc.com/?api-key={k}"
                    self.data = cache
                    self.month = time.strftime("%Y-%m-%d")
                    self.used = 0
                    self.gtfa = cache.get("gtfa", False)
                    self.sol_usd = 150.0

            self.rpc = _Rpc(key)

    # ------------------------------------------------------------ Budget
    def credits_today(self):
        return self.cache["credits"].get(self.today, 0)

    def budget_ok(self):
        return self.credits_today() < CREDITS_PER_DAY

    def can_lookup(self):
        return self.on and self.budget_ok() and self.new < NEW_WALLETS_PER_SCAN

    # ------------------------------------------------------------ Bausteine
    def funder(self, owner):
        """Geldquelle einer Wallet. '' = unbekannt, None = noch nicht nachgeschlagen (Limit erreicht)."""
        f = self.cache["funders"].get(owner)
        if f is not None:
            return f
        if not self.can_lookup():
            return None
        self.new += 1
        try:
            f = self.wc.funder_of(self.rpc.first_transactions(owner), owner) or ""
        except Exception:
            return None
        self.cache["funders"][owner] = f
        return f

    def is_service(self, funder):
        s = self.cache["services"].get(funder)
        if s is None:
            s = False
            if self.on and self.budget_ok():
                try:
                    sigs = self.rpc.signatures(funder, limit=SERVICE_TX)
                    ts = [x.get("blockTime") or 0 for x in sigs if x.get("blockTime")]
                    s = len(sigs) >= SERVICE_TX and (max(ts) - min(ts)) < SERVICE_SPAN_H * 3600
                except Exception:
                    s = False
                self.cache["services"][funder] = s
        if s:
            return True
        # versorgt Holder in vielen verschiedenen Coins? -> Dienst
        coins = sum(1 for c in self.cache["coins"].values() if funder in (c.get("funders_seen") or []))
        return coins >= SERVICE_SPREAD

    # ------------------------------------------------------------ Hauptprüfung
    def check(self, coin, holders):
        """holders: Liste (owner, pct) der größten Holder ohne Pools/Börsen.
        Rückgabe: dict(status=ok|pending|off, cluster_pct, groups, largest_pct)"""
        if not self.on:
            return {"status": "off", "why": "kein Helius-Key"}
        holders = holders[:TOP_N]
        old = self.cache["coins"].get(coin)
        if old and time.time() - old["t"] < RECHECK_H * 3600:
            return old
        if not self.budget_ok():
            return old or {"status": "off", "why": "Tagesbudget aufgebraucht"}

        funders, missing = {}, 0
        for owner, _ in holders:
            f = self.funder(owner)
            if f is None:
                missing += 1
            else:
                funders[owner] = f
        if missing:
            if not self.budget_ok():
                return old or {"status": "off", "why": "Tagesbudget aufgebraucht"}
            return {"status": "pending", "why": f"{missing} Holder noch offen"}

        # Verbindungen: Holder <-> Geldquelle (außer Börsen/Dienste). Geldquelle kann selbst Top-Holder sein.
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        holder_set = {o for o, _ in holders}
        for owner, f in funders.items():
            if f and (f in holder_set or not self.is_service(f)):
                parent[find(owner)] = find(f)

        groups = {}
        for owner, pct in holders:
            groups.setdefault(find(owner), []).append((owner, pct))
        clusters = [g for g in groups.values() if len(g) >= 2]
        sums = sorted((round(sum(p for _, p in g), 2) for g in clusters), reverse=True)
        res = {"status": "ok", "t": time.time(),
               "cluster_pct": round(sum(sums), 2),
               "largest_pct": sums[0] if sums else 0.0,
               "groups": [[o for o, _ in g] for g in clusters],
               "funders_seen": sorted({f for f in funders.values() if f})}
        self.cache["coins"][coin] = res
        return res

    def save(self):
        if not self.on:
            return
        self.cache["gtfa"] = self.rpc.gtfa
        # aufräumen: Coins älter als 7 Tage, Credit-Zähler älter als 40 Tage
        cut = time.time() - 7 * 86400
        self.cache["coins"] = {k: v for k, v in self.cache["coins"].items() if v.get("t", 0) >= cut}
        days = sorted(self.cache["credits"])[-40:]
        self.cache["credits"] = {d: self.cache["credits"][d] for d in days}
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f)
        os.replace(tmp, CACHE_FILE)
