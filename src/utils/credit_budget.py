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

29-oct-26 (2): los cierres primero. Desde el 24-oct casi no se guardaban
cierres válidos (24-25 oct: 15-19% de los partidos; antes 98-100%): a las
13:30 UTC del sábado el closing ya estaba "en pausa, 803 de 364 créditos",
porque los mercados por evento (morning, evening y la actualización normal
del closing) se comían el día antes de que empezaran los partidos. En un
fin de semana completo los cierres por liga cuestan ~300 (2 recargas por
horario de liga, medido 10-20 oct) y todo lo demás pedía ~1,200. Ahora:
  - capture_reserve: lo que todavía necesitan hoy los cierres por liga;
  - extras_budget: lo único que pueden gastar los mercados por evento (en
    cualquier modo), la actualización normal del closing y los mercados por
    evento de las candidatas sombra. Nunca toca la reserva de los cierres.
"""

from datetime import datetime, timedelta, timezone

from config.settings import env_int

STATE_KEY = "odds_credit_day"
RESET_DAY = env_int("ODDS_API_RESET_DAY", 1)          # día del mes en que se renueva el plan
RESERVE = env_int("ODDS_API_RESERVE", 600)             # nunca se presupuesta por debajo de esto
# lo que gastan cada día morning, evening y resultados, que no se frenan
# (medido 23-28 oct: ~60 + ~66 + ~114 antes de pedir solo las ligas que faltan)
MANDATORY_PER_DAY = env_int("ODDS_API_MANDATORY_PER_DAY", 200)
ALERT_KEY = "odds_credit_alert"
# recargas de liga por horario (liga + hora de inicio) en su ventana de cierre:
# 2.0-2.2 medido 10-20 oct (91 recargas para 42 horarios el 17-oct)
CAPTURES_PER_SLOT = env_int("ODDS_API_CAPTURES_PER_SLOT", 2)
CAPTURE_LEAD_MIN = 80          # la recarga dirigida mira hasta 80 min adelante (CLOSING_WINDOW_MIN)
STOP_THRESHOLD = env_int("API_CREDITS_STOP_THRESHOLD", 500)   # el mismo umbral de parada de update_upcoming_matches
HISTORY_DAYS = 14


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
    """Estado del día: si cambió el día UTC, el saldo actual es el inicial y
    lo que bajó el saldo del día anterior queda en `history` (29-oct-26, para
    medir el ritmo: credit_pace)."""
    today = now.strftime("%Y-%m-%d")
    prev = previous or {}
    hist = dict(prev.get("history") or {})
    if prev.get("day") == today and isinstance(prev.get("start"), int) and "allowance" in prev:
        return {"day": today, "start": prev["start"], "allowance": prev["allowance"], "history": hist}  # fijo todo el día
    # si el saldo subió, se renovó el plan en medio: ese día no se puede medir
    if prev.get("day") and isinstance(prev.get("start"), int) and remaining <= prev["start"]:
        hist[prev["day"]] = {"spent": prev["start"] - remaining, "allowance": prev.get("allowance")}
    hist = dict(sorted(hist.items())[-HISTORY_DAYS:])
    return {"day": today, "start": remaining, "allowance": round(daily_allowance(remaining, now), 1),
            "history": hist}


def credit_pace(state: dict | None, now: datetime, stop: int = STOP_THRESHOLD, last: int = 3) -> dict | None:
    """Al ritmo de los últimos `last` días medidos, ¿cuándo cruza el saldo el
    umbral de parada? None sin estado o con menos de 2 días medidos. El 28-oct
    lo cruzó 3.5 días antes de la renovación y nada lo midió antes."""
    if not state or not isinstance(state.get("start"), int):
        return None
    days = [v["spent"] for _, v in sorted((state.get("history") or {}).items())[-last:]
            if isinstance(v, dict) and isinstance(v.get("spent"), (int, float))]
    if len(days) < 2:
        return None
    pace = sum(days) / len(days)
    reset = next_reset(now)
    days_left = (reset - now).total_seconds() / 86400.0
    lasts = (state["start"] - stop) / pace if pace > 0 else float("inf")
    hist = state.get("history") or {}
    yesterday = hist[max(hist)] if hist else None
    return {"pace": pace, "days": len(days), "lasts_days": lasts, "days_left": days_left,
            "short_days": max(0.0, days_left - lasts), "reset": reset, "yesterday": yesterday}


def run_budget(state: dict, remaining: int, mandatory: int = MANDATORY_PER_DAY) -> float:
    """Créditos que todavía puede gastar el closing hoy: lo del día menos lo
    ya gastado y menos lo que morning, evening y resultados necesitan."""
    spent = max(0, state["start"] - remaining)
    return max(0.0, state["allowance"] - mandatory - spent)


def capture_reserve(kickoffs, now: datetime, league_cost: int, per_slot: int = CAPTURES_PER_SLOT) -> float:
    """Créditos que todavía necesitan hoy los cierres por liga: cada horario
    (liga, hora de inicio) cuya ventana de cierre cae antes de que acabe el
    día UTC se recarga ~per_slot veces. `kickoffs`: pares (liga, datetime)."""
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end += timedelta(minutes=CAPTURE_LEAD_MIN)
    slots = {(sport, ko) for sport, ko in kickoffs if now < ko <= end}
    return float(len(slots) * per_slot * league_cost)


def extras_budget(state: dict, remaining: int, reserve: float, mandatory: int = MANDATORY_PER_DAY) -> float:
    """Lo que pueden gastar hoy los mercados por evento y las recargas que no
    son cierres: lo del closing menos la reserva de los cierres que faltan."""
    return max(0.0, run_budget(state, remaining, mandatory) - reserve)


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
