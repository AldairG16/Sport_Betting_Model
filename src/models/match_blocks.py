"""
src/models/match_blocks.py
==========================
Los partidos en Telegram por bloques (20-nov-26). El dueño, al ver "Partidos de hoy" (un mensaje fijado que se
editaba cada 30 min) y después "Hoy no hay recomendados… No hace falta apostar": "esto se me hace una
incongruencia y estar llenando de basura el canal, necesito que cada que mires el partido se mueve mandes ese
partido no que me actualices el mismo mensaje, se pierde, y también vayas mandando los partidos por bloques …
tampoco quiero el mensaje de no hay partidos recomendados … mejor en los partidos dime cuál es recomendado y cuál
no … de las 12 pm para abajo mándame unos y de las 12 para arriba otros … si el partido ya tiene alineación
ponlo con una palomita en verde". Eligió (1a) cada partido cuando sale su alineación, con aviso si cambia en los
últimos 30 min; (2a) todos los partidos, cada uno con recomendado o no; (3) un bloque con todos los partidos de
antes de las 12:00 y otro con todos los demás; (4) al canal, solo los partidos cuando ya tienen alineación.

Todo son mensajes nuevos: nada se edita ni se fija.
- Bloques del día, solo al dueño. En la primera revisión desde las 06:00 (hora de México), los partidos de hoy de
  antes de las 12:00; en la primera desde las 12:00, los de las 12:00 hasta las 06:00 del día siguiente. Cada uno
  con su hora, su pick, "🎯 RECOMENDADO" y su momio mínimo o "solo para seguir", y ✅ si ya tiene alineación. Los
  parlays van en el bloque de su primer partido.
- Con alineación, al dueño y al canal. En cada revisión, en un solo mensaje, los partidos de las próximas
  LINEUP_AHEAD_H horas que acaban de tener alineación, con su pick ya revisado y "antes: …" si cambió desde el
  bloque.
- Último aviso, al dueño y al canal, en la última revisión antes del partido (final_alerts.cutoff): el que
  cambió después de mandarse ("cambió a última hora") y el que nunca tuvo alineación (ESPN no la dio a tiempo o
  su liga no la tiene); ese es su pick final.
Cada partido sale una vez con alineación o en el último aviso, y otra solo si cambia (model_state
'telegram_blocks'). Las funciones de formato y plan() son puras; run_match_blocks hace las consultas y los envíos.
"""

import html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from src.models import final_alerts as FA
from src.models import telegram_reports as TR
from src.models.program_picks import LINEUP_OK
from src.utils.log import get_logger

log = get_logger(__name__)

STATE_KEY = "telegram_blocks"
OLD_BOARD_KEYS = ("telegram_today", "telegram_today_channel")   # el tablero fijado de antes (today_board)
FIRST_HOUR = 6                 # el bloque de la mañana sale en la primera revisión desde las 06:00
NOON = 12                      # y el de la tarde en la primera desde las 12:00
LINEUP_AHEAD_H = 3             # partidos que se miran por si ya tienen alineación
KEEP_DAYS = 2
CHUNK_CHARS = 3800             # Telegram corta en 4096

_SQL = """
    SELECT match, display, match_date, league, label, family, market, reason, min_us, lineup, form_note, result,
           lineup_mark
    FROM program_picks
    WHERE match_date > :now AND match_date < :end AND result IS DISTINCT FROM 'stale'
    ORDER BY match_date, display
"""
_LINEUP_SQL = """
    SELECT home, away, kickoff, weakened, missing FROM lineup_alerts
    WHERE kickoff > :now AND kickoff <= :end
"""
LEGEND = ("<i>🎯 = recomendado: apuesta si PlayDoit paga el mínimo o más. ✅ = ya con alineación. ⚡ = pago "
          "anticipado: si el equipo va 2 goles arriba, PlayDoit la paga como ganada. El pick se revisa hasta unos 30 "
          "min antes: cada partido vuelve a llegar cuando sale su alineación.</i>")


def _ts(v):
    return FA._ts(v)


def has_lineup(r: dict) -> bool:
    return str(r.get("lineup") or "").startswith(LINEUP_OK)


def sig(r: dict) -> str:
    """Lo que se le dijo al dueño de un partido: el pick y si era 🎯."""
    return f"{r.get('label') or ''}|{int(TR.is_star(r))}"


def before_text(s: str) -> str:
    label, _, star = str(s).rpartition("|")
    return html.escape(label or "sin pick") + (" (🎯 recomendado)" if star == "1" else " (solo para seguir)")


def match_lines(r: dict, tz, day=None, before: str | None = None, missing=()) -> list:
    """Las líneas de un partido: hora (con el día si no es `day`), nombre, ✅ si ya tiene alineación y su pick,
    recomendado o solo para seguir; "antes: …" si cambió desde lo que se le dijo."""
    from src.models.early_payout import applies
    from src.utils.min_odds import fmt_american
    ko = _ts(r["match_date"]).astimezone(tz)
    star = TR.is_star(r)
    when = f"<b>{ko:%H:%M}</b>" + (f" ({TR.DIAS[ko.weekday()]})" if day is not None and ko.date() != day else "")
    lines = [f"{'🎯 ' if star else ''}{when} {html.escape(TR.match_name(r))}" + (" ✅" if has_lineup(r) else "")]
    if not TR._str(r.get("market")):
        lines.append("   sin pick: el programa ve lo mismo que el mercado · solo para seguir")
    elif star:
        mn = r.get("min_us")
        price = (f"apuesta si PlayDoit paga <b>{fmt_american(int(mn))}</b> o más"
                 if mn is not None and not pd.isna(mn) else "sin momio de PlayDoit para decir cuánto debe pagar")
        pa = " ⚡" if applies(r.get("league"), r.get("market")) else ""
        lines.append(f"   <b>RECOMENDADO</b> · {html.escape(TR.pick_label(r))}{pa} · {price}")
        form = FA.form_line(r)
        if form:
            lines.append(f"   {form}")
    else:
        lines.append(f"   {html.escape(TR.pick_label(r))} · solo para seguir")
    if before is not None and before != sig(r):
        lines.append(f"   🔄 antes: {before_text(before)}")
    lines += [f"   {t}" for t in missing]
    lines += [f"   {t}" for t in mark_lines(r)]
    return lines


def mark_lines(r: dict) -> list:
    """Las bajas que pesan (26-nov-26, program_picks.lineup_mark), una línea por equipo: '⚠️ A Bayern le faltan
    Kane y Olise, que metieron 39 de cada 100 goles del equipo (es una marca: el 🎯 se queda)'."""
    mark = TR._str(r.get("lineup_mark"))
    if not mark:
        return []
    tail = " <i>(es una marca: el 🎯 se queda, la casa ya lo cuenta en el momio)</i>" if TR.is_star(r) else ""
    return [f"⚠️ {html.escape(part)}{tail}" for part in mark.split(" · ") if part.strip()]


def _chunks(head: str, blocks: list, more: str) -> list:
    out, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 2 > CHUNK_CHARS:
            out.append(cur)
            cur = more
        cur += "\n\n" + b
    out.append(cur)
    return out


def part_of(r: dict, tz) -> str:
    return "am" if _ts(r["match_date"]).astimezone(tz).hour < NOON else "pm"


def day_block_texts(day, part: str, rows: list, parlays: list, tz) -> list:
    """El bloque del día (solo al dueño): todos los partidos de antes de las 12:00 (am) o de las 12:00 en adelante
    (pm), recomendados o no, y los parlays que empiezan ahí. [] si no hay nada."""
    if not rows and not parlays:
        return []
    stars = sum(1 for r in rows if TR.is_star(r))
    n = len(rows)
    title = ("☀️ <b>Partidos de hoy · antes de las 12:00</b>" if part == "am"
             else "🌤️ <b>Partidos de hoy · de las 12:00 en adelante</b>")
    head = (f"{title}\n<i>{TR.day_name(day)} · {n} partido{'s' if n != 1 else ''}, {stars} 🎯 "
            f"recomendado{'s' if stars != 1 else ''}</i>")
    blocks = ["\n".join(match_lines(r, tz, day)) for r in rows]
    if parlays:
        blocks.append("\n".join(TR.parlay_lines(parlays, tz)))
    blocks.append(LEGEND)
    return _chunks(head, blocks, f"{title} (sigue)")


def _times(rows: list, tz) -> str:
    hm = [t.astimezone(tz).strftime("%H:%M") for t in sorted({_ts(r["match_date"]) for r in rows})]
    return hm[0] if len(hm) == 1 else ", ".join(hm[:-1]) + " y " + hm[-1]


def lineup_texts(items: list, tz, missing: dict) -> list:
    """Los partidos que acaban de tener alineación (al dueño y al canal)."""
    if not items:
        return []
    rows = [i["row"] for i in items]
    head = (f"📋 <b>Con alineación · {_times(rows, tz)}</b>\n"
            "<i>Ya con la alineación: si cambia antes del partido, llega otra vez.</i>")
    blocks = ["\n".join(match_lines(i["row"], tz, None, i.get("before"), missing.get(str(i["row"]["match"]).lower(), [])))
              for i in items]
    return _chunks(head, blocks, "📋 <b>Con alineación</b> (sigue)")


def final_texts(items: list, tz, missing: dict) -> list:
    """El último aviso (al dueño y al canal): lo que cambió después de mandarse y lo que nunca tuvo alineación."""
    if not items:
        return []
    rows = [i["row"] for i in items]
    head = (f"⏰ <b>Último aviso · {_times(rows, tz)}</b>\n"
            "<i>Es la última revisión antes del partido: ya no cambia.</i>")
    blocks = []
    for i in items:
        r = i["row"]
        lines = match_lines(r, tz, None, i.get("before"), missing.get(str(r["match"]).lower(), []))
        if i["kind"] == "changed":
            lines[0] = f"🔄 {lines[0]} · <i>cambió a última hora</i>"
        else:
            lines.insert(1, "   ⏳ sin alineación a tiempo: este es su pick final")
        blocks.append("\n".join(lines))
    return _chunks(head, blocks, "⏰ <b>Último aviso</b> (sigue)")


def _keep(d: dict, now: datetime) -> dict:
    old = now - timedelta(days=KEEP_DAYS)
    return {k: v for k, v in d.items() if (_ts(k.rsplit("|", 1)[-1]) or now) >= old}


def plan(rows: list, state: dict, now: datetime, tz) -> dict:
    """Función pura: qué mandar en esta revisión. rows: program_picks de ahora a mañana a las 06:00 (o más).
    Devuelve {"am", "pm": filas del bloque o None, "lineup", "final": [{"row", "before", "kind"}], "state"}."""
    loc = now.astimezone(tz)
    today = loc.date()
    days = dict(state.get("days") or {})
    shown, sent = dict(state.get("shown") or {}), dict(state.get("sent") or {})
    rows = sorted((r for r in rows if _ts(r["match_date"]) is not None and _ts(r["match_date"]) > now),
                  key=lambda r: (_ts(r["match_date"]), str(r.get("display") or r["match"])))
    out = {"am": None, "pm": None, "lineup": [], "final": []}
    start = datetime(today.year, today.month, today.day, tzinfo=tz)
    span = {"am": (start, start + timedelta(hours=NOON)),
            "pm": (start + timedelta(hours=NOON), start + timedelta(days=1, hours=FIRST_HOUR))}
    for part, hour in (("am", FIRST_HOUR), ("pm", NOON)):
        if loc.hour < hour or days.get(part) == today.isoformat():
            continue
        days[part] = today.isoformat()
        a, b = span[part]
        mine = [r for r in rows if a <= _ts(r["match_date"]) < b]
        out[part] = mine
        for r in mine:
            shown[FA.key_of(r)] = sig(r)
    this_run = set()
    for r in rows:
        k = FA.key_of(r)
        if _ts(r["match_date"]) <= now + timedelta(hours=LINEUP_AHEAD_H) and has_lineup(r) and k not in sent:
            out["lineup"].append({"row": r, "before": shown.get(k)})
            sent[k] = sig(r)
            this_run.add(k)
    end = FA.cutoff(now)
    for r in rows:
        k = FA.key_of(r)
        if _ts(r["match_date"]) > end or k in this_run:
            continue
        if k not in sent:
            out["final"].append({"row": r, "before": shown.get(k), "kind": "nolineup"})
        elif sent[k] != sig(r):
            out["final"].append({"row": r, "before": sent[k], "kind": "changed"})
        sent[k] = sig(r)
    out["state"] = {"days": days, "shown": _keep(shown, now), "sent": _keep(sent, now)}
    return out


def retire_board(load_state, save_state, unpin) -> int:
    """Una vez: quita de arriba del chat y del canal el tablero fijado de antes (today_board)."""
    n = 0
    for key in OLD_BOARD_KEYS:
        st = load_state(key) or {}
        ids = list(st.get("ids") or [])
        if not ids or st.get("retired"):
            continue
        chat = None
        if key.endswith("_channel"):
            from src.models import telegram_channel as TC
            ch = TC.current(load_state)
            chat = ch["chat_id"] if ch else None
            if chat is None:
                continue
        for mid in ids:
            n += bool(unpin(mid, chat_id=chat))
        save_state(key, {**st, "retired": True}, history=False)
    return n


def run_match_blocks(engine=None, now: datetime | None = None, send=None, share=None, load_state=None,
                     save_state=None, unpin=None) -> dict:
    """Closing, después de rehacer los picks: los bloques del día, los partidos con alineación y el último aviso."""
    if engine is None:
        from config.database import engine
    if send is None or share is None or unpin is None:
        from scripts import notify_telegram as nt
        send, share, unpin = send or nt.send_message, share or nt.share_message, unpin or nt.unpin_message
    if load_state is None or save_state is None:
        from src.utils import model_state as ms
        load_state, save_state = load_state or ms.load_state, save_state or ms.save_state
    from config.settings import USER_TIMEZONE
    tz = ZoneInfo(USER_TIMEZONE)
    now = now or datetime.now(timezone.utc)
    try:
        retire_board(load_state, save_state, unpin)
    except Exception as e:                          # mejor esfuerzo: el mensaje viejo solo queda fijado
        log.warning(f"⚠️  No se pudo quitar el tablero fijado de antes ({type(e).__name__})")
    loc = now.astimezone(tz)
    end = datetime(loc.year, loc.month, loc.day, tzinfo=tz) + timedelta(days=1, hours=FIRST_HOUR)
    end = max(end.astimezone(timezone.utc), now + timedelta(hours=LINEUP_AHEAD_H))
    p = {"now": now, "end": end}
    rows = pd.read_sql(text(_SQL), engine, params=p).to_dict("records")
    try:
        lineups = pd.read_sql(text(_LINEUP_SQL), engine, params={
            "now": now, "end": now + timedelta(hours=LINEUP_AHEAD_H)}).to_dict("records")
    except Exception as e:
        if "does not exist" not in str(e):           # la tabla nace con la primera alerta de alineación
            raise
        lineups = []
    missing = FA.missing_lines(lineups)
    st = load_state(STATE_KEY) or {}
    pl = plan(rows, st, now, tz)
    owner, both = [], []
    for part in ("am", "pm"):
        if pl[part] is not None:
            pls = [d for d in TR.parlays_of(TR.load_parlays(engine, loc.date())) if d["parlay"]
                   and ("am" if _ts(d["slot"]).astimezone(tz).hour < NOON else "pm") == part]
            owner += day_block_texts(loc.date(), part, pl[part], pls, tz)
    both += lineup_texts(pl["lineup"], tz, missing) + final_texts(pl["final"], tz, missing)
    sent = sum(1 for t in owner + both if send(t))
    if sent < len(owner) + len(both):                # se reintenta en la siguiente revisión
        log.error(f"❌ Partidos por bloques: {len(owner) + len(both) - sent} de {len(owner) + len(both)} "
                  f"mensaje(s) no se mandaron a Telegram")
    else:
        if pl["state"] != st:
            save_state(STATE_KEY, pl["state"], history=False)
        for t in both:                               # al canal, solo los partidos con alineación y el último aviso
            share(t, "los partidos")
    print(f"📋 Partidos por bloques en Telegram: bloque de la mañana {len(pl['am'] or [])}, de la tarde "
          f"{len(pl['pm'] or [])}, con alineación {len(pl['lineup'])}, último aviso {len(pl['final'])} · "
          f"{sent} mensaje(s)")
    return {"am": len(pl["am"] or []), "pm": len(pl["pm"] or []), "lineup": len(pl["lineup"]),
            "final": len(pl["final"]), "messages": sent}
