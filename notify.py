"""
Push-Nachrichten aufs Handy über ntfy.sh (kostenlos, ohne Konto).

Einrichtung: App "ntfy" installieren, das Thema (Topic) abonnieren und dasselbe Thema im GitHub-Repo
als Secret NTFY_TOPIC eintragen. Ohne Secret wird nichts verschickt.
"""

import json
import os
import urllib.request

TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def push(title, text, click=None, priority=3, tags=None):
    """priority: 1-5 (2 = leise, 3 = normal, 4 = wichtig). Fehler werden ignoriert."""
    if not TOPIC:
        return False
    body = {"topic": TOPIC, "title": title, "message": text, "priority": priority}
    if click:
        body["click"] = click
    if tags:
        body["tags"] = list(tags)
    try:
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        print(f"[Push] fehlgeschlagen: {e}", flush=True)
        return False


def dex(ca):
    return f"https://dexscreener.com/solana/{ca}"
