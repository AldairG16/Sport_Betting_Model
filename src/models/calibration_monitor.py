"""
Calibration Monitor
===================
Mide si las probabilidades del modelo son REALES o inventadas.

Problema que resuelve:
  Si el modelo dice "65% de probabilidad" y el equipo gana solo el 48%
  de las veces que el modelo dice 65%, hay un sesgo sistemático (sobreconfianza).
  Sin detectar esto, los "edges" del modelo son parcialmente ficticios.

Métricas:
  - Brier Score: MSE entre probabilidades y resultados (0=perfecto, 0.25=azar)
  - Calibration Buckets: para cada rango de prob, ¿qué % real gana?
  - Calibration Factor: actual_win_rate / predicted_win_rate por mercado
    → factor > 1: modelo subconfiado (gana más de lo que predice)
    → factor < 1: modelo sobreconfiado (gana menos de lo que predice)

Los factores se guardan en config/calibration_factors.json y se
aplican automáticamente en el pipeline para corregir el sesgo.

Mínimo recomendado: 50 bets por mercado para que los factores sean estables.
"""

import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import numpy as np

sys.path.append(str(Path(__file__).parent.parent.parent))

from config.database import engine

CALIBRATION_FILE = Path(__file__).parent.parent.parent / "config" / "calibration_factors.json"
MIN_BETS_FOR_CALIBRATION = 50   # mínimo por mercado para aplicar el factor
MARKETS = ["home_win", "draw", "away_win", "over25", "under25", "btts"]

# Reducción máxima / aumento máximo del factor de calibración
# Limitar para no sobre-corregir con pocos datos
CAL_FACTOR_MIN = 0.75
CAL_FACTOR_MAX = 1.30

# Ventana temporal para calibración: solo los últimos N días.
# Evita que apuestas viejas (temporadas previas, modelo anterior) contaminen
# el factor actual — si el modelo mejoró, las bets viejas sesgan hacia abajo.
CALIBRATION_WINDOW_DAYS = 60


# ─────────────────────────────────────────────────────────────
# CARGA / GUARDADO
# ─────────────────────────────────────────────────────────────

def load_calibration_factors() -> dict:
    """
    Carga los factores de calibración guardados.
    Si el archivo no existe, retorna factores neutros (1.0).
    """
    if CALIBRATION_FILE.exists():
        try:
            with open(CALIBRATION_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass

    return _neutral_factors()


def _neutral_factors() -> dict:
    return {
        market: {"factor": 1.0, "brier": None, "n_bets": 0, "win_rate_actual": None}
        for market in MARKETS
    }


def _save_calibration_factors(factors: dict):
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    # tz-aware UTC: evita ambigüedad cuando el script corre en distintas zonas
    factors["updated_at"]    = datetime.now(timezone.utc).isoformat()
    factors["window_days"]   = CALIBRATION_WINDOW_DAYS
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(factors, f, indent=2)


def get_calibration_factor(market: str) -> float:
    """
    Retorna el factor de calibración para un mercado específico.
    Usado en el pipeline para ajustar la probabilidad antes de calcular edge.

    Returns:
        float en [0.75, 1.30] — 1.0 si no hay suficientes datos
    """
    factors = load_calibration_factors()
    data = factors.get(market, {})
    if isinstance(data, dict):
        return float(data.get("factor", 1.0))
    return 1.0


# ─────────────────────────────────────────────────────────────
# CÓMPUTO
# ─────────────────────────────────────────────────────────────

def compute_calibration(min_bets: int = MIN_BETS_FOR_CALIBRATION,
                        verbose: bool = True) -> dict:
    """
    Lee bets_history, calcula Brier Score y factores de calibración por mercado.

    Args:
        min_bets: mínimo de bets por mercado para calcular factor (evita ruido)
        verbose:  imprimir reporte en consola

    Returns:
        dict con factores por mercado y métricas
    """
    try:
        # Filtro de ventana temporal: sólo últimos CALIBRATION_WINDOW_DAYS días.
        # Esto desacopla la calibración del modelo *actual* del histórico lejano
        # (bets generadas con thresholds/features distintos).
        df = pd.read_sql(f"""
            SELECT market, probability, result
            FROM bets_history
            WHERE result IN ('win', 'loss')
              AND probability IS NOT NULL
              AND probability > 0
              AND probability < 1
              AND match_date >= NOW() - INTERVAL '{CALIBRATION_WINDOW_DAYS} days'
        """, engine)
    except Exception as e:
        print(f"❌ calibration_monitor: no se pudo leer bets_history: {e}")
        return _neutral_factors()

    if df.empty:
        if verbose:
            print("⚠️  Sin bets resueltas para calibración")
        return _neutral_factors()

    # Resultado binario: 1 = win, 0 = loss
    df["outcome"] = (df["result"] == "win").astype(int)

    factors = {}
    summary_lines = [
        "\n📐 CALIBRACIÓN DEL MODELO",
        f"{'Mercado':<12} {'N':>5} {'Brier':>7} {'Pred%':>7} {'Real%':>7} {'Factor':>7} {'Estado':>12}",
        "─" * 60,
    ]

    for market in MARKETS:
        mdf = df[df["market"] == market].copy()

        if len(mdf) < min_bets:
            factors[market] = {
                "factor":           1.0,
                "brier":            None,
                "n_bets":           len(mdf),
                "win_rate_actual":  None,
                "win_rate_pred":    None,
            }
            summary_lines.append(
                f"{market:<12} {len(mdf):>5}   {'—':>7}   {'—':>7}   {'—':>7}   {'1.00':>7}  datos insuf."
            )
            continue

        prob    = mdf["probability"].values
        outcome = mdf["outcome"].values

        # ── Brier Score ───────────────────────────────────────────────────
        brier = float(np.mean((prob - outcome) ** 2))

        # ── Win rates ─────────────────────────────────────────────────────
        win_rate_pred   = float(np.mean(prob))
        win_rate_actual = float(np.mean(outcome))

        # ── Factor de calibración ─────────────────────────────────────────
        # actual / predicted: corrige el sesgo sistemático del modelo
        if win_rate_pred > 0:
            raw_factor = win_rate_actual / win_rate_pred
        else:
            raw_factor = 1.0

        # Suavizado: no corregir 100% con pocos datos
        # Con min_bets datos → 20% del factor; con 500 datos → 100%
        smoothing = min(1.0, len(mdf) / 500)
        factor = 1.0 + (raw_factor - 1.0) * smoothing

        # Cap de seguridad
        factor = round(max(CAL_FACTOR_MIN, min(CAL_FACTOR_MAX, factor)), 4)

        # ── Estado ────────────────────────────────────────────────────────
        if abs(factor - 1.0) < 0.03:
            estado = "calibrado ✅"
        elif factor > 1.0:
            estado = f"subconf +{(factor-1)*100:.0f}%"
        else:
            estado = f"sobreconf -{(1-factor)*100:.0f}%"

        factors[market] = {
            "factor":           factor,
            "brier":            round(brier, 5),
            "n_bets":           len(mdf),
            "win_rate_actual":  round(win_rate_actual, 4),
            "win_rate_pred":    round(win_rate_pred,   4),
        }

        summary_lines.append(
            f"{market:<12} {len(mdf):>5} {brier:>7.4f} "
            f"{win_rate_pred*100:>6.1f}% {win_rate_actual*100:>6.1f}% "
            f"{factor:>7.3f}  {estado}"
        )

    # ── Brier global ─────────────────────────────────────────────────────
    df["outcome_int"] = (df["result"] == "win").astype(int)
    global_brier = float(np.mean((df["probability"] - df["outcome_int"]) ** 2))
    summary_lines += [
        "─" * 60,
        f"Brier global: {global_brier:.4f}  "
        f"(ref: aleatorio=0.250, perfecto=0.000)",
        f"Total bets analizadas: {len(df)}",
    ]

    if verbose:
        print("\n".join(summary_lines))

    # ── Guardar ───────────────────────────────────────────────────────────
    _save_calibration_factors(factors)

    return factors


# ─────────────────────────────────────────────────────────────
# ALERTA TELEGRAM
# ─────────────────────────────────────────────────────────────

def check_calibration_alert(factors: dict) -> str | None:
    """
    Retorna un mensaje de alerta si la calibración es preocupante.
    Usado en el ciclo semanal para enviar alerta a Telegram si es necesario.

    Returns:
        str con el mensaje de alerta, o None si todo está bien
    """
    bad_markets = []

    for market, data in factors.items():
        if not isinstance(data, dict):
            continue
        factor = data.get("factor", 1.0)
        n      = data.get("n_bets", 0)
        brier  = data.get("brier")

        if n < MIN_BETS_FOR_CALIBRATION:
            continue

        # Factor muy lejos de 1.0 → problema sistemático
        if factor < 0.82 or factor > 1.20:
            bad_markets.append(
                f"  {market}: factor={factor:.2f}  Brier={brier:.4f}"
            )

    if not bad_markets:
        return None

    return (
        "⚠️ <b>ALERTA CALIBRACIÓN</b>\n\n"
        "Los siguientes mercados tienen sesgo sistemático:\n"
        + "\n".join(bad_markets)
        + "\n\n<i>Considera revisar los thresholds o reentrenar el modelo.</i>"
    )


# ─────────────────────────────────────────────────────────────
# REPORTE RÁPIDO
# ─────────────────────────────────────────────────────────────

def calibration_summary_for_telegram() -> str:
    """Genera un resumen de calibración para incluir en el reporte semanal."""
    factors = load_calibration_factors()

    lines = ["— <b>CALIBRACIÓN</b> —"]
    has_data = False

    for market in MARKETS:
        data = factors.get(market, {})
        if not isinstance(data, dict):
            continue
        factor = data.get("factor", 1.0)
        brier  = data.get("brier")
        n      = data.get("n_bets", 0)

        if n < MIN_BETS_FOR_CALIBRATION:
            continue

        has_data = True
        emoji = "✅" if abs(factor - 1.0) < 0.05 else ("📈" if factor > 1.0 else "📉")
        from scripts.notify_telegram import MARKET_LABELS
        label = MARKET_LABELS.get(market, market)
        brier_str = f" Brier={brier:.3f}" if brier else ""
        lines.append(f"  {emoji} {label}: {factor:.2f}x{brier_str} (n={n})")

    if not has_data:
        return ""

    return "\n".join(lines)


if __name__ == "__main__":
    factors = compute_calibration(verbose=True)
    alert = check_calibration_alert(factors)
    if alert:
        print(f"\n{alert}")
    else:
        print("\n✅ Calibración dentro de rangos aceptables")
