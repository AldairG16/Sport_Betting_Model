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

20-nov-26: los partidos (🎯 o no) van por bloques (src/models/match_blocks.py): con alineación y en el último
aviso, al dueño y al canal. Aquí quedan solo los parlays, al dueño (el canal recibe solo partidos). De aquí usa
match_blocks cutoff, key_of, form_line y missing_lines.
"""

import html
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from src.models import parlay_alerts as PA
from src.models import program_parlays as PL
from src.models.program_picks import KICK_LOCK_MIN
from src.utils.log import get_logger

log = get_logger(__name__)

STATE_KEY = "telegram_final_picks"
RUN_EVERY_MIN = 30                       # el closing corre a las :00 y a las :30 (cron-job.org)
KEEP_DAYS = 2
CHUNK_CHARS = 3800                       # Telegram corta en 4096
MAX_NAMES = 5                            # jugadores que se nombran en la línea de quién falta

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


def plan(legs: list, sent: dict, now: datetime, covered=None) -> tuple[list, dict]:
    """Función pura: los parlays que se fijan en esta corrida y el estado nuevo.
    legs: piernas de los parlays con bono por fijarse; sent: {"p": {parlay: firma}}."""
    end = cutoff(now)
    p_sent = dict(sent.get("p") or {})
    items = []
    by = {}
    for x in legs:
        by.setdefault(PA.key_of(x), []).append(x)
    for key in sorted(by):
        ls = by[key]
        first = min(_ts(x["match_date"]) for x in ls)
        need = PL.min_legs(ls[0].get("kind"))
        if not now < first <= end or len(ls) < need:
            continue
        sig = PA.signature(ls)
        before = p_sent.get(key)
        p_sent[key] = sig
        if before != sig:
            items.append({"kind": "parlay", "legs": ls, "late": before is not None,
                          "ready": PA.is_ready(ls, covered)})
    return items, {"p": _keep(p_sent, now)}


def form_line(r: dict) -> str:
    """El aviso de que un grande de nombre viene mal (big_names, 13-nov-26), en
    corto: '⚠️ Juventus viene mal: 15 puntos de sus últimos 30 en liga (es un
    aviso: el pick no cambia)'. '' si no hay."""
    note = r.get("form_note")
    if not isinstance(note, str) or not note.strip():
        return ""
    parts = [part.split(". ")[0].split(": ")[0].strip() if part.startswith("⭐") else part.split(". ")[0].strip()
             for part in note.split(" · ")]
    firm = [html.escape(x) for x in parts if x.startswith("⭐")]          # la marca del más firme (v2.14.0)
    warn = [html.escape(x) for x in parts if not x.startswith("⭐")]
    out = firm + ([f"⚠️ {' · '.join(warn)} <i>(es un aviso: el pick no cambia)</i>"] if warn else [])
    return " · ".join(out)


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
    ts = {min(_ts(x["match_date"]) for x in i["legs"]) for i in items}
    hm = [t.astimezone(tz).strftime("%H:%M") for t in sorted(ts)]
    return hm[0] if len(hm) == 1 else ", ".join(hm[:-1]) + " y " + hm[-1]


def final_texts(items: list, tz) -> list:
    """Los mensajes de los parlays de la corrida (uno, o varios si no cabe)."""
    if not items:
        return []
    head = (f"⏰ <b>Parlays finales · {_times(items, tz)}</b>\n"
            "<i>Es la última revisión antes de su primer partido: ya no cambian.</i>")
    chunks, cur = [], head
    for i in items:
        b = PA.alert_text(i["legs"], tz, changed=i["late"], late=not i["ready"])
        if len(cur) + len(b) + 2 > CHUNK_CHARS:
            chunks.append(cur)
            cur = "⏰ <b>Parlays finales</b> (sigue)"
        cur += "\n\n" + b
    chunks.append(cur)
    return chunks


def run_final_alerts(engine=None, now: datetime | None = None, send=None, load_state=None, save_state=None,
                     covered=None) -> dict:
    """Closing, después de rehacer picks y parlays: los parlays que se fijan en esta revisión, al dueño."""
    if engine is None:
        from config.database import engine
    if send is None:
        from scripts.notify_telegram import send_message as send
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
    legs = []
    try:
        legs = pd.read_sql(text(PA._SQL), engine, params={**p, "rule": PL.RULE}).to_dict("records")
    except Exception as e:
        if "does not exist" not in str(e):          # la tabla nace con el primer parlay
            raise
    st = load_state(STATE_KEY) or {}
    items, new_state = plan(legs, st, now, covered)
    texts = final_texts(items, tz)
    sent = sum(1 for t in texts if send(t))
    if sent < len(texts):                           # se reintenta en la siguiente corrida
        log.error(f"❌ Parlays finales: {len(texts) - sent} de {len(texts)} mensaje(s) no se mandaron a Telegram")
    elif new_state != {"p": dict(st.get("p") or {})}:
        save_state(STATE_KEY, new_state, history=False)
    print(f"⏰ Parlays finales en Telegram (hasta las {cutoff(now).astimezone(tz):%H:%M}): {len(items)} · "
          f"{sent} mensaje(s)")
    return {"parlay": len(items), "messages": sent}
