"""
src/utils/db_budget.py
======================
El presupuesto del plan gratis de Neon (11-nov-26).

El 9-nov Neon bloqueó la base: se pasó de la transferencia del plan gratis
(5.58 de 5 GB) y todo el programa quedó detenido. El dueño: "esto tiene que
funcionar 100% con planes gratis… no debemos sufrir estos ataques y pausar
todo por eso". Nada sabía cuánto se llevaba gastado ni frenaba antes.

Con lo que mide cada proceso (db_meter → pipeline_runs.bytes_read y awake_s)
se calcula lo gastado en el mes y, al ritmo de los últimos 3 días, a cuánto
se llega el día último. Con eso:

- avisos por Telegram al pasar del 50% y del 75% (una vez cada uno por mes);
- modo ahorro desde el 75%: los cierres corren cada hora (salvo partidos que
  empiezan en las próximas 2 h) y se saltan los pasos opcionales; el
  dashboard se actualiza cada 15 min;
- modo esencial desde el 90%: solo lo esencial (pick final, resultados y
  cobro); los cierres solo cuando hay partidos en las próximas 2 h.
Así se frena solo antes del 100%.

Neon cuenta el mes de calendario en UTC (la consola: "Usage since Nov 1").
Lo que el medidor no ve (TLS, TCP, consultas a mano) va en WIRE_FACTOR, y la
base se cuenta siempre del tamaño fijo CU_SIZE (Neon → Computes: 0.25).

Con el plan de pago (model_state 'db_budget_plan', Launch del 11-nov al
1-dic-26) no hay freno, pero se estima la cuenta del mes (paid_bill) y se
avisa por Telegram si va a llegar al tope del dueño; al terminar el periodo
pagado se le recuerda bajar al plan gratis (downgrade_message). La alerta de
$15 de Neon es un correo que no se puede probar: este aviso sí tiene pruebas.
"""

from datetime import datetime, timezone

FREE_TRANSFER_GB = 5.0
FREE_CU_HOURS = 100.0
CU_SIZE = 0.25
WIRE_FACTOR = 1.15
RATE_DAYS = 3
MIN_MEASURED_H = 24         # menos medición que esto: no opina
# Lo que corre una vez por semana se reparte en 7 días: con 3 días de ritmo, el
# weekly y el respaldo contaban como si pasaran cada día (el 11-nov, recién
# medido, el pronóstico salía del 400% por el weekly y la primera copia).
WEEKLY_MODES = ("weekly", "respaldo", "proc:weekly_sanity_audit", "proc:clv_audit")
ALERT_PCTS = (50, 75)
AHORRO_PCT = 75
ESENCIAL_PCT = 90
LEVELS = ("normal", "ahorro", "esencial")
KICKOFF_SOON_H = 2
# El plan de pago (Launch; consola de Neon → Billing, 11-nov-26). El tope es el
# de la alerta que el dueño puso en Neon ("agregué la alerta de 15 dólares como
# tope"); otro tope: db_budget_plan → "cap_usd".
PAID_CU_HOUR_USD = 0.106
PAID_GB_MONTH_USD = 0.55      # 0.35 de almacenamiento + 0.20 de historia (instant restore)
PAID_CAP_USD = 15.0
PAID_WARN_SHARE = 0.8         # avisa antes del tope: el medidor no ve las consultas hechas a mano
DOWNGRADE_GRACE_H = 12        # el recordatorio sale con el mes de Neon (UTC) ya cerrado
DOWNGRADE_REMIND_DAYS = 14

USAGE_SQL = """
    SELECT MIN(ran_at)                     FILTER (WHERE ran_at >= :start)                        AS measured_since,
           COALESCE(SUM(bytes_read)        FILTER (WHERE ran_at >= :start), 0)                    AS bytes,
           COALESCE(SUM(awake_s)           FILTER (WHERE ran_at >= :start), 0)                    AS awake_s,
           MIN(ran_at)                     FILTER (WHERE ran_at >= :rate_from)                    AS rate_since,
           COALESCE(SUM(bytes_read) FILTER (WHERE ran_at >= :rate_from AND mode <> ALL(:weekly)), 0) AS rate_bytes,
           COALESCE(SUM(awake_s)    FILTER (WHERE ran_at >= :rate_from AND mode <> ALL(:weekly)), 0) AS rate_awake_s,
           COALESCE(SUM(bytes_read) FILTER (WHERE ran_at >= :week_from AND mode = ANY(:weekly)), 0)  AS week_bytes,
           COALESCE(SUM(awake_s)    FILTER (WHERE ran_at >= :week_from AND mode = ANY(:weekly)), 0)  AS week_awake_s,
           MIN(ran_at)                     FILTER (WHERE ran_at >= :paid_from)                    AS paid_measured_since,
           COALESCE(SUM(awake_s)           FILTER (WHERE ran_at >= :paid_from), 0)                AS paid_awake_s
    FROM pipeline_runs
    WHERE ran_at >= LEAST(CAST(:start AS timestamptz), CAST(:week_from AS timestamptz)) AND bytes_read IS NOT NULL
"""


def month_bounds(now: datetime) -> tuple:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def _ts(v):
    import pandas as pd
    if v is None or (isinstance(v, float) and v != v):
        return None
    t = pd.Timestamp(v)
    if pd.isna(t):
        return None
    return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).to_pydatetime()


def project(usage: dict, now: datetime) -> dict:
    """Función pura. usage: la fila de USAGE_SQL. Lo gastado, el pronóstico al
    fin de mes y el nivel. Si la medición empezó a medio mes, lo de antes se
    estima con el mismo ritmo (del lado alto)."""
    start, end = month_bounds(now)
    month_days = (end - start).total_seconds() / 86400
    since, rate_since = _ts(usage.get("measured_since")), _ts(usage.get("rate_since"))
    out = {"level": "normal", "measured": False, "month": start.strftime("%Y-%m")}
    if since is None or rate_since is None or (now - rate_since).total_seconds() < MIN_MEASURED_H * 3600:
        return out
    rate_days = (now - rate_since).total_seconds() / 86400
    week_b, week_s = float(usage.get("week_bytes") or 0), float(usage.get("week_awake_s") or 0)
    gb_day = (float(usage["rate_bytes"]) / rate_days + week_b / 7) * WIRE_FACTOR / 1e9
    cuh_day = (float(usage["rate_awake_s"]) / rate_days + week_s / 7) / 3600 * CU_SIZE
    unmeasured_days = max(0.0, (since - start).total_seconds() / 86400)
    used_gb = float(usage["bytes"]) * WIRE_FACTOR / 1e9 + gb_day * unmeasured_days
    used_cuh = float(usage["awake_s"]) / 3600 * CU_SIZE + cuh_day * unmeasured_days
    left = max(0.0, (end - now).total_seconds() / 86400)
    proj_gb, proj_cuh = used_gb + gb_day * left, used_cuh + cuh_day * left
    pct = {"transfer": 100 * proj_gb / FREE_TRANSFER_GB, "compute": 100 * proj_cuh / FREE_CU_HOURS}
    used_pct = {"transfer": 100 * used_gb / FREE_TRANSFER_GB, "compute": 100 * used_cuh / FREE_CU_HOURS}
    worst = max(pct, key=pct.get)
    top = max(max(pct.values()), max(used_pct.values()))
    level = "esencial" if top >= ESENCIAL_PCT else "ahorro" if top >= AHORRO_PCT else "normal"
    out.update(measured=True, level=level, worst=worst, pct=round(pct[worst], 1),
               used_gb=round(used_gb, 2), proj_gb=round(proj_gb, 2), gb_day=round(gb_day, 3),
               used_cuh=round(used_cuh, 1), proj_cuh=round(proj_cuh, 1), cuh_day=round(cuh_day, 2),
               pct_transfer=round(pct["transfer"], 1), pct_compute=round(pct["compute"], 1),
               estimated_days=round(unmeasured_days, 1), month_days=round(month_days))
    return out


def status(engine=None, now: datetime | None = None) -> dict:
    """project() con lo que hay en pipeline_runs. Sin base o sin la columna:
    nivel normal (que el freno nunca detenga el programa por sí mismo)."""
    from datetime import timedelta
    from sqlalchemy import text
    now = now or datetime.now(timezone.utc)
    start, _ = month_bounds(now)
    try:
        if engine is None:
            from config.database import engine
        with engine.connect() as conn:
            plan = _plan(conn.execute(text("SELECT value FROM model_state WHERE key = 'db_budget_plan'")).scalar())
            row = conn.execute(text(USAGE_SQL), {"start": start, "rate_from": now - timedelta(days=RATE_DAYS),
                                                 "week_from": now - timedelta(days=7), "weekly": list(WEEKLY_MODES),
                                                 "paid_from": paid_from(plan, now)}).mappings().first()
            usage = dict(row) if row else {}
            until = _ts(plan.get("paid_until"))
            if until is not None and now < until:          # solo con el plan de pago hace falta el tamaño
                usage["storage_bytes"] = conn.execute(text("SELECT pg_database_size(current_database())")).scalar()
        return paid_plan(project(usage, now), plan, now, usage)
    except Exception as e:
        return {"level": "normal", "measured": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def _plan(plan) -> dict:
    import json
    if isinstance(plan, str):
        plan = json.loads(plan)
    return plan if isinstance(plan, dict) else {}


def paid_from(plan, now: datetime) -> datetime:
    """Desde cuándo se cobra este mes: el inicio del plan de pago ("since") o el
    día 1 (Neon factura por mes de calendario)."""
    start, _ = month_bounds(now)
    since = _ts(_plan(plan).get("since"))
    return max(since, start) if since is not None else start


def paid_bill(st: dict, plan, now: datetime, usage: dict | None) -> dict:
    """Función pura: la cuenta estimada del plan de pago en este mes, hasta hoy
    y al terminar el periodo (el día que vence o el último del mes). Horas
    encendida × CU_SIZE × precio, más el almacenamiento. Lo que no se midió
    desde que empezó el plan se estima con el ritmo (del lado alto)."""
    plan, usage = _plan(plan), usage or {}
    until = _ts(plan.get("paid_until"))
    if until is None or not st.get("measured"):
        return {}
    start, end = month_bounds(now)
    since, period_end = paid_from(plan, now), min(until, end)
    month_days = (end - start).total_seconds() / 86400
    measured = _ts(usage.get("paid_measured_since")) or now
    gap_days = max(0.0, (min(measured, now) - since).total_seconds() / 86400)
    cuh = float(usage.get("paid_awake_s") or 0) / 3600 * CU_SIZE + st["cuh_day"] * gap_days
    left = max(0.0, (period_end - now).total_seconds() / 86400)
    gb = float(usage.get("storage_bytes") or 0) / 1e9
    storage = (lambda to: gb * PAID_GB_MONTH_USD * max(0.0, (to - since).total_seconds() / 86400) / month_days)
    cap = float(plan.get("cap_usd") or PAID_CAP_USD)
    return {"bill_usd": round(cuh * PAID_CU_HOUR_USD + storage(now), 2),
            "bill_proj_usd": round((cuh + st["cuh_day"] * left) * PAID_CU_HOUR_USD + storage(period_end), 2),
            "bill_cap_usd": cap, "bill_until": period_end.date().isoformat()}


def paid_plan(st: dict, plan, now: datetime, usage: dict | None = None) -> dict:
    """model_state 'db_budget_plan' = {"paid_until": "2026-12-01"}: con el plan
    de pago (Launch del 11-nov al 1-dic-26) se mide igual, pero el freno no
    actúa (el pronóstico sigue diciendo si cabría en el gratis) y se estima la
    cuenta (paid_bill). Vencido el periodo, toca recordar la bajada al gratis
    (downgrade_due) durante DOWNGRADE_REMIND_DAYS."""
    from datetime import timedelta
    plan = _plan(plan)
    until = _ts(plan.get("paid_until"))
    if until is None:
        return st
    if now < until:
        return {**st, "free_level": st.get("level"), "level": "normal", "paid_until": until.date().isoformat(),
                **paid_bill(st, plan, now, usage)}
    if until + timedelta(hours=DOWNGRADE_GRACE_H) <= now < until + timedelta(days=DOWNGRADE_REMIND_DAYS):
        st = {**st, "downgrade_due": until.date().isoformat(), "paid_plan_name": str(plan.get("plan") or "de pago")}
    return st


def bill_stage(st: dict) -> int:
    """0 = la cuenta va bien; 1 = a este ritmo llega cerca del tope; 2 = ya lo pasó."""
    if st.get("bill_usd") is None:
        return 0
    if st["bill_usd"] >= st["bill_cap_usd"]:
        return 2
    return 1 if st["bill_proj_usd"] >= PAID_WARN_SHARE * st["bill_cap_usd"] else 0


def bill_message(st: dict) -> str:
    """Telegram para el dueño cuando la cuenta del plan de pago se acerca al tope o lo pasa."""
    cap, usd, proj = st["bill_cap_usd"], st["bill_usd"], st["bill_proj_usd"]
    hours = st.get("cuh_day", 0) / CU_SIZE
    if bill_stage(st) == 2:
        head = (f"💳 <b>Neon: la cuenta del mes ya pasó de ${cap:.0f}</b>\nVan unos ${usd:.2f} (estimado con lo "
                f"que mide el programa) y el tope de tu alerta de Neon es ${cap:.0f}.")
    else:
        head = (f"💳 <b>Neon: la cuenta del mes va para ${proj:.0f}</b>\nHasta hoy van unos ${usd:.2f}; a este "
                f"ritmo, el {st['bill_until']} serían ${proj:.2f}. El tope de tu alerta de Neon es ${cap:.0f}.")
    return (head + f"\nLo que se cobra son las horas que la base pasa encendida: ahora, unas {hours:.0f} al día. "
            "La cifra exacta está en Neon → Billing.")


def downgrade_message(st: dict) -> str:
    """Telegram el día que vence el periodo pagado: el programa no puede saber
    si el dueño ya bajó el plan, y un mes olvidado se cobra entero."""
    return (f"📌 <b>Hoy toca bajar Neon al plan gratis</b>\nEl periodo que pagaste del plan "
            f"{st.get('paid_plan_name', 'de pago')} terminó el {st['downgrade_due']}. Para que no se cobre otro "
            "mes: Neon → Billing → Change plan → Free. Antes revisa que la base siga fija en 0.25 CU "
            "(Neon → Computes).\nSi ya lo bajaste, no hagas caso de este mensaje. Desde hoy el programa cuida solo "
            "el plan gratis: avisa al 50% y al 75% del mes y se frena antes de llegar al límite.")


_CACHE: dict = {}


def level(engine=None) -> str:
    """El nivel del proceso: se calcula una vez (las corridas duran minutos)."""
    if "status" not in _CACHE:
        _CACHE["status"] = status(engine)
    return _CACHE["status"]["level"]


def describe(st: dict) -> str:
    """Una línea para el log y los mensajes."""
    if not st.get("measured"):
        return "Plan gratis de Neon: todavía sin medición suficiente"
    est = f" (los primeros {st['estimated_days']:.0f} días estimados)" if st.get("estimated_days", 0) >= 1 else ""
    if st.get("paid_until"):
        est += (f" [plan de pago hasta el {st['paid_until']}: el freno no actúa; en el gratis sería modo "
                f"{st.get('free_level')}]")
        if st.get("bill_usd") is not None:
            est += (f" [cuenta estimada: ${st['bill_usd']:.2f} hasta hoy, ${st['bill_proj_usd']:.2f} al "
                    f"{st['bill_until']}; tope ${st['bill_cap_usd']:.0f}]")
    return (f"Plan gratis de Neon este mes{est}: transferencia {st['used_gb']:.2f} de {FREE_TRANSFER_GB:.0f} GB, "
            f"a este ritmo {st['proj_gb']:.2f} GB ({st['pct_transfer']:.0f}%); base encendida "
            f"{st['used_cuh']:.0f} de {FREE_CU_HOURS:.0f} h, a este ritmo {st['proj_cuh']:.0f} h "
            f"({st['pct_compute']:.0f}%)")


_WHAT = {"transfer": "la transferencia (lo que el programa descarga de la base)",
         "compute": "las horas que la base pasa encendida"}


def level_message(old: str, new: str, st: dict) -> str:
    """Telegram cuando cambia el nivel (texto para el dueño)."""
    what = _WHAT.get(st.get("worst"), "el plan")
    if new == "ahorro":
        head = (f"💡 <b>Modo ahorro de la base</b>\nA este ritmo, el mes llegaría al {st['pct']:.0f}% de {what} "
                f"del plan gratis de Neon.\nDesde ahora los cierres corren cada hora (cada 30 min solo si hay "
                f"partidos en las próximas {KICKOFF_SOON_H} horas), se saltan los pasos opcionales y el "
                f"dashboard se actualiza cada 15 min. Se quita solo cuando el ritmo baje.")
    elif new == "esencial":
        head = (f"⚠️ <b>Modo esencial de la base</b>\nEl mes va al {st['pct']:.0f}% de {what} del plan gratis "
                f"de Neon. Para no llegar al límite, solo corre lo esencial: el pick final antes de cada "
                f"partido, los resultados y el cobro. Se quita solo cuando el ritmo baje.")
    else:
        head = ("✅ <b>La base volvió a su ritmo normal</b>\nEl pronóstico del mes bajó del "
                f"{AHORRO_PCT}% del plan gratis de Neon: todo corre como siempre.")
    return head + "\n\n<i>" + describe(st) + "</i>"


def threshold_crossed(st: dict, alerted: dict | None) -> int | None:
    """El aviso (50 o 75) que toca mandar ahora, una vez por mes."""
    if not st.get("measured"):
        return None
    done = (alerted or {}).get(st["month"], 0)
    due = [p for p in ALERT_PCTS if st["pct"] >= p and p > done]
    return max(due) if due else None


def threshold_message(pct: int, st: dict) -> str:
    what = _WHAT.get(st.get("worst"), "el plan")
    nxt = ("Si llega al 75%, entra solo el modo ahorro." if pct < AHORRO_PCT
           else "El modo ahorro ya está activo para no llegar al límite.")
    return (f"📊 <b>Base de datos: {pct}% del plan gratis</b>\nA este ritmo el mes llegaría al {st['pct']:.0f}% "
            f"de {what}. {nxt}\n\n<i>{describe(st)}</i>")
