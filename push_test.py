"""Schickt die Erklärung der Nachrichten und eine Test-Nachricht aufs Handy (für den Knopf 'Push-Test')."""
import sys
import time

import notify

LEGENDE = """Ab jetzt meldet sich der Fomo-Bot hier. Alles ist Papier-Handel: kein echtes Geld, nur ein Test der Regeln.

👀 SIGNAL – eine unserer Smart-Wallets hat gerade einen kleinen Coin gekauft. Nur ein Hinweis, noch kein Kauf.
🟢 KAUF – ein Bot hat auf dem Papier gekauft. Drin steht: welcher Bot, warum und wann er wieder verkauft.
💰 TEILVERKAUF – ein Bot hat einen Teil mit Gewinn verkauft, der Rest läuft weiter.
✅ / 🔴 VERKAUF – ein Bot hat verkauft, mit Ergebnis in % (grün = Gewinn, rot = Verlust).

Die Bots: Alle Coins, Nur ab 14 Tagen, Tempo (schnell, kleine Ziele), Trailing (Gewinner laufen lassen), Qualität (große Coins), Smart-Money (folgt den Wallets), Muster (Gewinner-Muster aus dem Rückblick).

Tippen auf eine Nachricht öffnet den Chart bei Dexscreener. Wichtige Nachrichten (Smart-Money, Muster) kommen mit höherer Priorität."""

if not notify.TOPIC:
    print("Secret NTFY_TOPIC fehlt")
    sys.exit(1)
ok1 = notify.push("📖 So liest du die Fomo-Bot-Nachrichten", LEGENDE, priority=3, tags=["book"])
time.sleep(2)
ok2 = notify.push("🔔 Test: Kommt das als Mitteilung an?",
                  "Wenn dein iPhone hierbei vibriert bzw. eine Mitteilung zeigt, ist alles richtig eingestellt. "
                  "Tippen öffnet Dexscreener (Beispiel: CLAUDIA).",
                  click=notify.dex("2j5SaS7xy776qCBpyPQbZjyQSAtKiFgrwjfErthnW2ZM"), priority=5, tags=["white_check_mark"])
print("gesendet" if ok1 and ok2 else "fehlgeschlagen")
sys.exit(0 if ok1 and ok2 else 1)
