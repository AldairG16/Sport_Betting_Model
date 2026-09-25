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
