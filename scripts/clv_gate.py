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
# Ronda 7 (B1/R10): el criterio fijo n>=100 era INALCANZABLE — con las tasas
# reales (Q-Q: máximo 94 bets/120d en el mercado más grande, cobertura de
# closing 69-85%) el gate de mercados nunca había podido dispararse. Nuevo
# criterio ESTADÍSTICO: bloquear cuando el límite superior del IC95
# unilateral del CLV medio queda bajo cero (CLV significativamente negativo).
# Con un efecto real (CLV ≈ −1 a −2pt) alcanza con n=30-40, que sí es
# acumulable en semanas para los mercados grandes.
MIN_BETS_STAT = 30         # muestra mínima para el test estadístico
CLV_Z_95 = 1.645           # z unilateral 95% (normal aprox. del CLV medio)
MIN_BETS      = 100      # legado: solo se usa en el payload de política
CLV_FLOOR     = -0.005   # legado: documentación del criterio viejo


def market_clv_blocked(n: int, mean: float, sd: float) -> bool:
    """
    Criterio B1 (ronda 7): un mercado se bloquea cuando su CLV medio es
    significativamente negativo — límite superior del IC95 unilateral por
    debajo de cero. Con sd típico de CLV (~0.04) y efecto real (−1 a −2pt),
    dispara con n=30-40 en vez de n=100.
    """
    if n < MIN_BETS_STAT or sd is None or sd <= 0:
        return False
    upper = mean + CLV_Z_95 * sd / (n ** 0.5)
    return upper < 0


def _deviation_band(dev: float) -> str:
    """Banda de desvío para el análisis CLV del shadow (B2, ronda 7)."""
    if dev is None:
        return "sin_ref"
    b = abs(dev) * 100
    if b < 5:
        return "0-5"
    if b < 10:
        return "5-10"
    if b < 15:
        return "10-15"
    if b < 19:
        return "15-19"
    if b < 25:
        return "19-25"
    if b <= 30:
        return "25-30"
    return "30+"


def shadow_clv_bands(verbose: bool = True) -> dict:
    """
    CLV medio por banda de desvío de las candidatas SHADOW (B2, ronda 7).
    Responde empíricamente si la ventana apostable [piso, techo] está en el
    lado correcto: CLV positivo en bandas bajas excluidas ⇒ bajar el piso;
    CLV plano/negativo abajo y positivo dentro ⇒ ventana validada.
    Solo tiene sentido cuando shadow_bets acumule closing_odds (semanas).
    """
    try:
        df = pd.read_sql(text("""
            SELECT deviation, odds, closing_odds
            FROM shadow_bets
            WHERE closing_odds IS NOT NULL AND closing_odds > 1 AND odds > 1
              AND deviation IS NOT NULL
        """), engine)
    except Exception as e:
        # tabla aún no creada → no es error en el primer ciclo
        if verbose:
            print(f"   ℹ️  shadow_clv_bands: {type(e).__name__} (¿tabla sin crear?)")
        return {"status": "no_table"}

    if df.empty:
        if verbose:
            print("   ℹ️  shadow_clv_bands: sin closing acumulado todavía")
        return {"status": "no_data"}

    df["clv"] = 1.0 / df["closing_odds"] - 1.0 / df["odds"]
    df["band"] = df["deviation"].map(_deviation_band)
    out = {}
    if verbose:
        print("\n🌑 SHADOW CLV por banda de desvío (candidatas no apostadas):")
    for band, sub in df.groupby("band"):
        n = len(sub)
        avg = float(sub["clv"].mean())
        out[band] = {"n": n, "avg_clv": round(avg, 5)}
        if verbose:
            print(f"   [{band:>5}] n={n:>4}  CLV medio={avg:+.4f}")
    return {"status": "ok", "bands": out}


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
        sd = float(sub["clv"].std(ddof=1)) if n > 1 else None
        entry = {"market": str(mkt), "n": n, "avg_clv": round(avg, 5)}
        # B1 (ronda 7): criterio estadístico — CLV significativamente negativo
        # (IC95 unilateral por debajo de 0). Sustituye al n>=100 fijo
        # inalcanzable (R10).
        if market_clv_blocked(n, avg, sd):
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
        print(f"\n🚦 CLV GATE (últimos {LOOKBACK_DAYS}d, criterio: IC95 unilateral "
              f"< 0 con n>={MIN_BETS_STAT})")
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
            "criterion": "mean_clv + 1.645 * sd/sqrt(n) < 0 (IC95 unilateral)",
            "min_bets_stat": MIN_BETS_STAT,
            "legacy_min_bets": MIN_BETS,
            "legacy_clv_floor": CLV_FLOOR,
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
