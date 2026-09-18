"""
scripts/weekly_narrator.py
==========================
NARRADOR SEMANAL AUTÓNOMO — IA en su papel correcto: NO predice, NARRA.

Cada semana (tras el pipeline semanal) reúne los números de la semana
(resultados por mercado, brecha de calibración, CLV, alertas de sanidad,
señales activas) y pide al modelo local (Ollama, costo cero) un
diagnóstico en lenguaje natural: qué funcionó, qué falló, qué vigilar.

El texto se guarda en la tabla weekly_narrative y el dashboard lo muestra
en la sección "🧠 Diagnóstico IA de la semana".

REGLA DE HIERRO: el narrador jamás modifica probabilidades ni apuestas —
es la voz que explica lo que ya ocurrió.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from config.settings import USER_TIMEZONE


# ─────────────────────────────────────────────────────────────
# RECOLECCIÓN
# ─────────────────────────────────────────────────────────────

def collect_week() -> dict:
    df = pd.read_sql(text("""
        SELECT market, league, result, profit, stake, odds
        FROM bets_history
        WHERE result IN ('win','loss','push','half_win','half_loss')
          AND match_date >= NOW() - INTERVAL '7 days'
    """), engine)

    resolved = len(df)
    wins = int(df["result"].isin(["win", "half_win"]).sum()) if resolved else 0
    profit = float(df["profit"].sum()) if resolved else 0.0
    staked = float(df["stake"].sum()) if resolved else 0.0

    by_mkt = (df.groupby("market")["profit"].sum().sort_values()
                if resolved else pd.Series(dtype=float))

    cal = pd.read_sql(text("""
        SELECT AVG(probability) AS pred,
               AVG(CASE WHEN result IN ('win','half_win') THEN 1.0 ELSE 0.0 END) AS real
        FROM bets_history
        WHERE result IN ('win','loss','half_win','half_loss')
          AND probability IS NOT NULL
          AND match_date >= NOW() - INTERVAL '45 days'
    """), engine)
    gap = None
    if not cal.empty and pd.notna(cal.iloc[0]["pred"]):
        gap = round(float(cal.iloc[0]["pred"]) - float(cal.iloc[0]["real"]), 3)

    clv = pd.read_sql(text("""
        SELECT AVG(clv) AS avg_clv, COUNT(*) AS n FROM bets_history
        WHERE clv IS NOT NULL AND match_date >= NOW() - INTERVAL '30 days'
    """), engine)
    clv_avg = None
    clv_n = 0
    if not clv.empty and pd.notna(clv.iloc[0]["avg_clv"]):
        clv_avg = round(float(clv.iloc[0]["avg_clv"]), 4)
        clv_n = int(clv.iloc[0]["n"])

    pending = pd.read_sql(text("""
        SELECT COUNT(*) n FROM bets_history WHERE result='pending'
    """), engine)

    data = {
        "resueltas": resolved,
        "ganadas": wins,
        "profit_u": round(profit, 2),
        "apostado_u": round(staked, 2),
        "mejor_mercado": (by_mkt.index[-1] if resolved and len(by_mkt) else None),
        "mejor_mercado_profit": (round(float(by_mkt.iloc[-1]), 2) if resolved and len(by_mkt) else None),
        "peor_mercado": (by_mkt.index[0] if resolved and len(by_mkt) else None),
        "peor_mercado_profit": (round(float(by_mkt.iloc[0]), 2) if resolved and len(by_mkt) else None),
        "calibracion_predicha_pct": (round(float(cal.iloc[0]["pred"]) * 100, 1)
                                     if not cal.empty and pd.notna(cal.iloc[0]["pred"]) else None),
        "calibracion_real_pct": (round(float(cal.iloc[0]["real"]) * 100, 1)
                                 if not cal.empty and pd.notna(cal.iloc[0]["real"]) else None),
        "brecha_calibracion_pts": gap,
        "clv_medio_30d": clv_avg,
        "clv_muestra": clv_n,
        "pendientes": int(pending.iloc[0]["n"]) if not pending.empty else 0,
    }
    return data


# ─────────────────────────────────────────────────────────────
# PROMPT (puro — testeable)
# ─────────────────────────────────────────────────────────────

def build_prompt(data: dict) -> str:
    return (
        "Eres un analista de apuestas deportivas. Con SOLO los datos JSON "
        "siguientes, escribe un diagnóstico de la semana en español: 4 a 6 "
        "frases. Estructura: (1) cómo fue la semana con números, (2) qué "
        "funcionó, (3) qué falló, (4) una recomendación accionable. NO "
        "inventes datos que no estén en el JSON. Tono: profesional, directo.\n\n"
        "Datos:\n" + json.dumps(data, ensure_ascii=False)
    )


# ─────────────────────────────────────────────────────────────
# NARRACIÓN + PERSISTENCIA
# ─────────────────────────────────────────────────────────────

def narrate(data: dict) -> tuple[str, str] | tuple[None, None]:
    """Devuelve (texto, engine). Ollama local primero; None si no hay modelo."""
    from src.utils.ollama_client import ask
    out = ask(build_prompt(data))
    if out:
        return out, "ollama"
    return None, None


def _ensure_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS weekly_narrative (
            week_start DATE PRIMARY KEY,
            narrative TEXT NOT NULL,
            engine TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))


def save_narrative(narrative: str, eng: str):
    from zoneinfo import ZoneInfo
    from config.settings import USER_TIMEZONE
    today = datetime.now(ZoneInfo(USER_TIMEZONE)).date()
    with engine.begin() as conn:
        _ensure_table(conn)
        conn.execute(text("""
            INSERT INTO weekly_narrative (week_start, narrative, engine)
            VALUES (:w, :n, :e)
            ON CONFLICT (week_start) DO UPDATE SET
                narrative = EXCLUDED.narrative, engine = EXCLUDED.engine,
                created_at = NOW()
        """), {"w": today - timedelta(days=(today.weekday())), "n": narrative, "e": eng})


def generate_weekly_narrative(verbose: bool = True) -> str | None:
    from src.utils.ollama_client import ollama_available
    if not ollama_available():
        if verbose:
            print("   Narrador IA: Ollama no disponible o sin modelo de chat "
                  "(ollama pull qwen2.5:7b) — omitido")
        return None
    data = collect_week()
    text_out, eng = narrate(data)
    if text_out:
        save_narrative(text_out, eng)
        if verbose:
            print(f"   🧠 Narrador ({eng}): {text_out[:120]}...")
    elif verbose:
        print("   Narrador IA: sin respuesta del modelo local")
    return text_out


if __name__ == "__main__":
    generate_weekly_narrative()
