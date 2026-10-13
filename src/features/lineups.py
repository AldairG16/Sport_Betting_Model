"""
src/features/lineups.py
=======================
GUARDIA DE ALINEACIONES via API-Football (plan gratuito: 100 req/día).

Idea profesional: ~80% del movimiento real de cuotas ocurre cuando salen
las alineaciones (~40 min antes del partido). Si el goleador estelar de un
equipo NO está en el once confirmado, las apuestas de ese lado pierden su
base — hay que cancelarlas, no apostarlas a ciegas.

Flujo (lo llama revalidate_pending_bets en el closing horario):
  1. Para bets con kickoff inminente, busca el fixture del día en
     API-Football (matcheo de nombres por similitud).
  2. Descarga los onces confirmados.
  3. Compara contra los goleadores top del equipo (match_events, 0 coste).
  4. Si falta un goleador de élite → cancela la bet de ese lado.

Defensivo total: sin API_FOOTBALL_KEY, sin alineación publicada o ante
cualquier error → no-op. Nunca rompe el closing.

Pero no callado (13-oct-26): la API contesta 200 con el motivo en "errors"
(clave inválida, cuota del día agotada, temporada fuera del plan gratuito)
y el filtro quedaba apagado sin que nadie lo supiera. Ahora ese motivo se
registra, y self_check() lo revisa una vez al día desde el closing.
"""

import json
import unicodedata
import urllib.request
from datetime import datetime, timezone

from src.utils.log import get_logger

log = get_logger(__name__)

BASE = "https://v3.football.api-sports.io"


def _norm(name: str) -> str:
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return " ".join(n.lower().replace("'", "").split())


def api_errors(data) -> str | None:
    """El motivo que API-Football pone en "errors" (dict o lista), o None."""
    errs = (data or {}).get("errors") if isinstance(data, dict) else None
    if not errs:
        return None
    if isinstance(errs, dict):
        return "; ".join(f"{k}: {v}" for k, v in errs.items())
    return "; ".join(str(e) for e in errs)


def _fetch(url: str) -> tuple[dict | None, str | None]:
    """(respuesta, motivo del fallo). Sin clave: (None, None)."""
    import os
    key = os.environ.get("API_FOOTBALL_KEY", "")
    if not key:
        return None, None
    try:
        req = urllib.request.Request(url, headers={"x-apisports-key": key})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"
    return data, api_errors(data)


def _get(url: str) -> dict | None:
    data, err = _fetch(url)
    if err:
        # WARNING: el chequeo diario (self_check) es el que avisa a Telegram
        log.warning(f"⚠️  API-Football no respondió datos: {err}")
        return None
    return data


def find_fixture(kickoff_utc: datetime, home: str, away: str) -> int | None:
    """Busca el fixture de API-Football para esta fecha y equipos."""
    data = _get(f"{BASE}/fixtures?date={kickoff_utc.strftime('%Y-%m-%d')}")
    if not data:
        return None
    hn, an = _norm(home), _norm(away)
    best, best_score = None, 0.0
    import difflib
    for fx in data.get("response", []):
        teams = fx.get("teams", {})
        fh = _norm(teams.get("home", {}).get("name", ""))
        fa = _norm(teams.get("away", {}).get("name", ""))
        score = difflib.SequenceMatcher(None, hn, fh).ratio() + \
                difflib.SequenceMatcher(None, an, fa).ratio()
        if score > best_score:
            best, best_score = fx.get("fixture", {}).get("id"), score
    if best and best_score > 1.1:   # umbral de similitud conjunta
        return best
    return None


def get_lineup_surnames(fixture_id: int) -> dict[str, set[str]] | None:
    """Surnios (última palabra) de los 11 titulares por equipo. None si no
    hay alineación publicada aún."""
    data = _get(f"{BASE}/fixtures/lineups?fixture={fixture_id}")
    if not data or not data.get("response"):
        return None
    out: dict[str, set[str]] = {}
    for team in data["response"]:
        players = team.get("startXI", []) or []
        surnames = set()
        for p in players:
            name = _norm(p.get("player", {}).get("name", ""))
            if name:
                surnames.add(name.split()[-1])
        out[_norm(team.get("team", {}).get("name", ""))] = surnames
    return out or None


def self_check(today: datetime | None = None) -> dict:
    """
    ¿Funciona la API de alineaciones? /status no gasta cuota; los partidos
    de hoy gastan 1 de las 100 consultas diarias del plan gratuito y prueban
    lo que el filtro necesita de verdad (la temporada actual).
    """
    import os
    if not os.environ.get("API_FOOTBALL_KEY"):
        return {"ok": False, "error": "sin API_FOOTBALL_KEY"}
    day = (today or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    status, err = _fetch(f"{BASE}/status")
    if err or not status:
        return {"ok": False, "error": err or "sin respuesta en /status"}
    resp = status.get("response") or {}
    resp = resp if isinstance(resp, dict) else {}
    plan = (resp.get("subscription") or {}).get("plan")
    req = resp.get("requests") or {}
    fixtures, err = _fetch(f"{BASE}/fixtures?date={day}")
    n = len((fixtures or {}).get("response") or [])
    out = {"ok": err is None and n > 0, "plan": plan, "fixtures_today": n,
           "requests": f"{req.get('current')}/{req.get('limit_day')}", "day": day}
    if err:
        out["error"] = err
    elif n == 0:
        out["error"] = f"0 partidos el {day}: el plan no devuelve la temporada actual"
    return out


def star_missing(lineup_surnames: set[str], top_scorers: list[str]) -> str | None:
    """Devuelve el nombre del goleador de élite ausente, o None.

    top_scorers: nombres completos (ya normalizados) ordenados por tasa.
    Un goleador 'está' si su apellido aparece entre los titulares.
    """
    for scorer in top_scorers:
        surname = _norm(scorer).split()[-1]
        if surname not in lineup_surnames:
            return scorer
    return None
