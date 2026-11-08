"""
src/models/telegram_reports.py
==============================
Los mensajes del día en Telegram, en palabras normales (6-nov-26).

El dueño: "necesito que el texto … sea más amigable" y, del resumen de la
noche, "cámbialo también". Además los tres mensajes leían `bets_history`, que
dejó de llenarse con el modo colección (25-sep-26): la mañana decía cada día
"No apuestes nada: el sistema sigue recolectando datos en sombra", la noche
"Sin apuestas para hoy" y el semanal "Sin apuestas jugadas esta semana",
aunque el dashboard y "Partidos de hoy" sí recomendaban 🎯 y parlays.

Ahora leen lo mismo que el dashboard: los picks del programa
(`program_picks`; 🎯 = el que no trae `reason`) y los parlays con bono
(`program_parlays`, armados con `dashboard.parlay`). La ganancia se dice
"apostando 100 a cada uno" (el pick a 1 unidad al momio de PlayDoit, × 100)
y los momios en americano.

- morning_text: los 🎯 de hoy con su momio mínimo, los parlays y cuántos
  partidos más hay sin 🎯 (solo para seguirlos).
- evening_text: cómo terminó cada 🎯, los parlays, los demás picks en una
  línea, lo acumulado desde el 22-oct y cuántos 🎯 hay mañana.
- weekly_lines: la semana de 🎯, parlays y demás picks, por liga y lo
  acumulado.

Las funciones de formato son puras; load_* hacen las consultas.
"""

import html
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import text

FIXED_SINCE = pd.Timestamp("2026-10-22", tz="UTC")     # = dashboard.board.MODEL_FIXED
PLAYED = ("win", "loss", "push", "half_win", "half_loss")
WIN_LIKE = ("win", "half_win")
LOSS_LIKE = ("loss", "half_loss")
LAG_FAMILIES = ("Córners", "Tarjetas")                 # su dato oficial llega 1-2 días después
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

PICKS_SQL = """
    SELECT match, display, match_date, league, label, family, market, reason, min_us, playdoit_odds,
           result, profit, pa_paid
    FROM program_picks
    WHERE match_date >= :a AND match_date < :b AND market IS NOT NULL
      AND result IS DISTINCT FROM 'stale'          -- el horario viejo de un reprogramado
    ORDER BY match_date, display
"""
PARLAYS_SQL = "SELECT * FROM program_parlays WHERE rule = 'bono' AND day >= :d0 AND day <= :d1"


# ─── piezas ──────────────────────────────────────────────────────────────────

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _str(v) -> str | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else (str(v).strip() or None)


def _ts(v) -> pd.Timestamp | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def day_name(day) -> str:
    """'viernes 7 nov'."""
    d = pd.Timestamp(day)
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


def day_bounds(day, tz) -> tuple[datetime, datetime]:
    """El día de México en UTC: [inicio, fin)."""
    d = pd.Timestamp(day)
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def money(units) -> str:
    """La ganancia a 1 unidad dicha apostando 100: 0.45 → '+45', -1 → '-100'."""
    v = round((_num(units) or 0.0) * 100)
    return f"+{v}" if v > 0 else str(v)


def us(dec) -> str | None:
    """Momio americano del momio decimal de PlayDoit."""
    from src.utils.min_odds import fmt_american, to_american
    d = _num(dec)
    return fmt_american(to_american(d)) if d and d > 1 else None


def is_star(r: dict) -> bool:
    return bool(_str(r.get("market"))) and not _str(r.get("reason"))


def _teams(r: dict) -> tuple[str, str]:
    from dashboard.display import team_name
    shown = _str(r.get("display"))
    if shown and " vs " in shown:
        h, a = shown.split(" vs ", 1)
        return h, a
    h, _, a = str(r.get("match") or "").partition(" vs ")
    return team_name(h), team_name(a)


def match_name(r: dict) -> str:
    h, a = _teams(r)
    return f"{h} vs {a}"


def pick_label(r: dict) -> str:
    """'Gana RB Leipzig' en vez de 'Gana el local'; lo demás como viene."""
    h, a = _teams(r)
    m = str(r.get("market") or "")
    if m == "home_win":
        return f"Gana {h}"
    if m == "away_win":
        return f"Gana {a}"
    return _str(r.get("label")) or m


def record(rows: list) -> dict:
    """Ganados, perdidos y anulados de lo ya liquidado; la ganancia solo de los
    que tienen momio de PlayDoit (como el dashboard)."""
    done = [r for r in rows if _str(r.get("result")) in PLAYED]
    won = sum(1 for r in done if r["result"] in WIN_LIKE)
    lost = sum(1 for r in done if r["result"] in LOSS_LIKE)
    priced = [_num(r.get("profit")) for r in done if _num(r.get("profit")) is not None]
    return {"n": len(done), "won": won, "lost": lost, "void": len(done) - won - lost,
            "units": round(sum(priced), 4), "priced": len(priced)}


def record_text(rec: dict) -> str:
    """'2 ganados y 1 perdido · apostando 100 a cada uno: +45'."""
    if not rec["n"]:
        return "todavía no termina ninguno"
    parts = [f"{rec['won']} ganado{'s' if rec['won'] != 1 else ''}",
             f"{rec['lost']} perdido{'s' if rec['lost'] != 1 else ''}"]
    if rec["void"]:
        parts.append(f"{rec['void']} anulado{'s' if rec['void'] != 1 else ''}")
    s = ", ".join(parts[:-1]) + " y " + parts[-1]
    if rec["priced"]:
        s += f" · apostando 100 a cada uno: <b>{money(rec['units'])}</b>"
    return s


def times_text(p) -> str | None:
    from src.models.parlay_alerts import times_text as _t
    p = _num(p)
    return _t(p) if p else None


def parlay_name(d: dict, tz) -> str:
    slot = _ts(d["slot"]).astimezone(tz)
    seq = int(d.get("seq") or 1)
    if d.get("kind") == "dia":
        return "Parlay del día" + (f" #{seq}" if seq > 1 else "")
    return f"Parlay de las {slot:%H:%M}"


def parlays_of(rows: list) -> list:
    """Los parlays con bono armados como en la pestaña (dashboard.parlay),
    del más temprano al más tarde. Sin marcadores en vivo: solo lo liquidado."""
    from dashboard.parlay import parlay_board
    if not rows:
        return []
    board = parlay_board(pd.DataFrame(rows), None)
    return sorted(board["parlays"], key=lambda d: (d["slot"], 0 if d["kind"] == "dia" else 1, d["seq"]))


# ─── el estado de un pick ya empezado ────────────────────────────────────────

def pick_status(r: dict, now: datetime) -> tuple[str, str]:
    """(ícono, texto) de cómo terminó o en qué va un pick."""
    res = _str(r.get("result"))
    if res == "win":
        paid = r.get("pa_paid") is True or str(r.get("pa_paid")).lower() == "true"
        return "✅", "ganado" + (" ⚡ (cobrado por ir 2 goles arriba)" if paid else "")
    if res == "half_win":
        return "✅", "medio ganado"
    if res == "loss":
        return "❌", "perdido"
    if res == "half_loss":
        return "❌", "medio perdido"
    if res == "push":
        return "↔️", "anulado: se devuelve lo apostado"
    kick = _ts(r.get("match_date"))
    mins = (now - kick).total_seconds() / 60 if kick is not None else 0
    if mins < 0:
        return "🕒", "todavía no empieza"
    if mins < 120:
        return "🕒", "se está jugando"
    if r.get("family") in LAG_FAMILIES:
        return "⏳", f"esperando el dato oficial de {str(r['family']).lower()} (llega en 1 o 2 días)"
    return "⏳", "esperando el resultado oficial"


# ─── mañana ──────────────────────────────────────────────────────────────────

def morning_text(day, picks: list, parlay_rows: list, tz, when: str = "Hoy") -> tuple[str, int]:
    """El mensaje de la mañana (o el avance de mañana, when="Mañana") y
    cuántos 🎯 lleva."""
    from src.models.early_payout import applies
    from src.utils.min_odds import fmt_american
    stars = [r for r in picks if is_star(r)]
    others = len({r["match"] for r in picks if not is_star(r)})
    hoy = when.lower()
    lines = [f"{'☀️' if when == 'Hoy' else '🌙'} <b>{when}, {day_name(day)}</b>", ""]
    if stars:
        lines.append(f"🎯 <b>Recomendados</b> ({len(stars)})")
        for r in stars:
            ko = _ts(r["match_date"]).astimezone(tz)
            mn = _num(r.get("min_us"))
            price = (f"apuesta si PlayDoit paga <b>{fmt_american(int(mn))}</b> o más" if mn is not None
                     else f"{us(r.get('playdoit_odds')) or 'sin momio'} en PlayDoit")
            pa = " ⚡" if applies(r.get("league"), r.get("market")) else ""
            lines.append(f"<b>{ko:%H:%M}</b> {html.escape(match_name(r))}\n"
                         f"   {html.escape(pick_label(r))}{pa} · {price}")
        lines.append("")
    else:
        lines += [f"🎯 {when} no hay recomendados: ningún equipo muy favorito paga bien en una liga con "
                  "pago anticipado. No hace falta apostar.", ""]
    pls = [d for d in parlays_of(parlay_rows) if d["parlay"]]
    if pls:
        lines.append("🧩 <b>Parlays</b>")
        for d in pls:
            p = d["parlay"]
            pay = f"paga <b>{p['odds_us']}</b>" + (f" (con el bono: <b>{p['odds_bonus_us']}</b>)" if p["bonus"] else "")
            odds = times_text(p.get("p_market"))
            lines.append(f"{parlay_name(d, tz)} · {p['k']} partidos · {pay}"
                         + (f"\n   se gana {odds}" if odds else ""))
        lines += ["<i>El parlay final llega con los picks finales, en la última revisión antes de su primer "
                  "partido.</i>", ""]
    if others:
        lines.append(f"🤖 Otro partido de {hoy} no tiene 🎯: sale" if others == 1 else
                     f"🤖 Otros {others} partidos de {hoy} no tienen 🎯: salen")
        lines[-1] += " en \"Partidos de hoy\" (mensaje fijado) y en el dashboard, solo para seguirlos."
    if stars:
        lines.append("<i>⚡ = pago anticipado: si el equipo va 2 goles arriba, PlayDoit la paga como ganada. "
                     "El pick final llega en la última revisión antes de cada partido: unos 30 min antes (15 si "
                     "empieza a y cuarto o a menos cuarto), ya con la alineación si salió.</i>")
    return "\n".join(lines).rstrip(), len(stars)


# ─── noche ───────────────────────────────────────────────────────────────────

def evening_text(day, picks: list, parlay_rows: list, since_rows: list, tomorrow: dict, now: datetime,
                 tz) -> str:
    """El resumen de la noche."""
    stars = [r for r in picks if is_star(r)]
    others = [r for r in picks if not is_star(r)]
    lines = [f"🌙 <b>Así terminó el {day_name(day)}</b>", ""]
    if stars:
        lines.append(f"🎯 <b>Recomendados</b>: {record_text(record(stars))}")
        for r in stars:
            icon, what = pick_status(r, now)
            gain = f" ({money(r['profit'])})" if _num(r.get("profit")) is not None and r.get("result") in PLAYED else ""
            lines.append(f"{icon} {html.escape(match_name(r))} — {html.escape(pick_label(r))}: {what}{gain}")
        lines.append("")
    else:
        lines += ["🎯 Hoy no hubo recomendados.", ""]
    pls = [d for d in parlays_of(parlay_rows) if d["parlay"]]
    if pls:
        lines.append("🧩 <b>Parlays</b>")
        for d in pls:
            p = d["parlay"]
            name = f"{parlay_name(d, tz)} ({p['k']} partidos)"
            if p["status"] == "win":
                lines.append(f"✅ {name}: ganado · apostando 100: <b>{money(p['profit'])}</b>"
                             + (f" · {p['pa_legs']} cobrado(s) por ir 2 goles arriba ⚡" if p["pa_legs"] else ""))
            elif p["status"] == "loss":
                lines.append(f"❌ {name}: perdido ({p['won_legs']} de {p['k']} ganados) · apostando 100: -100")
            elif p["status"] == "push":
                lines.append(f"↔️ {name}: anulado, se devuelve lo apostado")
            else:
                lines.append(f"⏳ {name}: {p['won_legs']} de {p['k']} ganados hasta ahora")
        lines.append("")
    if others:
        rec = record(others)
        lines.append(f"🤖 Los demás picks (solo para seguirlos): {record_text(rec)}"
                     if rec["n"] else f"🤖 Los demás picks ({len(others)}) todavía no terminan.")
        lines.append("")
    tot = record([r for r in since_rows if is_star(r)])
    if tot["n"]:
        lines.append(f"📈 <b>Desde el 22 oct</b>, 🎯 recomendados: {tot['won']} de {tot['n']} ganados"
                     + (f" · apostando 100 a cada uno: <b>{money(tot['units'])}</b>" if tot["priced"] else ""))
    n_star, n_parlay = int(tomorrow.get("stars") or 0), int(tomorrow.get("parlays") or 0)
    if n_star or n_parlay:
        lines.append(f"🗓️ <b>Mañana</b>: {n_star} 🎯 recomendado{'s' if n_star != 1 else ''}"
                     + (f" y {n_parlay} parlay{'s' if n_parlay != 1 else ''}" if n_parlay else "")
                     + ". La lista llega en el mensaje de la mañana.")
    else:
        lines.append("🗓️ <b>Mañana</b>: por ahora sin 🎯 recomendados.")
    return "\n".join(lines).rstrip()


# ─── semana ──────────────────────────────────────────────────────────────────

def weekly_lines(picks: list, parlay_rows: list, since_rows: list) -> list:
    """El reporte de la semana (sin el encabezado ni los cambios con la alineación)."""
    from dashboard.display import league_name
    stars = [r for r in picks if is_star(r)]
    others = [r for r in picks if not is_star(r)]
    lines = ["— <b>ESTA SEMANA</b> —"]
    rec = record(stars)
    lines.append(f"🎯 Recomendados: {record_text(rec)}" if rec["n"] else "🎯 Recomendados: ninguno terminado.")
    by = {}
    for r in stars:
        if _str(r.get("result")) in PLAYED:
            by.setdefault(str(r.get("league") or ""), []).append(r)
    ranked = sorted(((lg, record(v)) for lg, v in by.items()), key=lambda x: (-x[1]["units"], -x[1]["n"]))
    if len(ranked) >= 2:
        best, worst = ranked[0], ranked[-1]
        lines.append(f"   Mejor liga: {html.escape(league_name(best[0]))} ({best[1]['won']} de {best[1]['n']}, "
                     f"{money(best[1]['units'])})")
        if worst[1]["units"] < 0:
            lines.append(f"   Peor liga: {html.escape(league_name(worst[0]))} ({worst[1]['won']} de "
                         f"{worst[1]['n']}, {money(worst[1]['units'])})")
    done = [d for d in parlays_of(parlay_rows) if d["parlay"] and d["parlay"]["settled"]]
    if done:
        won = sum(1 for d in done if d["parlay"]["status"] == "win")
        lost = sum(1 for d in done if d["parlay"]["status"] == "loss")
        units = sum(d["parlay"]["profit"] or 0 for d in done)
        lines.append(f"🧩 Parlays: {won} ganado{'s' if won != 1 else ''} y {lost} perdido{'s' if lost != 1 else ''}"
                     f" · apostando 100 a cada uno: <b>{money(units)}</b>")
    orec = record(others)
    if orec["n"]:
        lines.append(f"🤖 Los demás picks (solo para seguirlos): {record_text(orec)}")
    lines.append("")
    tot = record([r for r in since_rows if is_star(r)])
    if tot["n"]:
        lines += ["— <b>DESDE EL 22 OCT</b> —",
                  f"🎯 Recomendados: {tot['won']} de {tot['n']} ganados"
                  + (f" · apostando 100 a cada uno: <b>{money(tot['units'])}</b>" if tot["priced"] else ""), ""]
    return lines


# ─── consultas ───────────────────────────────────────────────────────────────

def load_picks(engine, a: datetime, b: datetime) -> list:
    return pd.read_sql(text(PICKS_SQL), engine, params={"a": a, "b": b}).to_dict("records")


def load_parlays(engine, d0, d1=None) -> list:
    try:
        df = pd.read_sql(text(PARLAYS_SQL), engine, params={"d0": pd.Timestamp(d0).date(),
                                                            "d1": pd.Timestamp(d1 or d0).date()})
    except Exception as e:                     # la tabla nace con el primer closing de v2.6.0
        if "does not exist" in str(e):
            return []
        raise
    return df.to_dict("records")


def load_since(engine, now: datetime) -> list:
    """Los 🎯 liquidados desde el modelo corregido (22-oct), lo del dashboard."""
    return pd.read_sql(text("""
        SELECT market, reason, result, profit FROM program_picks
        WHERE match_date >= :s AND match_date < :n AND market IS NOT NULL AND reason IS NULL
          AND result IN ('win', 'loss', 'push', 'half_win', 'half_loss')
    """), engine, params={"s": FIXED_SINCE.to_pydatetime(), "n": now}).to_dict("records")


def tomorrow_counts(engine, day, tz) -> dict:
    a, b = day_bounds(pd.Timestamp(day) + pd.Timedelta(days=1), tz)
    picks = load_picks(engine, a, b)
    parlays = parlays_of(load_parlays(engine, pd.Timestamp(day) + pd.Timedelta(days=1)))
    return {"stars": sum(1 for r in picks if is_star(r)), "parlays": len(parlays)}
