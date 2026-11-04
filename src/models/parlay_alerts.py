"""
src/models/parlay_alerts.py
===========================
El aviso de Telegram del parlay listo (v2.7.0, 4-nov-26). El dueño: "en los
parlays necesito que se tomen en cuenta ya que se hagan las alineaciones…
si son al mismo horario sería mejor porque las alineaciones ya estarían para
todos los picks del parlay". Eligió que avise Telegram (3a).

Un parlay (los partidos de un horario, program_parlays.slot) está listo cuando
todas sus piernas se eligieron con la alineación publicada (lineup
"confirmada…") o su liga no la tiene en ESPN (ahí nunca llega). El closing
(cada 30 min) guarda las alineaciones, rehace el parlay con ellas
(snapshot_parlays) y después avisa: una vez por parlay listo y otra si cambia
antes de quedar fijo (5 min antes de su primer partido). Estado en
model_state 'telegram_parlays' ({horario: piernas avisadas}).

Medido el 4-nov: el último pick con alineación se calculó entre 6 y 33 min
antes del partido (casi siempre ~28): el aviso llega con ese margen.
"""

import html
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from src.models import program_parlays as PL
from src.utils.log import get_logger

log = get_logger(__name__)

ALERT_KEY = "telegram_parlays"
AHEAD_HOURS = 3              # las alineaciones salen ~1 h antes: más adelante nada está listo
KEEP_DAYS = 2                # lo avisado se olvida a los 2 días
DIAS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

_SQL = """
    SELECT slot, rank, match, display, match_date, league, label, market, model_prob, market_prob,
           playdoit_odds, lineup
    FROM program_parlays
    WHERE result IS NULL AND slot > :now AND slot <= :end
    ORDER BY slot, rank
"""


def _ts(v) -> datetime | None:
    t = pd.to_datetime(v, utc=True, errors="coerce")
    return None if t is None or pd.isna(t) else t.to_pydatetime()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _us(dec) -> str:
    from src.utils.min_odds import fmt_american, to_american
    return fmt_american(to_american(dec)) if dec and dec > 1 else "—"


def _pct(p) -> str:
    return f"{p:.0%}" if p is not None else "—"


def is_ready(legs: list, covered=None) -> bool:
    return bool(legs) and all(PL.lineup_ready(x.get("lineup"), x.get("league"), covered) for x in legs)


def signature(legs: list) -> str:
    """Lo que se avisó: cada partido con su apuesta, en orden."""
    return " | ".join(f"{x['match']}:{x['market']}" for x in sorted(legs, key=lambda x: int(x["rank"])))


def alert_text(legs: list, tz, changed: bool = False) -> str:
    """El mensaje del parlay de un horario: sus partidos (de la más segura a
    la menos) y lo que paga el de 2, 3 y 4 con la probabilidad de que salgan
    todas. Momios americanos, como PlayDoit."""
    legs = sorted(legs, key=lambda x: int(x["rank"]))
    slot = min(_ts(x["match_date"]) for x in legs).astimezone(tz)
    fixed = slot - timedelta(minutes=PL.LOCK_MINUTES)
    title = (f"🔄 <b>Cambió el parlay de las {slot:%H:%M}</b>" if changed
             else f"🧩 <b>Parlay de las {slot:%H:%M}</b> · ya vio las alineaciones")
    parts = [title + f"\n<i>{DIAS[slot.weekday()]} {slot.day} {MESES[slot.month - 1]} · "
                     f"queda fijo a las {fixed:%H:%M} (hora de México)</i>"]
    rows = []
    for i, x in enumerate(legs, 1):
        ko = _ts(x["match_date"]).astimezone(tz)
        note = str(x.get("lineup") or "")
        extra = note.split(":", 1)[1].strip() if note.startswith(PL.LINEUP_OK) and ":" in note else ""
        rows.append(f"{i}. <b>{ko:%H:%M}</b> {html.escape(str(x.get('display') or x['match']))}\n"
                    f"   {html.escape(str(x.get('label') or x['market']))} · {_us(_num(x['playdoit_odds']))}"
                    + ("" if note.startswith(PL.LINEUP_OK) else " · <i>sin alineación en ESPN</i>")
                    + (f"\n   <i>{html.escape(extra)}</i>" if extra else ""))
    parts.append("\n".join(rows))
    sizes = []
    for k in range(PL.MIN_BLOCK_LEGS, len(legs) + 1):
        use = legs[:k]
        odds = math.prod(_num(x["playdoit_odds"]) or 1 for x in use)
        pm = [_num(x.get("model_prob")) for x in use]
        pk = [_num(x.get("market_prob")) for x in use]
        sizes.append(f"Parlay de {k}: <b>{_us(odds)}</b> · salen todas: programa "
                     f"{_pct(math.prod(pm) if None not in pm else None)} / mercado "
                     f"{_pct(math.prod(pk) if None not in pk else None)}")
    parts.append("\n".join(sizes))
    parts.append("<i>" + ("El de 2 son los 2 primeros, el de 3 los 3 primeros. " if len(legs) > 2 else "")
                 + "Momio estimado de PlayDoit (cada partido multiplicado).</i>")
    return "\n\n".join(parts)


def to_alert(rows: list, sent: dict, covered=None) -> list:
    """[(horario ISO, piernas, ¿cambió?)] de los parlays listos que todavía
    no se avisaron así (sent: {horario ISO: firma avisada})."""
    by_slot = {}
    for r in rows:
        by_slot.setdefault(_ts(r["slot"]).isoformat(), []).append(r)
    out = []
    for key in sorted(by_slot):
        legs = by_slot[key]
        if len(legs) < PL.MIN_BLOCK_LEGS or not is_ready(legs, covered):
            continue
        if sent.get(key) != signature(legs):
            out.append((key, legs, key in sent))
    return out


def run_parlay_alerts(engine=None, now: datetime | None = None, send=None, load_state=None, save_state=None,
                      covered=None) -> dict:
    """Closing, después de rehacer el parlay: avisa los que ya vieron todas
    sus alineaciones (y los que cambiaron después del aviso)."""
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
    rows = pd.read_sql(text(_SQL), engine, params={"now": now, "end": now + timedelta(hours=AHEAD_HOURS)})
    rows = rows.to_dict("records")
    st = load_state(ALERT_KEY) or {}
    sent = {k: v for k, v in (st.get("sent") or {}).items()
            if (_ts(k) or now) >= now - timedelta(days=KEEP_DAYS)}
    pending = to_alert(rows, sent, covered)
    done = 0
    for key, legs, changed in pending:
        if send(alert_text(legs, tz, changed)):
            sent[key] = signature(legs)
            done += 1
        else:                                    # sin Telegram: se reintenta en el siguiente closing
            log.error(f"❌ Aviso del parlay de las {_ts(key).astimezone(tz):%H:%M} no se mandó a Telegram")
    if sent != (st.get("sent") or {}):
        save_state(ALERT_KEY, {"sent": sent}, history=False)
    slots = {_ts(r["slot"]).isoformat() for r in rows}
    waiting = len(slots) - sum(1 for s in slots
                               if is_ready([r for r in rows if _ts(r["slot"]).isoformat() == s], covered))
    print(f"🧩 Parlay listo en Telegram: {done} aviso(s)"
          + (f" · {waiting} horario(s) esperando alineaciones" if waiting else ""))
    return {"sent": done, "waiting": waiting}
