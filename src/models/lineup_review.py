"""
src/models/lineup_review.py
===========================
Revisión con Claude de cada alerta de alineación (20-oct-26, aprobado por el
dueño con un tope de 50 centavos de dólar por semana).

Antes de mandar la alerta, Claude busca noticias (2 búsquedas web como máximo)
y dice si las bajas son lesión o sanción (el equipo sale de verdad debilitado)
o descanso planeado (otro partido importante, ya clasificado, suplentes de
nivel), que es lo que la casa probablemente ya cuenta. El veredicto va en el
mensaje y queda guardado en lineup_alerts: el weekly mide si las alertas que
Claude confirmó rindieron más que las que descartó. Si no sirve, se apaga.

Costo: Claude Haiku 5.5 ($0.10 / $0.50 por millón de tokens) + $0.01 por
búsqueda; ~2-3 centavos por revisión, y la mayor parte son las búsquedas. Dos
topes: el semanal de este módulo (LINEUP_REVIEW_WEEKLY_USD, 0.50, contado en
lineup_alerts) y el diario de toda la cuenta (anthropic_budget.can_call). Sin
tope disponible, sin clave o con error, la alerta sale igual, sin revisión.
"""

import json
import os
from datetime import datetime, timezone

from sqlalchemy import text

from config.settings import env_float
from src.utils.log import get_logger

log = get_logger(__name__)

MODEL = "claude-haiku-5-5"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
EFFORT = "medium"
MAX_TOKENS = 4000
EST_COST_USD = 0.03
WEEKLY_CAP_USD = env_float("LINEUP_REVIEW_WEEKLY_USD", 0.50)
VERDICTS = ("CONFIRMA", "DUDA", "DESCARTA")

SYSTEM = """\
Eres un analista de fútbol que revisa alertas de alineación para un apostador en México.
La fecha de hoy es {today}. Tus datos de entrenamiento terminan antes de hoy: plantillas,
lesiones y calendarios pueden haber cambiado, así que busca noticias de este partido antes
de responder.

Te doy un partido que arranca en menos de una hora y los titulares habituales que NO salen
en la alineación del equipo (o los equipos) con bajas. Decide si sale de verdad debilitado:
- CONFIRMA: las bajas son por lesión, sanción u otra causa que debilita al equipo, y no hay
  noticias que indiquen que el rival no lo aprovechará.
- DESCARTA: las bajas son descanso planeado (otro partido más importante cerca, ya
  clasificado o sin nada en juego) con suplentes de nivel parecido, o hay noticias que hacen
  la alerta engañosa.
- DUDA: no encontraste información suficiente para decidir.

Responde SOLO con un objeto JSON, sin texto antes ni después:
{{"veredicto": "CONFIRMA" | "DUDA" | "DESCARTA", "bajas": "lesion" | "rotacion" | "mixto" | "desconocido",
 "motivo": "<una frase en español, máximo 200 caracteres>"}}"""


def _user_message(group: list) -> str:
    """Las alertas de UN partido (23-oct-26: una revisión por partido, no por
    equipo); puede haber bajas en los dos equipos."""
    a = group[0]
    lines = [f"Partido: {a['home']} (local) vs {a['away']} (visitante), {a['sport_key']}, "
             f"inicio {a['kickoff']} UTC."]
    seen = set()
    for x in group:
        if x["weakened"] in seen:
            continue
        seen.add(x["weakened"])
        rival = x["away"] if x["weakened"] == x["home"] else x["home"]
        lines.append(f"Equipo con bajas: {x['weakened']} (rival: {rival}). Titulares habituales que no "
                     f"salen: {', '.join(x['missing']) or 'ninguno'}.")
        if x.get("top_scorer_out"):
            lines.append(f"Entre ellos su goleador: {x['top_scorer_out']}.")
    for x in group:
        lines.append(f"Pinnacle le da {x['pin_prob'] * 100:.0f}% a '{x['market']}'.")
    return "\n".join(lines)


def extract_json(raw: str) -> dict | None:
    """El primer objeto JSON balanceado del texto (Claude a veces agrega prosa)."""
    if not raw:
        return None
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        start = raw.find("{", start + 1)
    return None


def weekly_spent(engine) -> float | None:
    """Lo gastado en revisiones en los últimos 7 días (None si no se puede leer)."""
    try:
        with engine.connect() as conn:
            if conn.execute(text("SELECT to_regclass('lineup_alerts')")).scalar() is None:
                return 0.0
            v = conn.execute(text("""
                SELECT COALESCE(SUM(analyst_cost), 0) FROM lineup_alerts
                WHERE alerted_at >= NOW() - INTERVAL '7 days'
            """)).scalar()
        return float(v or 0.0)
    except Exception as e:
        log.error(f"❌ Revisión de alineación: gasto semanal ilegible: {type(e).__name__}: {e}")
        return None


def review_alert(group: list, engine=None, client=None, now=None) -> dict | None:
    """
    {"verdict", "absences", "reason", "cost"} para las alertas de un partido
    (uno o los dos equipos), o None (sin tope, sin clave, error o sin respuesta
    usable).
    """
    from src.utils.anthropic_budget import (can_call, estimate_call_cost,
                                            extract_usage_from_response, record_call)
    if engine is None:
        from config.database import engine
    spent = weekly_spent(engine)
    if spent is None or spent + EST_COST_USD > WEEKLY_CAP_USD:
        print(f"🔎 Revisión de alineación omitida: tope semanal (${spent or 0:.2f} de ${WEEKLY_CAP_USD:.2f})")
        return None
    ok, why = can_call(engine, estimate_call_cost(MODEL, 3000, 800, web_searches=2))
    if not ok:
        print(f"🔎 Revisión de alineación omitida: {why}")
        return None
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.error("❌ Revisión de alineación: falta ANTHROPIC_API_KEY — la alerta sale sin revisión")
            return None
        import anthropic
        client = anthropic.Anthropic()
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM.format(today=today),
            output_config={"effort": EFFORT},
            tools=[dict(WEB_SEARCH_TOOL)],
            messages=[{"role": "user", "content": _user_message(group)}],
        )
    except Exception as e:
        log.error(f"❌ Revisión de alineación: Claude falló: {type(e).__name__}: {str(e)[:160]}")
        return None
    u = extract_usage_from_response(resp)
    cost = record_call(engine, input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                       model=MODEL, web_searches=u["web_searches"],
                       cache_read_tokens=u["cache_read_tokens"])
    if resp.stop_reason in ("refusal", "pause_turn", "max_tokens"):
        print(f"🔎 Revisión de alineación sin respuesta usable (stop={resp.stop_reason})")
        return {"verdict": None, "absences": None, "reason": None, "cost": cost}
    raw = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    parsed = extract_json(raw) or {}
    verdict = str(parsed.get("veredicto", "")).upper()
    if verdict not in VERDICTS:
        print(f"🔎 Revisión de alineación ilegible: {raw[:160]!r}")
        return {"verdict": None, "absences": None, "reason": None, "cost": cost}
    return {"verdict": verdict, "absences": parsed.get("bajas"),
            "reason": str(parsed.get("motivo") or "")[:220], "cost": cost}


def review_line(review: dict | None) -> str:
    """Renglón del mensaje de Telegram."""
    if not review or not review.get("verdict"):
        return ""
    head = {"CONFIRMA": "🔎 Claude: <b>confirma las bajas</b>",
            "DUDA": "🔎 Claude: <b>sin información clara</b>",
            "DESCARTA": "🔎 Claude: <b>no apuestes al rival</b>"}[review["verdict"]]
    return f"{head} — {review['reason']}" if review.get("reason") else head
