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
"""

import unicodedata
import urllib.request
from datetime import datetime

BASE = "https://v3.football.api-sports.io"


def _norm(name: str) -> str:
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return " ".join(n.lower().replace("'", "").split())


def _get(url: str) -> dict | None:
    import os
    key = os.environ.get("API_FOOTBALL_KEY", "")
    if not key:
        return None
    try:
        req = urllib.request.Request(url, headers={"x-apisports-key": key})
        with urllib.request.urlopen(req, timeout=12) as r:
            return __import__("json").loads(r.read())
    except Exception:
        return None


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
