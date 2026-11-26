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

26-nov-26, el dueño eligió (3) para la regla de 4+ titulares fuera: se queda
y se mide. Desde aquí el bloque dice también cuántos 🎯 quitó esa regla y
cómo habría salido el 🎯 de antes (removed_star), y cuántos picks para seguir
cambiaron en partidos con la marca de las bajas que pesan (lineup_mark: los
que meten un tercio de los goles o la defensa cambiada, que no quita el 🎯).
"""

import math

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

SETTLED = ("win", "loss", "push", "half_win", "half_loss")
WEAK_REASON = "Alineación:"      # el motivo de la regla de 4+ fuera (program_picks.WEAKENED)
MIN_N = 20                   # cambios con algún 🎯 para dar veredicto
Z_VERDICT = 2.0

_SQL = """
    SELECT match, display, match_date, label, market, reason, playdoit_odds, result, profit,
           pre_lineup_label, pre_lineup_market, pre_lineup_star, pre_lineup_odds, pre_lineup_at,
           pre_lineup_result, pre_lineup_profit, lineup_mark
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
                        "star_before": star_before, "star_after": star_after, "ub": ub, "ua": ua,
                        "weak_rule": star_before and not star_after
                        and str(_txt(r.get("reason")) or "").startswith(WEAK_REASON),
                        "marked": bool(_txt(r.get("lineup_mark")))})
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
        # la regla de 4+ fuera (26-nov-26: se queda y se mide): los 🎯 que quitó y cómo salía el de antes
        "removed_star": sum(1 for c in changed if c["weak_rule"]),
        "removed_won": sum(1 for c in changed if c["weak_rule"] and c["before_win"]),
        "removed_units": sum(c["ub"] for c in changed if c["weak_rule"] and c["ub"] is not None),
        # la marca de las bajas que pesan: cambia solo el pick para seguir (el 🎯 se queda)
        "marked_changed": sum(1 for c in changed if c["marked"] and not c["weak_rule"]
                              and c["star_before"] == c["star_after"]),
        "marked_before": sum(1 for c in changed if c["marked"] and not c["weak_rule"]
                             and c["star_before"] == c["star_after"] and c["before_win"]),
        "marked_after": sum(1 for c in changed if c["marked"] and not c["weak_rule"]
                            and c["star_before"] == c["star_after"] and c["after_win"]),
    }


def _u(x: float) -> str:
    """La ganancia a 1 unidad dicha apostando 50 (6-nov-26: el dueño no entendía "u"; 50 desde el 8-nov)."""
    from src.models.telegram_reports import money
    return money(x)


def plain_verdict(total: dict) -> str:
    """¿Conviene cambiar el pick con la alineación?, en palabras normales."""
    z, n = total.get("z"), int(total.get("n_star") or 0)
    if n < MIN_N:
        return f"todavía no se sabe: van {n} de {MIN_N} cambios en 🎯"
    if z is not None and z >= Z_VERDICT:
        return "sí, los cambios ayudan"
    if z is not None and z <= -Z_VERDICT:
        return "no, los cambios estorban"
    return "no se nota diferencia"


def format_lines(week: dict, total: dict, since) -> list:
    """El bloque del reporte semanal (HTML de Telegram), en palabras normales
    (6-nov-26); la ganancia, apostando 50 a cada uno."""
    lines = ["— <b>CAMBIOS CON LA ALINEACIÓN</b> —"]
    if week["changed"]:
        lines += [f"Esta semana se revisaron {week['seen']} picks con la alineación y {week['changed']} cambiaron.",
                  f"  De los que cambiaron, el pick de antes ganó {week['hits_before']} y el nuevo ganó "
                  f"{week['hits_after']}.",
                  f"  En los 🎯: con el pick de antes, {week['bets_before']} apuestas y {_u(week['units_before'])};"
                  f" con el nuevo, {week['bets_after']} apuestas y {_u(week['units_after'])}."]
    else:
        lines.append(f"Esta semana se revisaron {week['seen']} picks con la alineación y ninguno cambió.")
    lines.append(f"Desde el {since:%d-%m}: {total['changed']} cambios · en los 🎯, con el pick de antes "
                 f"{_u(total['units_before'])} y con el nuevo {_u(total['units_after'])}"
                 + (f" ({total['unknown']} sin momio guardado no cuentan en la ganancia)" if total["unknown"] else ""))
    lines += rule_lines(total)
    lines += [f"¿Conviene cambiar el pick con la alineación? {plain_verdict(total)}", ""]
    return lines


def rule_lines(total: dict) -> list:
    """La regla de 4+ fuera y la marca de las bajas que pesan, desde que se miden (26-nov-26)."""
    out = []
    n = int(total.get("removed_star") or 0)
    if n:
        out.append(f"  La regla de 4 o más titulares fuera quitó {n} 🎯: el 🎯 de antes habría ganado "
                   f"{total['removed_won']} de {n} ({_u(total['removed_units'])}).")
    m = int(total.get("marked_changed") or 0)
    if m:
        many = m != 1
        out.append(f"  Con la marca de bajas que pesan {'cambiaron' if many else 'cambió'} {m} "
                   f"{'picks' if many else 'pick'} para seguir: el de antes ganó {total['marked_before']} y el "
                   f"nuevo {total['marked_after']}.")
    return out


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
