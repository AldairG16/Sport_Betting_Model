"""
src/utils/pipeline_runs.py
==========================
Registro de cada corrida del orquestador y diagnóstico de su regularidad
(24-sep-26).

Por qué: morning, evening, closing y weekly los dispara un servicio externo
(cron-job.org → workflow_dispatch). Si ese servicio deja de disparar, nada
falla: simplemente no corre. El watchdog detectaba morning/evening parados
(upcoming_matches sin actualizar), pero NO el closing ni el weekly:
  - sin closing cada 30 min no llegan las confirmaciones oficiales antes de
    cada partido y el aprendizaje se queda sin cierres válidos;
  - sin weekly el sistema deja de aprender y recalibrar.
Y el cron propio de GitHub para el closing sigue disparando a ratos (hueco
medio de 4 h, medido): un closing "que corrió hace poco" no prueba que el
dispatcher viva. Por eso se mira también cuántas corridas hubo.
"""

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

RUNS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        id       SERIAL PRIMARY KEY,
        mode     TEXT NOT NULL,
        ran_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        failed   INT NOT NULL DEFAULT 0,
        seconds  REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_mode_ran ON pipeline_runs (mode, ran_at DESC)",
)
# Sin poda: ~50 filas/día (casi todas del closing) son ~1 MB al año.

# El closing corre cada 30 min (≈12 por ventana de 6 h).
CLOSING_MAX_GAP_H = 2.0
CLOSING_WINDOW_H = 6.0
CLOSING_MIN_RUNS = 6
# El weekly corre los lunes; un día de holgura.
WEEKLY_MAX_GAP_D = 8.0
# Resultados y resolvedor corren dos veces al día, disparados desde el latido
# del closing (scripts/heartbeat_dispatch.py). El resolvedor pasó del 11-may al
# 1-oct-26 sin correr una sola vez y nada lo detectó.
# Morning y evening (29-oct-26), una vez al día cada uno. Antes el watchdog lo
# medía con upcoming_matches.updated_at, que también se queda quieto si el
# pipeline corre sin créditos de The Odds API: "PIPELINE SIN CORRER" falso el
# 29-oct, con las dos corridas sanas.
DAILY_MAX_GAP_H = {"results": 30.0, "resolve_pending": 30.0, "morning": 26.0, "evening": 26.0}
_HEARTBEAT = ("Lo dispara el closing cada 30 min (heartbeat_dispatch.py): revisa {wf} y el paso "
              "del closing que lo dispara.")
_DISPATCH = "Lo dispara cron-job.org (workflow_dispatch contra master): revisa {wf} en GitHub Actions."
DAILY_LABELS = {      # título, workflow, sin él…, quién lo dispara, veces al día
    "results": ("REINTENTO DE RESULTADOS", "late_results.yml",
                "los partidos que terminan tarde no se liquidan hasta el día siguiente", _HEARTBEAT, 2),
    "resolve_pending": ("RESOLVEDOR DE PENDIENTES", "resolve_pending.yml",
                        "las apuestas sin marcador en la API quedan sin resultado", _HEARTBEAT, 2),
    "morning": ("MORNING", "morning.yml", "no salen los picks ni el resumen de la mañana", _DISPATCH, 1),
    "evening": ("EVENING", "evening.yml", "no se liquida el día ni corre el chequeo de salidas", _DISPATCH, 1),
}


def record_run(engine, mode: str, failed: int, seconds: float | None) -> None:
    """Una fila por corrida."""
    with engine.begin() as conn:
        for stmt in RUNS_DDL:
            conn.execute(text(stmt))
        conn.execute(text("""
            INSERT INTO pipeline_runs (mode, failed, seconds)
            VALUES (:mode, :failed, :seconds)
        """), {"mode": mode, "failed": int(failed),
               "seconds": None if seconds is None else round(float(seconds), 1)})


def _hours(now, t) -> float:
    return (now - t).total_seconds() / 3600


def closing_issue(recent, first_ever, last_run, now) -> str | None:
    """
    `recent`: horas de las corridas del closing de las últimas horas (al
    menos CLOSING_WINDOW_H); `first_ever` / `last_run`: la primera y la
    última registradas. None = sano, o sin historial para opinar.
    """
    if first_ever is None or last_run is None:
        return None
    ts = sorted(pd.to_datetime(list(recent), utc=True))
    last_age = _hours(now, pd.Timestamp(last_run))
    if last_age > CLOSING_MAX_GAP_H:
        return ("🔴 <b>CLOSING SIN CORRER</b>\n"
                f"Última corrida hace {last_age:.1f} h (debe correr cada 30 min).\n"
                "→ Revisa en cron-job.org que el trabajo del closing siga activo. Sin él "
                "no llegan las confirmaciones antes de cada partido y el aprendizaje "
                "no recibe cierres.")
    if _hours(now, pd.Timestamp(first_ever)) < CLOSING_WINDOW_H:
        return None                     # menos historial que la ventana: no opina
    n = sum(1 for t in ts if _hours(now, t) <= CLOSING_WINDOW_H)
    if n < CLOSING_MIN_RUNS:
        return ("🟡 <b>CLOSING IRREGULAR</b>\n"
                f"Solo {n} corridas en las últimas {CLOSING_WINDOW_H:.0f} h "
                f"(esperadas ~{int(CLOSING_WINDOW_H * 2)}).\n"
                "→ El disparo cada 30 min de cron-job.org parece caído y solo quedan "
                "las corridas sueltas de GitHub. Revisa el trabajo del closing.")
    return None


def weekly_issue(last_weekly, now) -> str | None:
    """None = sano o sin dato."""
    if last_weekly is None or pd.isna(last_weekly):
        return None
    age_d = _hours(now, pd.Timestamp(last_weekly)) / 24
    if age_d <= WEEKLY_MAX_GAP_D:
        return None
    return ("🔴 <b>WEEKLY SIN CORRER</b>\n"
            f"Última corrida hace {age_d:.0f} días (debe correr cada lunes).\n"
            "→ Revisa en cron-job.org el trabajo del weekly. Sin él el sistema no "
            "aprende ni recalibra.")


def daily_issue(mode: str, last_run, now) -> str | None:
    """Corridas diarias: None = sano. Sin ninguna corrida registrada también
    es problema: así se escondió el resolvedor."""
    title, workflow, consequence, trigger, per_day = DAILY_LABELS[mode]
    max_h = DAILY_MAX_GAP_H[mode]
    if last_run is not None and not pd.isna(last_run):
        age = _hours(now, pd.Timestamp(last_run))
        if age <= max_h:
            return None
        when = f"Última corrida hace {age:.0f} h"
    else:
        when = "No hay ninguna corrida registrada"
    veces = "1 vez" if per_day == 1 else f"{per_day} veces"
    return (f"🟡 <b>{title} SIN CORRER</b>\n"
            f"{when} (debe correr {veces} al día).\n"
            f"→ {trigger.format(wf=workflow)} Sin él {consequence}.")


def _ts(v):
    t = pd.to_datetime(v, utc=True, errors="coerce")
    return None if pd.isna(t) else t


def check_scheduled_runs(engine, now: datetime | None = None) -> list[str]:
    """Problemas del closing y del weekly para el watchdog ([] = sano)."""
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    with engine.begin() as conn:
        for stmt in RUNS_DDL:
            conn.execute(text(stmt))
    closing = pd.read_sql(text(f"""
        SELECT ran_at FROM pipeline_runs
        WHERE mode = 'closing' AND ran_at >= NOW() - INTERVAL '{CLOSING_WINDOW_H + 1:.0f} hours'
    """), engine)
    span = pd.read_sql(text("""
        SELECT MIN(ran_at) AS first_ever, MAX(ran_at) AS last_run
        FROM pipeline_runs WHERE mode = 'closing'
    """), engine)
    # El weekly deja su huella también en model_state (anchor_weights): sirve
    # de respaldo antes de que exista su primera fila en pipeline_runs.
    weekly = pd.read_sql(text("""
        SELECT GREATEST(
            (SELECT MAX(ran_at) FROM pipeline_runs WHERE mode = 'weekly'),
            (SELECT MAX(updated_at) FROM model_state WHERE key = 'anchor_weights')
        ) AS last_weekly
    """), engine)
    daily = pd.read_sql(text("""
        SELECT mode, MAX(ran_at) AS last_run FROM pipeline_runs
        WHERE mode IN ('results', 'resolve_pending', 'morning', 'evening') GROUP BY mode
    """), engine)
    last_daily = {r["mode"]: _ts(r["last_run"]) for _, r in daily.iterrows()}
    first_ever = _ts(span.iloc[0]["first_ever"]) if not span.empty else None
    last_run = _ts(span.iloc[0]["last_run"]) if not span.empty else None
    issues = []
    for issue in (
        closing_issue(closing["ran_at"] if not closing.empty else [], first_ever, last_run, now),
        weekly_issue(_ts(weekly.iloc[0]["last_weekly"]) if not weekly.empty else None, now),
        *(daily_issue(m, last_daily.get(m), now) for m in DAILY_MAX_GAP_H),
    ):
        if issue:
            issues.append(issue)
    return issues
