"""
src/models/parlay_alerts.py
===========================
El aviso de Telegram del parlay listo (v2.7.0, 4-nov-26). El dueño: "en los
parlays necesito que se tomen en cuenta ya que se hagan las alineaciones…"
y eligió que avise Telegram (3a).

El closing (cada 30 min) guarda las alineaciones, rehace los parlays con ellas
(snapshot_parlays) y después avisa: una vez por parlay listo y otra si cambia
antes de quedar fijo (5 min antes de su primer partido). Estado en
model_state 'telegram_parlays' ({parlay: piernas avisadas}).

v2.9.0 (6-nov-26), el parlay con bono (program_parlays). Hay dos tipos:
  - el del horario ('horario'): listo cuando todas sus piernas vieron la
    alineación (o su liga no la tiene en ESPN), como antes;
  - el del día ('dia'): sus partidos de más tarde no tienen alineación cuando
    se fija; listo cuando la vieron los de su primer horario.
Si a READY_LATE_MIN minutos de fijarse todavía no está listo, avisa igual y lo
dice: antes un parlay sin alineaciones no avisaba nunca. El mensaje trae el
momio con el bono de PlayDoit sobre la ganancia y marca el pago anticipado.
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
READY_LATE_MIN = 40          # a 40 min de su primer partido avisa aunque falten alineaciones
KEEP_DAYS = 2                # lo avisado se olvida a los 2 días
DIAS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

_SQL = """
    SELECT slot, rank, kind, seq, match, display, match_date, league, label, market, model_prob, market_prob,
           playdoit_odds, lineup, pa
    FROM program_parlays
    WHERE result IS NULL AND rule = :rule AND slot > :now AND slot <= :end
    ORDER BY slot, kind, seq, rank
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


def key_of(r: dict) -> str:
    """El parlay de una pierna: horario|tipo|número."""
    return f"{_ts(r['slot']).isoformat()}|{r.get('kind') or 'horario'}|{int(r.get('seq') or 1)}"


def first_block(legs: list) -> list:
    """Las piernas del primer horario del parlay (hasta BLOCK_MINUTES después del primero)."""
    first = min(_ts(x["match_date"]) for x in legs)
    return [x for x in legs if _ts(x["match_date"]) - first <= timedelta(minutes=PL.BLOCK_MINUTES)]


def needs(legs: list) -> list:
    """Las piernas que tienen que ver la alineación antes de avisar."""
    return first_block(legs) if (legs and legs[0].get("kind") == "dia") else legs


def is_ready(legs: list, covered=None) -> bool:
    return bool(legs) and all(PL.lineup_ready(x.get("lineup"), x.get("league"), covered) for x in needs(legs))


def signature(legs: list) -> str:
    """Lo que se avisó: cada partido con su apuesta, en orden."""
    return " | ".join(f"{x['match']}:{x['market']}" for x in sorted(legs, key=lambda x: int(x["rank"])))


def times_text(p: float) -> str:
    """La probabilidad dicha en veces, como en el dashboard (pxOdds): 0.14 →
    "más o menos 1 de cada 7 veces"; desde 2 de 3 → "70 de cada 100 veces"."""
    n = round(1 / p) if p else 0
    return f"más o menos 1 de cada {n} veces" if n > 1 else f"{round(p * 100)} de cada 100 veces"


def alert_text(legs: list, tz, changed: bool = False, late: bool = False) -> str:
    """El mensaje de un parlay: sus partidos por hora, el momio con y sin el
    bono de PlayDoit y la probabilidad de que salgan todas (Pinnacle sin
    margen). Momios americanos, como PlayDoit."""
    legs = sorted(legs, key=lambda x: int(x["rank"]))
    slot = min(_ts(x["match_date"]) for x in legs).astimezone(tz)
    fixed = slot - timedelta(minutes=PL.LOCK_MINUTES)
    kind, seq = legs[0].get("kind") or "horario", int(legs[0].get("seq") or 1)
    name = (f"Parlay del día{f' #{seq}' if seq > 1 else ''}" if kind == "dia" else f"Parlay de las {slot:%H:%M}")
    title = f"🔄 <b>Cambió el {name[0].lower() + name[1:]}</b>" if changed else f"🧩 <b>{name}</b> · {len(legs)} partidos"
    parts = [title + f"\n<i>{DIAS[slot.weekday()]} {slot.day} {MESES[slot.month - 1]} · "
                     f"apuéstalo antes de las {fixed:%H:%M} (hora de México)</i>"]
    rows = []
    for x in sorted(legs, key=lambda x: (_ts(x["match_date"]), int(x["rank"]))):
        ko = _ts(x["match_date"]).astimezone(tz)
        note = str(x.get("lineup") or "")
        extra = note.split(":", 1)[1].strip() if note.startswith(PL.LINEUP_OK) and ":" in note else ""
        seen = "📋" if note.startswith(PL.LINEUP_OK) else "⏳"
        rows.append(f"{seen} <b>{ko:%H:%M}</b> {html.escape(str(x.get('display') or x['match']))}\n"
                    f"   {html.escape(str(x.get('label') or x['market']))} · {_us(_num(x['playdoit_odds']))}"
                    + (" · ⚡" if x.get("pa") else "")
                    + (f"\n   <i>{html.escape(extra)}</i>" if extra else ""))
    parts.append("\n".join(rows))
    pay = PL.payout([_num(x["playdoit_odds"]) or 1 for x in legs])
    pk = [_num(x.get("market_prob")) for x in legs]
    p_all = math.prod(pk) if None not in pk else None
    parts.append(f"Paga <b>{_us(pay['odds'])}</b>"
                 + (f" · con el bono de {pay['bonus']:.1%}".replace(".0%", "%") + f": <b>{_us(pay['with_bonus'])}</b>"
                    if pay["bonus"] else "")
                 + (f"\nSe gana {times_text(p_all)} (según Pinnacle)" if p_all is not None else ""))
    notes = ["📋 vio la alineación · ⏳ todavía no (sale ~1 h antes)",
             "⚡ = pago anticipado: si el favorito va 2 goles arriba, PlayDoit la paga como ganada",
             "El momio de PlayDoit es estimado; el bono se suma a la ganancia (3 partidos +5%, 4 +7.5%, 5 +10%, 6 +15%)"]
    if late:
        notes.insert(0, "Faltan alineaciones del primer horario y ya casi se fija: revísalas antes de apostar")
    parts.append("<i>" + "\n".join(notes) + "</i>")
    return "\n\n".join(parts)


def to_alert(rows: list, sent: dict, covered=None, now: datetime | None = None) -> list:
    """[(parlay, piernas, ¿cambió?, ¿tarde?)] de los parlays listos (o a
    READY_LATE_MIN de fijarse) que todavía no se avisaron así (sent: {parlay: firma})."""
    now = now or datetime.now(timezone.utc)
    by = {}
    for r in rows:
        by.setdefault(key_of(r), []).append(r)
    out = []
    for key in sorted(by):
        legs = by[key]
        need = PL.DAY_MIN_LEGS if legs[0].get("kind") == "dia" else PL.SLOT_MIN_LEGS
        if len(legs) < need:
            continue
        ready = is_ready(legs, covered)
        late = not ready and _ts(legs[0]["slot"]) - now <= timedelta(minutes=READY_LATE_MIN)
        if (ready or late) and sent.get(key) != signature(legs):
            out.append((key, legs, key in sent, late))
    return out


def run_parlay_alerts(engine=None, now: datetime | None = None, send=None, load_state=None, save_state=None,
                      covered=None) -> dict:
    """Closing, después de rehacer el parlay: avisa los que ya vieron sus
    alineaciones (o están por fijarse) y los que cambiaron después del aviso."""
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
    try:
        rows = pd.read_sql(text(_SQL), engine, params={"now": now, "end": now + timedelta(hours=AHEAD_HOURS),
                                                       "rule": PL.RULE})
    except Exception as e:
        if "does not exist" in str(e):          # la columna rule nace con el primer snapshot de v2.9.0
            print("🧩 Parlay listo en Telegram: todavía no hay parlays con bono")
            return {"sent": 0, "waiting": 0}
        raise
    rows = rows.to_dict("records")
    st = load_state(ALERT_KEY) or {}
    sent = {k: v for k, v in (st.get("sent") or {}).items()
            if (_ts(k.split("|")[0]) or now) >= now - timedelta(days=KEEP_DAYS)}
    pending = to_alert(rows, sent, covered, now)
    done = 0
    for key, legs, changed, late in pending:
        if send(alert_text(legs, tz, changed, late)):
            sent[key] = signature(legs)
            done += 1
        else:                                    # sin Telegram: se reintenta en el siguiente closing
            log.error(f"❌ Aviso del parlay {key} no se mandó a Telegram")
    if sent != (st.get("sent") or {}):
        save_state(ALERT_KEY, {"sent": sent}, history=False)
    keys = {key_of(r) for r in rows}
    waiting = len(keys) - sum(1 for k in keys if is_ready([r for r in rows if key_of(r) == k], covered))
    print(f"🧩 Parlay listo en Telegram: {done} aviso(s)"
          + (f" · {waiting} parlay(s) esperando alineaciones" if waiting else ""))
    return {"sent": done, "waiting": waiting}
