"""
scripts/clv_gate.py
===================
CLV como métrica de DECISIÓN (no solo auditoría).

Con pocas apuestas el ROI es puro ruido; el CLV promedio predice la
rentabilidad de largo plazo mucho antes. Regla:

  Si un mercado tiene >= MIN_BETS (default 100) bets con closing_odds en los
  últimos LOOKBACK días y CLV promedio < CLV_FLOOR (default -0.005, escala de
  probabilidad), el mercado se considera sin ventaja real contra el cierre
  y se BLOQUEA: se escribe en config/clv_blocked_markets.json, que el
  prediction_pipeline suma a sus kill-switches.

Se desbloquea solo: si el CLV trailing recupera (o la muestra cae por
rotación de la ventana), el mercado sale del archivo y vuelve a apostarse.

Ejecutar semanalmente (lo llama el orchestrator --mode weekly).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine

ROOT = Path(__file__).parent.parent
BLOCKED_FILE = ROOT / "config" / "clv_blocked_markets.json"

LOOKBACK_DAYS = 120
_LEAGUE_MIN_BETS = 20      # muestra mínima para auto-bloquear liga
_LEAGUE_CLV_FLOOR = -0.05  # CLV medio peor que -5% → auto-bloqueo      # ventana trailing
MIN_BETS      = 100      # muestra mínima para bloquear un mercado
CLV_FLOOR     = -0.005   # CLV medio peor que -0.5% (escala prob) = sin edge


def run_clv_gate(verbose: bool = True) -> dict:
    try:
        df = pd.read_sql(text(f"""
            SELECT market, odds, closing_odds
            FROM bets_history
            WHERE closing_odds IS NOT NULL
              AND closing_odds > 1
              AND odds > 1
              AND result IN ('win', 'loss', 'half_win', 'half_loss')
              AND match_date >= NOW() - INTERVAL '{LOOKBACK_DAYS} days'
        """), engine)
    except Exception as e:
        if verbose:
            print(f"❌ clv_gate: no se pudo leer bets_history: {e}")
        return {"status": "error", "error": str(e)}

    if df.empty:
        if verbose:
            print("clv_gate: sin datos de CLV — no se bloquea nada.")
        _write_blocked([], verbose=False)
        return {"status": "no_data", "blocked": []}

    # CLV en escala de probabilidad (misma definición que clv_tracker)
    df["clv"] = 1.0 / df["closing_odds"] - 1.0 / df["odds"]

    blocked = []
    report = []
    for mkt, sub in df.groupby("market"):
        n = len(sub)
        avg = float(sub["clv"].mean())
        entry = {"market": str(mkt), "n": n, "avg_clv": round(avg, 5)}
        if n >= MIN_BETS and avg < CLV_FLOOR:
            blocked.append(str(mkt))
            entry["blocked"] = True
        report.append(entry)

    # ── GATE POR LIGA (n>=20, CLV <= -5%) ─────────────────────────────
    # Protege las ligas re-habilitadas: si vuelven a fallar con muestra,
    # vuelven a bloquearse solas. Mismo criterio que los mercados.
    blocked_leagues = []
    try:
        lg = pd.read_sql(text(f"""
            SELECT league, odds, closing_odds
            FROM bets_history
            WHERE closing_odds IS NOT NULL AND closing_odds > 1 AND odds > 1
              AND league IS NOT NULL
              AND result IN ('win', 'loss', 'push', 'half_win', 'half_loss')
              AND match_date >= NOW() - INTERVAL '{LOOKBACK_DAYS} days'
        """), engine)
        if not lg.empty:
            lg["clv"] = 1.0 / lg["closing_odds"] - 1.0 / lg["odds"]
            for l, sub in lg.groupby("league"):
                if len(sub) < _LEAGUE_MIN_BETS:
                    continue
                avg = float(sub["clv"].mean())
                if avg <= _LEAGUE_CLV_FLOOR:
                    blocked_leagues.append(str(l))
    except Exception as e:
        print(f"   ⚠️  League gate omitido: {e}")
    _write_league_blocked(blocked_leagues, verbose=verbose)

    _write_blocked(blocked, verbose=verbose)

    if verbose:
        print(f"\n🚦 CLV GATE (últimos {LOOKBACK_DAYS}d, piso {CLV_FLOOR:+.3f}, n>={MIN_BETS})")
        for e in sorted(report, key=lambda x: x["avg_clv"]):
            flag = " ⛔ BLOQUEADO" if e.get("blocked") else ""
            print(f"   {e['market']:<16} n={e['n']:>4}  CLV medio={e['avg_clv']:+.4f}{flag}")
        if not blocked:
            print("   ✅ Ningún mercado cumple criterio de bloqueo")
        if blocked_leagues:
            print(f"   ⛔ LIGAS bloqueadas: {', '.join(blocked_leagues)}")
        else:
            print("   ✅ Ninguna liga cumple criterio de bloqueo")

    return {
        "status": "ok",
        "blocked": blocked,
        "blocked_leagues": blocked_leagues,
        "by_market": report,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_blocked(blocked: list, verbose: bool = True):
    payload = {
        "blocked_markets": blocked,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "lookback_days": LOOKBACK_DAYS,
            "min_bets": MIN_BETS,
            "clv_floor": CLV_FLOOR,
        },
    }
    try:
        BLOCKED_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        if verbose:
            print(f"⚠️  clv_gate: no se pudo escribir {BLOCKED_FILE}: {e}")


LEAGUE_BLOCKED_FILE = ROOT / "config" / "clv_blocked_leagues.json"


def _write_league_blocked(leagues: list, verbose: bool = True):
    payload = {
        "blocked_leagues": leagues,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {"min_bets": _LEAGUE_MIN_BETS, "clv_floor": _LEAGUE_CLV_FLOOR},
    }
    try:
        LEAGUE_BLOCKED_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        if verbose:
            print(f"⚠️  clv_gate: no se pudo escribir {LEAGUE_BLOCKED_FILE}: {e}")


def load_clv_blocked_leagues() -> set:
    """Lee config/clv_blocked_leagues.json → set de ligas a bloquear."""
    try:
        if LEAGUE_BLOCKED_FILE.exists():
            data = json.loads(LEAGUE_BLOCKED_FILE.read_text(encoding="utf-8"))
            return set(data.get("blocked_leagues", []))
    except Exception:
        pass
    return set()


def load_clv_blocked_markets() -> set:
    """Lee config/clv_blocked_markets.json → set de mercados a bloquear."""
    try:
        if BLOCKED_FILE.exists():
            data = json.loads(BLOCKED_FILE.read_text(encoding="utf-8"))
            return set(data.get("blocked_markets", []))
    except Exception:
        pass
    return set()


if __name__ == "__main__":
    run_clv_gate()
