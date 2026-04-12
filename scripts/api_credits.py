"""
scripts/api_credits.py
========================
Monitor de creditos de the-odds-api.

Uso:
  python scripts/api_credits.py          → muestra resumen del log
  python scripts/api_credits.py --check  → consulta saldo actual (1 llamada API)
"""

import sys
import os
import json
import requests
from pathlib import Path
from datetime import datetime, timezone

sys.path.append(str(Path(__file__).parent.parent))

from config.settings import ODDS_API_KEY, API_CREDITS_ALERT_THRESHOLD

CACHE_FILE  = Path(__file__).parent.parent / "data" / "api_cache.json"
CREDITS_LOG = Path(__file__).parent.parent / "logs" / "api_credits.log"


def check_live_credits() -> dict:
    """Consulta el saldo actual de creditos con 1 llamada liviana."""
    url = "https://api.the-odds-api.com/v4/sports"
    r = requests.get(url, params={"apiKey": ODDS_API_KEY}, timeout=10)

    used      = r.headers.get("x-requests-used", "?")
    remaining = r.headers.get("x-requests-remaining", "?")

    return {
        "status": r.status_code,
        "used": used,
        "remaining": remaining,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def show_log_summary():
    """Muestra las ultimas entradas del log de creditos."""
    if not CREDITS_LOG.exists():
        print("Sin historial de creditos todavia.")
        return

    lines = CREDITS_LOG.read_text().strip().splitlines()
    last = lines[-20:] if len(lines) > 20 else lines

    print("\n📊 HISTORIAL DE CREDITOS (ultimas entradas)\n")
    for line in last:
        print(" ", line)


def show_cache_status():
    """Muestra el estado del cache local."""
    if not CACHE_FILE.exists():
        print("\n📦 Sin cache local todavia.\n")
        return

    with open(CACHE_FILE) as f:
        cache = json.load(f)

    now = datetime.now(timezone.utc)
    print("\n📦 ESTADO DEL CACHE\n")
    print(f"  {'Liga':<45} {'Hace (min)':>12}  {'Creditos restantes':>18}")
    print("  " + "-" * 80)

    for sport_key, info in cache.items():
        fetched = datetime.fromisoformat(info["fetched_at"])
        age_min = (now - fetched).total_seconds() / 60
        remaining = info.get("credits_remaining", "?")
        flag = "✅" if age_min < 240 else "⏰"
        print(f"  {flag} {sport_key:<43} {age_min:>10.0f}m  {remaining:>18}")

    print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Monitor de creditos API")
    parser.add_argument("--check", action="store_true", help="Consultar saldo actual (1 llamada API)")
    args = parser.parse_args()

    if args.check:
        print("\n💳 CONSULTANDO SALDO ACTUAL...\n")
        info = check_live_credits()
        print(f"  Creditos usados:     {info['used']}")
        print(f"  Creditos restantes:  {info['remaining']}")
        print(f"  Consultado:          {info['checked_at']}")

        remaining_int = info["remaining"]
        if str(remaining_int).isdigit() and int(remaining_int) < API_CREDITS_ALERT_THRESHOLD:
            print(f"\n  ⚠️  ALERTA: quedan menos de {API_CREDITS_ALERT_THRESHOLD} creditos!")
    else:
        show_cache_status()
        show_log_summary()
