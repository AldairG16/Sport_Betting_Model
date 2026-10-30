"""
src/models/today_board.py
=========================
"Partidos de hoy" en Telegram (30-oct-26). El dueño: "yo quiero los partidos
que vamos teniendo y poder actualizarlos, por si en algún momento cambia el
pick poder notarlo"; "que lleven hora, para saber cuándo empiezan y así poder
ver su última actualización".

- Un mensaje con los partidos de hoy (hora de México) que no han empezado:
  hora de inicio, pick (🎯 si pasa la regla), momio mínimo de PlayDoit, de
  cuándo son sus cuotas y 🔄 si el pick cambió en las últimas 24 h. El closing
  lo EDITA en cada corrida (cada 30 min) sin sonar; al empezar el día va uno
  nuevo, fijado arriba del chat. Si no cabe, va en varios. Estado en
  model_state 'telegram_today'.
- Un aviso aparte (con sonido) cuando cambia el pick de un partido que empieza
  en las próximas CHANGE_ALERT_HOURS. Cada cambio sale una vez
  (model_state 'telegram_pick_changes').

El cambio lo anota snapshot_picks (program_picks.prev_label, prev_star,
changed_at). Las funciones de formato son puras; run_today_board hace las
consultas y los envíos.
"""

import html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

BOARD_KEY = "telegram_today"
CHANGES_KEY = "telegram_pick_changes"
CHANGE_ALERT_HOURS = 6           # aviso de cambio solo para lo que empieza pronto
CHANGED_RECENT_H = 24            # 🔄 en la lista si cambió en las últimas 24 h
CHUNK_CHARS = 3800               # Telegram corta en 4096
FEATURED_FAMILIES = ("Ganador", "Goles")   # sus cuotas vienen del fetch de la liga
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
DIAS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")

_SQL = """
    SELECT p.match, p.display, p.match_date, p.label, p.family, p.market, p.reason, p.min_us, p.lineup,
           p.prev_label, p.prev_star, p.changed_at, u.odds_fetched_at, u.specialty_fetched_at
    FROM program_picks p
    LEFT JOIN LATERAL (
        SELECT odds_fetched_at, specialty_fetched_at FROM upcoming_matches u
        WHERE u.home_team_norm || ' vs ' || u.away_team_norm = p.match
          AND ABS(EXTRACT(EPOCH FROM (u.match_date - p.match_date))) < 86400
        ORDER BY u.updated_at DESC NULLS LAST LIMIT 1
    ) u ON TRUE
    WHERE p.match_date >= :a AND p.match_date < :b
    ORDER BY p.match_date, p.display
"""


def _ts(v):
    t = pd.to_datetime(v, utc=True, errors="coerce")
    return None if t is None or pd.isna(t) else t.to_pydatetime()


def _hm(t: datetime, tz) -> str:
    return t.astimezone(tz).strftime("%H:%M")


def _when(t: datetime | None, now: datetime, tz) -> str:
    """'13:05' si es de hoy; '28 oct 03:31' si no."""
    if t is None:
        return "sin cuotas"
    loc = t.astimezone(tz)
    if loc.date() == now.astimezone(tz).date():
        return loc.strftime("%H:%M")
    return f"{loc.day} {MESES[loc.month - 1]} {loc.strftime('%H:%M')}"


def _star(r: dict) -> bool:
    return bool(r.get("label")) and not r.get("reason")


def pick_text(label, star: bool, min_us=None, reason=None) -> str:
    if not label:
        why = str(reason or "").split(":")[0].strip()
        return "Sin pick" + (f" · {why}" if why else "")
    from src.utils.min_odds import fmt_american
    s = ("🎯 " if star else "") + label
    if min_us is not None and not pd.isna(min_us):
        s += f" · mín. {fmt_american(int(min_us))}"
    return s


def odds_time(r: dict):
    """De cuándo son las cuotas del pick: las de la liga para ganador y goles,
    las del evento para lo demás (la otra si falta)."""
    league, event = _ts(r.get("odds_fetched_at")), _ts(r.get("specialty_fetched_at"))
    first, second = (league, event) if (r.get("family") or "Ganador") in FEATURED_FAMILIES else (event, league)
    return first or second


def match_lines(r: dict, now: datetime, tz) -> list:
    ko = _ts(r["match_date"])
    name = html.escape(str(r.get("display") or r.get("match")))
    changed = _ts(r.get("changed_at"))
    recent = changed is not None and now - changed <= timedelta(hours=CHANGED_RECENT_H)
    lines = [f"<b>{_hm(ko, tz)}</b> {name}" + (" 🔄" if recent else ""),
             f"   {html.escape(pick_text(r.get('label'), _star(r), r.get('min_us'), r.get('reason')))}"
             f" · cuotas {_when(odds_time(r), now, tz)}"
             + (" · alineación ✓" if str(r.get("lineup") or "").startswith("confirmada") else "")]
    if recent:
        before = pick_text(r.get("prev_label"), bool(r.get("prev_star")))
        lines.append(f"   🔄 cambió {_when(changed, now, tz)} · antes: {html.escape(before)}")
    return lines


def board_texts(rows: list, now: datetime, tz) -> list:
    """Los mensajes de "Partidos de hoy" (uno, o varios si no cabe)."""
    loc = now.astimezone(tz)
    day = f"{DIAS[loc.weekday()]} {loc.day} {MESES[loc.month - 1]}"
    head = (f"📋 <b>Partidos de hoy</b> · {day}\n"
            f"<i>Actualizado {loc.strftime('%H:%M')} (hora de México) · se actualiza solo cada 30 min</i>")
    upcoming = [r for r in rows if _ts(r["match_date"]) > now]
    started = len(rows) - len(upcoming)
    blocks = ["\n".join(match_lines(r, now, tz)) for r in upcoming]
    tail = []
    if not rows:
        tail.append("Hoy no hay partidos analizados.")
    elif not upcoming:
        tail.append("Ya empezaron todos los partidos de hoy.")
    elif started:
        tail.append(f"<i>Ya empezaron: {started}</i>")
    if any("🔄" in b for b in blocks):
        tail.append("<i>🔄 = el pick cambió en las últimas 24 h</i>")
    chunks, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 2 > CHUNK_CHARS:
            chunks.append(cur)
            cur = "📋 <b>Partidos de hoy</b> (sigue)"
        cur += "\n\n" + b
    for t in tail:
        if len(cur) + len(t) + 2 > CHUNK_CHARS:
            chunks.append(cur)
            cur = "📋 <b>Partidos de hoy</b> (sigue)"
        cur += "\n\n" + t
    chunks.append(cur)
    return chunks


def change_alert(rows: list, now: datetime, tz) -> str | None:
    """El aviso de los picks que cambiaron (None si no hay)."""
    if not rows:
        return None
    parts = ["🔄 <b>Cambió el pick</b>"]
    for r in sorted(rows, key=lambda r: _ts(r["match_date"])):
        name = html.escape(str(r.get("display") or r.get("match")))
        why = str(r.get("lineup") or "")
        parts.append(f"<b>{_hm(_ts(r['match_date']), tz)}</b> {name}\n"
                     f"Antes: {html.escape(pick_text(r.get('prev_label'), bool(r.get('prev_star'))))}\n"
                     f"Ahora: {html.escape(pick_text(r.get('label'), _star(r), r.get('min_us'), r.get('reason')))}"
                     + (f"\n<i>{html.escape(why)}</i>" if why.startswith("confirmada") else ""))
    return "\n\n".join(parts)


def changes_to_alert(rows: list, since: datetime | None, now: datetime) -> list:
    """Los cambios nuevos (después de `since`) de lo que empieza en las
    próximas CHANGE_ALERT_HOURS."""
    out = []
    for r in rows:
        changed, ko = _ts(r.get("changed_at")), _ts(r["match_date"])
        if changed is None or (since is not None and changed <= since):
            continue
        if now < ko <= now + timedelta(hours=CHANGE_ALERT_HOURS):
            out.append(r)
    return out


def run_today_board(engine=None, now: datetime | None = None, edit=None, send=None, pin=None,
                    load_state=None, save_state=None) -> dict:
    """Closing: actualiza "Partidos de hoy" y avisa los picks que cambiaron."""
    if engine is None:
        from config.database import engine
    if edit is None or send is None or pin is None:
        from scripts import notify_telegram as nt
        edit, send, pin = edit or nt.edit_or_send, send or nt.send_message, pin or nt.pin_message
    if load_state is None or save_state is None:
        from src.utils import model_state as ms
        load_state, save_state = load_state or ms.load_state, save_state or ms.save_state
    from config.settings import USER_TIMEZONE
    tz = ZoneInfo(USER_TIMEZONE)
    now = now or datetime.now(timezone.utc)
    loc = now.astimezone(tz)
    start = datetime(loc.year, loc.month, loc.day, tzinfo=tz).astimezone(timezone.utc)
    # lo de hoy y, para los avisos, lo que empieza en las próximas horas aunque sea mañana
    end = max(start + timedelta(days=1), now + timedelta(hours=CHANGE_ALERT_HOURS))
    rows = pd.read_sql(text(_SQL), engine, params={"a": start, "b": end}).to_dict("records")
    today = [r for r in rows if _ts(r["match_date"]) < start + timedelta(days=1)]

    st = load_state(BOARD_KEY) or {}
    same_day = st.get("day") == loc.date().isoformat()
    old = list(st.get("ids") or []) if same_day else []
    texts = board_texts(today, now, tz)
    ids = []
    for i, t in enumerate(texts):
        mid = edit(t, old[i] if i < len(old) else None, silent=True)
        if mid:
            ids.append(mid)
    for extra in old[len(texts):]:                 # sobraban mensajes de antes
        edit("📋 <i>Partidos de hoy: todo cabe en el mensaje de arriba.</i>", extra, silent=True)
    if ids and not same_day:
        pin(ids[0], unpin=list(st.get("ids") or [])[:1])
    save_state(BOARD_KEY, {"day": loc.date().isoformat(), "ids": ids or old}, history=False)

    cst = load_state(CHANGES_KEY) or {}
    since = _ts(cst.get("last"))
    changes = changes_to_alert(rows, since, now)
    msg = change_alert(changes, now, tz)
    if msg:
        send(msg)
    seen = [_ts(r.get("changed_at")) for r in rows if _ts(r.get("changed_at"))]
    last = max([t for t in (since, *seen) if t is not None], default=None)
    if last is not None and last != since:
        save_state(CHANGES_KEY, {"last": last.isoformat()}, history=False)

    n_up = sum(1 for r in today if _ts(r["match_date"]) > now)
    print(f"📋 Partidos de hoy en Telegram: {n_up} por empezar, {len(today) - n_up} ya empezaron, "
          f"{len(texts)} mensaje(s), {len(changes)} cambio(s) avisado(s)")
    return {"upcoming": n_up, "messages": len(texts), "changes": len(changes)}
