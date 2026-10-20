"""
src/utils/credit_budget.py
==========================
Presupuesto diario de créditos de The Odds API (20-oct-26).

El plan es de 20,000 créditos y se renueva el día 1 de cada mes (medido: el
saldo pasó de 9,548 a 19,958 el 1-oct a las 03:06 UTC). Del 10 al 20 de
octubre se gastaron ~925 por día, el 90% en el closing: ~90 recargas de liga
(3 créditos) y 55-78 recargas por evento solo para candidatas sombra
(7 créditos) en un día de fin de semana. A ese ritmo el saldo se acababa el
26-oct, y los topes fijos (sin cierres bajo 1,500, sin cuotas bajo 500)
dejaban al sistema sin aprender la última semana del mes.

Regla: el closing gasta como máximo
    (saldo al empezar el día − reserva) / días que faltan para la renovación
por día. El día se mide en UTC y su saldo inicial se guarda en model_state
(`odds_credit_day`): el gasto del día cuenta todo lo que bajó el saldo, sea
del morning, del evening o del closing. Lo que no se gasta un día queda para
los siguientes (el presupuesto se recalcula cada día con el saldo real).
Morning y evening no se frenan: sin sus cuotas no hay predicciones.
"""

from datetime import datetime, timezone

from config.settings import env_int

STATE_KEY = "odds_credit_day"
RESET_DAY = env_int("ODDS_API_RESET_DAY", 1)          # día del mes en que se renueva el plan
RESERVE = env_int("ODDS_API_RESERVE", 600)             # nunca se presupuesta por debajo de esto


def next_reset(now: datetime, reset_day: int = RESET_DAY) -> datetime:
    """Próxima renovación (00:00 UTC del día `reset_day`)."""
    y, m = now.year, now.month
    candidate = datetime(y, m, reset_day, tzinfo=timezone.utc)
    if candidate <= now:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        candidate = datetime(y, m, reset_day, tzinfo=timezone.utc)
    return candidate


def daily_allowance(start_remaining: int, now: datetime, reserve: int = RESERVE,
                    reset_day: int = RESET_DAY) -> float:
    """Créditos para todo el día: lo que sobra de la reserva entre los días
    que faltan (mínimo medio día, para no gastarlo todo la víspera)."""
    days_left = max((next_reset(now, reset_day) - now).total_seconds() / 86400.0, 0.5)
    return max(0.0, (start_remaining - reserve) / days_left)


def day_state(previous: dict | None, remaining: int, now: datetime) -> dict:
    """Estado del día: si cambió el día UTC, el saldo actual es el inicial."""
    today = now.strftime("%Y-%m-%d")
    prev = previous or {}
    if prev.get("day") == today and isinstance(prev.get("start"), int) and "allowance" in prev:
        return {"day": today, "start": prev["start"], "allowance": prev["allowance"]}   # fijo todo el día
    return {"day": today, "start": remaining, "allowance": round(daily_allowance(remaining, now), 1)}


def run_budget(state: dict, remaining: int) -> float:
    """Créditos que todavía puede gastar el closing hoy."""
    spent = max(0, state["start"] - remaining)
    return max(0.0, state["allowance"] - spent)


def load_and_update(remaining: int | None, now: datetime | None = None) -> dict | None:
    """Lee y guarda el estado del día. None si no se conoce el saldo (no se
    frena nada: el auto-stop de update_upcoming_matches sigue valiendo)."""
    if remaining is None:
        return None
    from src.utils.model_state import load_state, save_state
    now = now or datetime.now(timezone.utc)
    state = day_state(load_state(STATE_KEY), remaining, now)
    save_state(STATE_KEY, state, history=False)
    return state
