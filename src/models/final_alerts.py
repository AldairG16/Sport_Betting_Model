"""
src/models/final_alerts.py
==========================
Los picks finales en Telegram (v2.11.0, 8-nov-26). El dueño: "actualmente me
llegan muchas alertas de cambios de pick, a veces también son 2 veces antes del
partido, y todo eso se me pierde en el telegram, quiero que solo llegue el
último cambio, necesito que revises cuánto tiempo antes del partido cambia el
pick para que ese sea el límite de mandar el pick al telegram, y si hay parley
también mandarlo". Eligió (1.1) solo los 🎯 y los parlays, con aviso si uno que
salió 🎯 en la mañana dejó de serlo, y (2.1) mandarlo en la última revisión.

Lo medido (281 partidos del 31-oct al 7-nov, program_picks.changed_at): el
último cambio del pick llega hasta la última revisión antes del partido (30
entre 5 y 35 min antes) y nunca después: el closing corre a las :00 y :30 y a
KICK_LOCK_MIN del partido ya no lo toca. La alineación se ve casi siempre en
esa revisión (mediana 28 min antes). El sábado 7-nov llegaron 22 avisos de
"Cambió el pick" (36 cambios), 2 de parlay y hasta 11 de alineación.

En cada closing, después de rehacer los picks y los parlays:
- a tiempo (`cutoff`): los partidos que ninguna revisión regular vuelve a tocar,
  los que empiezan antes de la siguiente (:00 o :30) + KICK_LOCK_MIN. Para uno
  de las 13:00 es la revisión de las 12:30 (~28 min antes); para uno de las
  13:15 o 13:45, la de 15 min antes.
- un mensaje por corrida con lo que importa de esos horarios: los 🎯 (momio
  mínimo, ⚡, si ya vio la alineación y quién falta), los que salieron 🎯 en el
  mensaje de la mañana o en el avance de la noche y ya no lo son, y los parlays
  que se fijan ahí. Sin nada de eso no hay mensaje.
- cada partido y cada parlay se manda una vez (model_state 'telegram_final_picks').
  Si una corrida de más (los cron de GitHub) lo cambia antes del partido, sale
  otra vez marcado "cambió a última hora": también es el último cambio.
Ya no salen "Cambió el pick" (today_board), "Parlay listo" ni "Cambió el
parlay" (parlay_alerts), ni el aviso de alineación aparte: lineup_alerts lo
guarda (el dashboard lo muestra) y aquí va la línea de quién falta.
"""

import html
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from src.models import parlay_alerts as PA
from src.models import program_parlays as PL
from src.models import telegram_reports as TR
from src.models.program_picks import KICK_LOCK_MIN, LINEUP_OK
from src.utils.log import get_logger

log = get_logger(__name__)

STATE_KEY = "telegram_final_picks"
ANNOUNCED_KEY = "telegram_announced"     # los 🎯 del mensaje de la mañana y del avance de la noche
RUN_EVERY_MIN = 30                       # el closing corre a las :00 y a las :30 (cron-job.org)
KEEP_DAYS = 2
CHUNK_CHARS = 3800                       # Telegram corta en 4096
MAX_NAMES = 5                            # jugadores que se nombran en la línea de quién falta

_PICKS_SQL = """
    SELECT match, display, match_date, league, label, family, market, reason, star_note, min_us, lineup, result
    FROM program_picks
    WHERE match_date > :now AND match_date <= :end AND result IS DISTINCT FROM 'stale'
"""
_LINEUP_SQL = """
    SELECT home, away, kickoff, weakened, missing FROM lineup_alerts
    WHERE kickoff > :now AND kickoff <= :end
"""


def _ts(v) -> datetime | None:
    t = pd.to_datetime(v, utc=True, errors="coerce")
    return None if t is None or pd.isna(t) else t.to_pydatetime()


def key_of(r: dict) -> str:
    """El partido: nombre y hora de inicio (un reprogramado es otro)."""
    return f"{str(r['match']).lower()}|{_ts(r['match_date']).isoformat()}"


def _key_time(key: str) -> datetime | None:
    """La hora de un partido (…|hora) o de un parlay (hora|tipo|número)."""
    for part in (key.rsplit("|", 1)[-1], key.split("|", 1)[0]):
        t = _ts(part)
        if t is not None:
            return t
    return None


def _keep(d: dict, now: datetime) -> dict:
    old = now - timedelta(days=KEEP_DAYS)
    return {k: v for k, v in d.items() if (_key_time(k) or now) >= old}


def cutoff(now: datetime) -> datetime:
    """Hasta qué hora de inicio un partido ya no lo toca ninguna revisión
    regular: la siguiente (:00 o :30) + KICK_LOCK_MIN."""
    base = pd.Timestamp(now).floor(f"{RUN_EVERY_MIN}min")
    return (base + pd.Timedelta(minutes=RUN_EVERY_MIN + KICK_LOCK_MIN)).to_pydatetime()


def why_not(r: dict) -> str:
    """Por qué ya no es 🎯, en corto: 'a Napoli le faltan 5 titulares
    habituales', 'no hay favorito claro', 'Napoli paga muy poco (-650)'."""
    reason = str(r.get("reason") or "")
    if reason.startswith("Alineación") and ":" in reason:
        return reason.split(":", 1)[1].split("(")[0].strip()
    note = str(r.get("star_note") or reason).split(":")[0].strip()
    if note.startswith(("No ", "Sin ", "Liga ", "El ")):
        note = note[0].lower() + note[1:]
    return note or "cambiaron los momios"


def plan(picks: list, legs: list, sent: dict, announced: dict, now: datetime, covered=None,
         tz=None) -> tuple[list, dict]:
    """Función pura: qué decir en esta corrida y el estado nuevo.
    picks: program_picks de los próximos partidos; legs: piernas de los
    parlays con bono por fijarse; sent: {"m": {partido: firma}, "p": {parlay:
    firma}}; announced: {"keys": {partido: pick} que salieron 🎯, "days": días
    (de México) con mensaje}. "Nuevo" solo en un día con mensaje guardado."""
    tz = tz or ZoneInfo("America/Mexico_City")
    end = cutoff(now)
    keys, days = announced.get("keys") or {}, set(announced.get("days") or [])
    m_sent, p_sent = dict(sent.get("m") or {}), dict(sent.get("p") or {})
    items = []
    for r in sorted(picks, key=lambda r: (_ts(r["match_date"]), str(r.get("display") or r["match"]))):
        ko = _ts(r["match_date"])
        if ko is None or not now < ko <= end or r.get("result") == "stale":
            continue
        k, star = key_of(r), TR.is_star(r)
        sig = f"{r.get('label') or ''}|{int(star)}"
        before = m_sent.get(k)
        m_sent[k] = sig
        if before == sig:
            continue
        late = before is not None                      # ya se había mandado: cambió a última hora
        if star:
            new = k not in keys and ko.astimezone(tz).date().isoformat() in days
            items.append({"kind": "star", "row": r, "new": new, "late": late})
        elif (k in keys and not late) or (late and before.endswith("|1")):
            items.append({"kind": "lost", "row": r, "late": late})
    by = {}
    for x in legs:
        by.setdefault(PA.key_of(x), []).append(x)
    for key in sorted(by):
        ls = by[key]
        first = min(_ts(x["match_date"]) for x in ls)
        need = PL.DAY_MIN_LEGS if ls[0].get("kind") == "dia" else PL.SLOT_MIN_LEGS
        if not now < first <= end or len(ls) < need:
            continue
        sig = PA.signature(ls)
        before = p_sent.get(key)
        p_sent[key] = sig
        if before != sig:
            items.append({"kind": "parlay", "legs": ls, "late": before is not None,
                          "ready": PA.is_ready(ls, covered)})
    return items, {"m": _keep(m_sent, now), "p": _keep(p_sent, now)}


def lineup_note(r: dict, covered=None) -> str:
    if str(r.get("lineup") or "").startswith(LINEUP_OK):
        return "📋 ya revisado con la alineación"
    if covered is not None and r.get("league") in covered:
        return "⏳ todavía sin alineación: revísala antes de apostar"
    return ""


def missing_lines(rows: list) -> dict:
    """{partido: ["📋 A Elversberg le faltan 5 titulares: …"]} de lineup_alerts
    (dos filas por equipo, una por mercado del rival: una línea)."""
    from dashboard.display import team_name
    out, seen = {}, set()
    for a in rows:
        match = f"{a['home']} vs {a['away']}".lower()
        if (match, a["weakened"]) in seen:
            continue
        seen.add((match, a["weakened"]))
        names = a["missing"] if isinstance(a["missing"], list) else json.loads(a["missing"] or "[]")
        shown = ", ".join(names[:MAX_NAMES]) + ("…" if len(names) > MAX_NAMES else "")
        out.setdefault(match, []).append(f"📋 A {team_name(a['weakened'])} le faltan {len(names)} titulares: "
                                         f"{html.escape(shown)}")
    return out


def _times(items: list, tz) -> str:
    ts = set()
    for i in items:
        ts.add(_ts(i["row"]["match_date"]) if "row" in i else min(_ts(x["match_date"]) for x in i["legs"]))
    hm = [t.astimezone(tz).strftime("%H:%M") for t in sorted(ts)]
    return hm[0] if len(hm) == 1 else ", ".join(hm[:-1]) + " y " + hm[-1]


def final_texts(items: list, missing: dict, tz, covered=None) -> list:
    """Los mensajes de la corrida (uno, o varios si no cabe)."""
    if not items:
        return []
    from src.models.early_payout import applies
    from src.utils.min_odds import fmt_american
    head = (f"⏰ <b>Picks finales · {_times(items, tz)}</b>\n"
            "<i>Es la última revisión antes del partido: ya no cambian.</i>")
    blocks = []
    for i in items:
        if i["kind"] == "parlay":
            blocks.append(PA.alert_text(i["legs"], tz, changed=i["late"], late=not i["ready"]))
            continue
        r = i["row"]
        ko = _ts(r["match_date"]).astimezone(tz)
        name = html.escape(TR.match_name(r))
        if i["kind"] == "star":
            mn = r.get("min_us")
            price = (f"apuesta si PlayDoit paga <b>{fmt_american(int(mn))}</b> o más"
                     if mn is not None and not pd.isna(mn) else "sin momio de PlayDoit para decir cuánto debe pagar")
            pa = " ⚡" if applies(r.get("league"), r.get("market")) else ""
            first = f"🎯 <b>{ko:%H:%M}</b> {name}"
            if i["late"]:
                first = f"🔄 {first} · <i>cambió a última hora</i>"
            elif i["new"]:
                first += " · <i>nuevo: no estaba en el mensaje de la mañana</i>"
            lines = [first, f"   {html.escape(TR.pick_label(r))}{pa} · {price}"]
            note = lineup_note(r, covered)
            if note:
                lines.append(f"   {note}")
        else:
            lines = [("🔄 " if i["late"] else "") + f"❌ <b>{ko:%H:%M}</b> {name}",
                     f"   Ya no es 🎯: {html.escape(why_not(r))}. No lo apuestes."]
        lines += [f"   {t}" for t in missing.get(str(r["match"]).lower(), [])]
        blocks.append("\n".join(lines))
    # el parlay ya trae la nota del ⚡; sin ⚡ no hace falta
    if (not any(i["kind"] == "parlay" for i in items)
            and any(i["kind"] == "star" and applies(i["row"].get("league"), i["row"].get("market")) for i in items)):
        blocks.append("<i>⚡ = pago anticipado: si el equipo va 2 goles arriba, PlayDoit la paga como ganada.</i>")
    chunks, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 2 > CHUNK_CHARS:
            chunks.append(cur)
            cur = "⏰ <b>Picks finales</b> (sigue)"
        cur += "\n\n" + b
    chunks.append(cur)
    return chunks


def remember_announced(day, stars: list, now: datetime | None = None, load_state=None, save_state=None) -> int:
    """Los 🎯 que salieron en el mensaje de la mañana (o en el avance de la
    noche) y su día: el mensaje final avisa si alguno deja de serlo y marca
    "nuevo" el 🎯 que no estaba (también en un día que salió sin 🎯)."""
    if load_state is None or save_state is None:
        from src.utils import model_state as ms
        load_state, save_state = load_state or ms.load_state, save_state or ms.save_state
    now = now or datetime.now(timezone.utc)
    st = load_state(ANNOUNCED_KEY) or {}
    keys = dict(st.get("keys") or {})
    keys.update({key_of(r): str(r.get("label") or "") for r in stars})
    old = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    days = sorted({d for d in (st.get("days") or []) if d >= old} | {pd.Timestamp(day).date().isoformat()})
    save_state(ANNOUNCED_KEY, {"keys": _keep(keys, now), "days": days}, history=False)
    return len(stars)


def run_final_alerts(engine=None, now: datetime | None = None, send=None, load_state=None, save_state=None,
                     covered=None, share=None) -> dict:
    """Closing, después de rehacer picks y parlays: los picks finales de los
    partidos que ya no vuelve a tocar ninguna revisión."""
    if engine is None:
        from config.database import engine
    if send is None:
        from scripts.notify_telegram import send_message as send
    if share is None:                              # el canal de los partidos (8-nov-26)
        from scripts.notify_telegram import share_message as share
    if load_state is None or save_state is None:
        from src.utils import model_state as ms
        load_state, save_state = load_state or ms.load_state, save_state or ms.save_state
    if covered is None:
        from src.features.espn_lineups import espn_leagues
        covered = set(espn_leagues())
    from config.settings import USER_TIMEZONE
    tz = ZoneInfo(USER_TIMEZONE)
    now = now or datetime.now(timezone.utc)
    p = {"now": now, "end": cutoff(now)}
    picks = pd.read_sql(text(_PICKS_SQL), engine, params=p).to_dict("records")
    legs, lineups = [], []
    try:
        legs = pd.read_sql(text(PA._SQL), engine, params={**p, "rule": PL.RULE}).to_dict("records")
    except Exception as e:
        if "does not exist" not in str(e):          # la tabla nace con el primer parlay
            raise
    try:
        lineups = pd.read_sql(text(_LINEUP_SQL), engine, params=p).to_dict("records")
    except Exception as e:
        if "does not exist" not in str(e):          # la tabla nace con la primera alerta de alineación
            raise
    st = load_state(STATE_KEY) or {}
    announced = load_state(ANNOUNCED_KEY) or {}
    items, new_state = plan(picks, legs, st, announced, now, covered, tz)
    texts = final_texts(items, missing_lines(lineups), tz, covered)
    sent = 0
    for t in texts:
        if send(t):
            sent += 1
    if sent < len(texts):                           # se reintenta en la siguiente corrida
        log.error(f"❌ Picks finales: {len(texts) - sent} de {len(texts)} mensaje(s) no se mandaron a Telegram")
    else:
        if new_state != st:
            save_state(STATE_KEY, new_state, history=False)
        for t in texts:                             # al canal, una vez: solo si al dueño le llegó todo
            share(t, "los picks finales")
    count = {k: sum(1 for i in items if i["kind"] == k) for k in ("star", "lost", "parlay")}
    print(f"⏰ Picks finales en Telegram (hasta las {cutoff(now).astimezone(tz):%H:%M}): {count['star']} 🎯, "
          f"{count['lost']} que ya no son 🎯, {count['parlay']} parlay(s) · {sent} mensaje(s)")
    return {**count, "messages": sent}
