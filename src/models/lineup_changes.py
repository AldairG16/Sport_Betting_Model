"""
src/models/lineup_changes.py
============================
¿Los cambios de pick con la alineación ayudan o estorban? (5-nov-26)

El dueño: "mide si los cambios por alineación ayudan o estorban". El closing
rehace el pick cuando ESPN publica la alineación (~1 h antes). Medido a mano
el 4-nov con lo que había (solo el último cambio de cada partido, sin el
momio del pick de antes en 1.5/3.5, córners y tarjetas): 34 cambios, en
aciertos empate (15 → 17) y en los 🎯 −5.4u → +2.0u, pocos datos para
decidir. Desde v2.8.0 program_picks guarda el pick de antes de la alineación
(pre_lineup_*, con su momio de ese momento) y resolve_picks lo liquida igual
que el pick: esto los compara y sale cada semana en el reporte de Telegram.

Lo que cuenta es lo que se apuesta: los 🎯. Por cada partido que cambió, lo
que habría dejado el 🎯 de antes contra lo que deja el de después (sin 🎯 =
no se apuesta = 0). El veredicto pide MIN_N cambios con algún 🎯 y una
diferencia de Z_VERDICT desviaciones; mientras no, dice cuántos faltan.
Solo mide: no cambia nada del pick.
"""

import math

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

SETTLED = ("win", "loss", "push", "half_win", "half_loss")
MIN_N = 20                   # cambios con algún 🎯 para dar veredicto
Z_VERDICT = 2.0

_SQL = """
    SELECT match, display, match_date, label, market, reason, playdoit_odds, result, profit,
           pre_lineup_label, pre_lineup_market, pre_lineup_star, pre_lineup_odds, pre_lineup_at,
           pre_lineup_result, pre_lineup_profit
    FROM program_picks
    WHERE pre_lineup_at IS NOT NULL AND match_date < NOW()
"""


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _bet_units(star, market, result, profit):
    """Lo que dejó apostar ese pick a 1u: 0 si no era 🎯 (no se apuesta),
    None si no se sabe (sin liquidar o ganado sin momio guardado)."""
    if not star or not isinstance(market, str):
        return 0.0
    if result not in SETTLED:
        return None
    p = _num(profit)
    if p is not None:
        return p
    return -1.0 if result == "loss" else (0.0 if result == "push" else None)


def _settled(market, result) -> bool:
    return not isinstance(market, str) or result in SETTLED       # sin pick = nada que liquidar


def _txt(v):
    return v if isinstance(v, str) else None


def _flag(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and bool(v)


def summarize(rows: list) -> dict:
    """Función pura. rows: program_picks con pre_lineup_at (el pick vio la
    alineación). Solo cuentan los partidos con los dos picks liquidados."""
    seen, changed, unknown = 0, [], 0
    for r in rows:
        if not (_settled(r.get("pre_lineup_market"), r.get("pre_lineup_result"))
                and _settled(r.get("market"), r.get("result"))):
            continue
        seen += 1
        star_after = isinstance(r.get("market"), str) and _txt(r.get("reason")) is None    # 🎯 = pasa la regla
        star_before = _flag(r.get("pre_lineup_star")) and isinstance(r.get("pre_lineup_market"), str)
        if _txt(r.get("pre_lineup_label")) == _txt(r.get("label")) and star_before == star_after:
            continue
        ub = _bet_units(star_before, r.get("pre_lineup_market"), r.get("pre_lineup_result"), r.get("pre_lineup_profit"))
        ua = _bet_units(star_after, r.get("market"), r.get("result"), r.get("profit"))
        if ub is None or ua is None:
            unknown += 1
        changed.append({"before_win": r.get("pre_lineup_result") == "win", "after_win": r.get("result") == "win",
                        "star_before": star_before, "star_after": star_after, "ub": ub, "ua": ua})
    known = [c for c in changed if c["ub"] is not None and c["ua"] is not None]
    rel = [c["ua"] - c["ub"] for c in known if c["star_before"] or c["star_after"]]
    n = len(rel)
    z = None
    if n >= 2:
        mean = sum(rel) / n
        sd = math.sqrt(sum((d - mean) ** 2 for d in rel) / (n - 1))
        z = mean / (sd / math.sqrt(n)) if sd > 0 else None
    if n < MIN_N:
        verdict = f"todavía pocos datos ({n} de {MIN_N} cambios con 🎯)"
    elif z is not None and z >= Z_VERDICT:
        verdict = f"AYUDAN ({z:+.1f} desviaciones)"
    elif z is not None and z <= -Z_VERDICT:
        verdict = f"ESTORBAN ({z:+.1f} desviaciones)"
    else:
        verdict = f"sin diferencia clara ({z:+.1f} desviaciones)" if z is not None else "sin diferencia clara"
    return {
        "seen": seen, "changed": len(changed), "unknown": unknown,
        "hits_before": sum(c["before_win"] for c in changed), "hits_after": sum(c["after_win"] for c in changed),
        "helped": sum(1 for c in changed if c["after_win"] and not c["before_win"]),
        "hurt": sum(1 for c in changed if c["before_win"] and not c["after_win"]),
        "bets_before": sum(1 for c in known if c["star_before"]), "units_before": sum(c["ub"] for c in known),
        "bets_after": sum(1 for c in known if c["star_after"]), "units_after": sum(c["ua"] for c in known),
        "n_star": n, "z": z, "verdict": verdict,
    }


def _u(x: float) -> str:
    return f"{x:+.2f}u"


def format_lines(week: dict, total: dict, since) -> list:
    """El bloque del reporte semanal (HTML de Telegram)."""
    lines = ["— <b>CAMBIOS CON LA ALINEACIÓN</b> —"]
    if week["changed"]:
        lines += [f"Semana: {week['seen']} picks vieron la alineación, {week['changed']} cambiaron",
                  f"  Aciertos de los que cambiaron: antes {week['hits_before']} → después {week['hits_after']}",
                  f"  🎯 (lo que se apuesta): antes {week['bets_before']} apuestas {_u(week['units_before'])}"
                  f" → después {week['bets_after']} apuestas {_u(week['units_after'])}"]
    else:
        lines.append(f"Semana: {week['seen']} picks vieron la alineación, ninguno cambió")
    lines.append(f"Desde el {since:%d-%m}: {total['changed']} cambios · 🎯 antes {_u(total['units_before'])}"
                 f" → después {_u(total['units_after'])}"
                 + (f" ({total['unknown']} ganados sin momio guardado, fuera de las unidades)" if total["unknown"] else ""))
    lines += [f"Veredicto: {total['verdict']}", ""]
    return lines


def weekly_lines(engine=None, days: int = 7, now=None) -> list:
    """Bloque del reporte semanal; vacío si todavía no hay picks que hayan
    visto la alineación con el pick de antes guardado."""
    if engine is None:
        from config.database import engine
    try:
        df = pd.read_sql(text(_SQL), engine)
    except Exception as e:
        if "does not exist" not in str(e):        # las columnas nacen con el primer closing de v2.8.0
            log.error(f"❌ Cambios con la alineación: {type(e).__name__}: {str(e)[:160]}")
        return []
    if df.empty or not {"match_date", "pre_lineup_at", "pre_lineup_result", "result"} <= set(df.columns):
        return []
    try:                                          # el bloque nunca tumba el reporte semanal
        now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        when = pd.to_datetime(df["match_date"], utc=True)
        total = summarize(df.to_dict("records"))
        week = summarize(df[when >= now - pd.Timedelta(days=days)].to_dict("records"))
        if not total["seen"]:
            return []
        from config.settings import USER_TIMEZONE
        since = pd.to_datetime(df["pre_lineup_at"], utc=True).min().tz_convert(USER_TIMEZONE)
        lines = format_lines(week, total, since)
    except Exception as e:
        log.error(f"❌ Cambios con la alineación: {type(e).__name__}: {str(e)[:160]}")
        return []
    for x in lines[1:-1]:
        print("🔁 " + x)
    return lines
