"""
src/utils/bet_status.py
=======================
Estado de una bet de bets_history que no cabe en la columna `result`.

Una bet CANCELADA antes del kickoff (scripts/revalidate_pending_bets.py:
línea movida, sin valor frente a Pinnacle o sin referencia; o el lineup
guard) se guarda como result='stale', profit 0, con la razón en decision_log.
'stale' también significa "sin fuente de resultado", así que quien recupera
stale (resolve_stale_bets, backfill_espn_results) debe dejar fuera las
canceladas: no se apostaron y no cuentan para el ROI.
"""

CANCELLED_SQL = ("COALESCE((decision_log ? 'lineup_guard') OR "
                 "(LEFT(decision_log->'revalidation'->>'decision', 9) = 'cancelled'), FALSE)")

# Una apuesta JUGADA y liquidada. Todo lo que mide rendimiento (ROI, acierto,
# conteo de bets) debe filtrar por esto: 'stale' (cancelada o sin fuente de
# resultado) tiene profit 0 con su stake y, contada, diluye el ROI. El reporte
# semanal del 28-sep-26 las contaba: "0 bets" con 14 canceladas por mercado y
# un acumulado de 1,180 bets / ROI −7.3% cuando lo real era 1,128 / −7.7%.
RESOLVED_RESULTS = ("win", "loss", "push", "half_win", "half_loss")
RESOLVED_SQL = "result IN ('win', 'loss', 'push', 'half_win', 'half_loss')"
