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

29-oct-26: el saldo cruzó el umbral de parada el 28-oct, 3.5 días antes de la
renovación, con el presupuesto puesto. Dos huecos:
  - el presupuesto solo frenaba la recarga dirigida del closing; su
    actualización normal (ligas cada 12 h y mercados por evento de las
    próximas 48 h) no tenía tope y era ~85% del gasto (23-24 oct: ~2,200);
  - lo que no se frena (morning, evening y resultados, ~200 por día) salía
    del mismo presupuesto, así que el último día el saldo terminaba ~200 por
    debajo de la reserva, bajo el umbral de parada.
Ahora todo el closing gasta del presupuesto, y el presupuesto del closing es
el del día menos MANDATORY_PER_DAY. Los avisos de créditos van una vez por
día (alert_once): antes cada corrida del closing mandaba el suyo, cada 30 min.
"""

from datetime import datetime, timezone

from config.settings import env_int

STATE_KEY = "odds_credit_day"
RESET_DAY = env_int("ODDS_API_RESET_DAY", 1)          # día del mes en que se renueva el plan
RESERVE = env_int("ODDS_API_RESERVE", 600)             # nunca se presupuesta por debajo de esto
# lo que gastan cada día morning, evening y resultados, que no se frenan
# (medido 23-28 oct: ~60 + ~66 + ~114 antes de pedir solo las ligas que faltan)
MANDATORY_PER_DAY = env_int("ODDS_API_MANDATORY_PER_DAY", 200)
ALERT_KEY = "odds_credit_alert"


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


def run_budget(state: dict, remaining: int, mandatory: int = MANDATORY_PER_DAY) -> float:
    """Créditos que todavía puede gastar el closing hoy: lo del día menos lo
    ya gastado y menos lo que morning, evening y resultados necesitan."""
    spent = max(0, state["start"] - remaining)
    return max(0.0, state["allowance"] - mandatory - spent)


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


def alert_once(kind: str, html: str, now: datetime | None = None, send=None) -> bool:
    """Manda el aviso de créditos `kind` una sola vez por día UTC (29-oct-26:
    con el saldo bajo el umbral, cada corrida mandaba el suyo, ~50 al día).
    Si la base no se puede leer, lo manda: mejor repetido que callado.
    Devuelve True si lo mandó."""
    from src.utils.model_state import load_state, save_state
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    try:
        sent = load_state(ALERT_KEY) or {}
    except Exception:
        sent = {}
    if sent.get(kind) == today:
        print(f"   (aviso de créditos '{kind}' ya enviado hoy)")
        return False
    if send is None:
        from scripts.notify_telegram import send_message as send
    send(html)
    try:
        save_state(ALERT_KEY, {**sent, kind: today}, history=False)
    except Exception:
        pass
    return True
