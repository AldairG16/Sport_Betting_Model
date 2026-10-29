"""
src/features/clubelo.py
=======================
La fuerza de cada liga, con ClubElo (22-oct-26).

El programa arma la fuerza de cada equipo DENTRO de su liga (goles a favor y
en contra, Dixon-Coles, Elo propio). En un partido entre ligas distintas
(Champions, Europa League) eso no sirve: un gol en Noruega vale lo mismo que
uno en la Premier, y el 21-oct el programa le dio a Viking 20% contra el
Aston Villa (Pinnacle: 10%) porque Viking mete 2.3 por partido en Noruega.

ClubElo pone a todos los clubes de Europa en una sola escala de Elo (Arsenal
2049, Viking 1706, Shakhtar 1700). Aquí:
  - refresh_clubelo(): una vez por semana, el Elo de los clubes de cada país
    (las páginas públicas https://clubelo.com/<PAÍS>; su API de datos,
    api.clubelo.com, responde 502 desde el 22-oct-26 y la de partidos dice
    "Fixtures API deactivated"). Una petición por país, con pausa. Desde el
    29-oct-26 las páginas piden un reto anti-bots (403): no se salta, sigue
    la última foto (MAX_AGE_DAYS).
  - match_team(): el club de ClubElo que es cada equipo nuestro.
  - league_strengths(): la fuerza de cada liga, el Elo promedio de sus 5
    mejores clubes (Inglaterra 1965, Noruega 1642, Ucrania 1583).
  - league_factor(): cuánto repartir los goles esperados del programa por la
    diferencia entre las dos ligas, sin cambiar el total.
El pipeline lo aplica solo en partidos entre ligas distintas
(prediction_pipeline._stage_lambdas y el ensemble) y guarda el factor en
model_vs_sharp.league_factor; los picks de esos partidos solo salen con él.

Por qué la LIGA y no el Elo de cada club, y por qué amortiguado (GAMMA):
medido el 22-oct-26 en los 5 partidos de copas con goles esperados del
programa y momios de Pinnacle, la distancia del Dixon-Coles a Pinnacle fue
5.2 pts de mediana sin ajuste; con el Elo de cada club 5.6 (nada); con la
diferencia de ligas completa 12.3 (se pasa: los goles del programa ya traen
parte de la fuerza del equipo); con el 20% de esa diferencia 1.6 (media 5.7 →
2.9). Cinco partidos es poco: GAMMA es provisional y se recalibra con
model_vs_sharp.league_factor cuando haya más partidos de copas.
"""

import math
import re
import time
import unicodedata
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

BASE = "https://clubelo.com/{code}"
HEADERS = {"User-Agent": "SportBettingModel/1.0 (uso personal, una vez por semana)"}
# Países de la UEFA en ClubElo (código de su página)
COUNTRIES = ("ENG", "ESP", "ITA", "GER", "FRA", "NED", "POR", "BEL", "TUR", "GRE", "SCO", "AUT", "SUI",
             "DEN", "NOR", "SWE", "CZE", "CRO", "SRB", "UKR", "POL", "ISR", "CYP", "AZE", "KAZ", "HUN",
             "ROU", "BUL", "SVK", "SVN", "FIN", "IRL", "ISL", "NIR", "WAL", "ARM", "GEO", "BIH", "MKD",
             "ALB", "KOS", "MNE", "LUX", "LTU", "LVA", "EST", "FRO", "MLT", "MDA", "BLR", "GIB", "AND", "SMR")
CROSS_LEAGUE = ("uefa_champs", "uefa_europa", "conference")   # partidos entre ligas de Europa
# Un Elo más viejo no se usa. 29-oct-26: 10 → 45. Las páginas de ClubElo
# piden un reto anti-bots de Cloudflare ("Just a moment…", 403) y su API
# responde 502 desde el 22-oct; no se salta el reto. La fuerza de una liga
# (Elo promedio de sus 5 mejores clubes) se mueve unos pocos puntos al mes
# y pesa con GAMMA = 0.2: una foto de semanas sirve.
MAX_AGE_DAYS = 45
WARN_DAYS_LEFT = 7                                          # avisa (ERROR) una semana antes de que venza
BLOCK_PROBE = 3                                             # 403 seguidos para dar por bloqueado
ELO_HOME_ADV = 80                                           # el del ensemble (ensemble_model.ELO_HOME_ADV)
BASE_GOALS = 1.35                                           # goles por equipo de un partido promedio
TOP_N = 5                                                   # clubes que miden la fuerza de una liga
MIN_CLUBS = 3                                               # con menos, la liga no tiene fuerza medida
GAMMA = 0.2                                                 # fracción de la diferencia de ligas (provisional)

DDL = """
    CREATE TABLE IF NOT EXISTS club_elo (
        fetched_on DATE NOT NULL, country TEXT NOT NULL, club TEXT NOT NULL, slug TEXT NOT NULL,
        elo NUMERIC NOT NULL, PRIMARY KEY (fetched_on, slug)
    )
"""

_ROW = re.compile(r"alt=\"([A-Z]{2,3})\"[^']*?<small>\s*\d+\s*</small><a href=\"/([^\"]+)\">([^<]+)<span[^']*?</a></td>', '(\d+)'")
_STOP = {"fc", "cf", "sc", "fk", "ac", "afc", "sk", "club", "de", "cd", "ss", "as", "us", "rc", "sv", "if", "bk",
         "nk", "hnk", "gnk", "pfc", "kv", "krc", "rsc", "sl", "ud", "real", "the", "1"}
# nuestro nombre canónico → slug de ClubElo, cuando las palabras no alcanzan
# ("inter milan" también tiene "milan"; "bod/glimt" perdió la ø)
SLUG_ALIAS = {"inter milan": "Inter", "bod/glimt": "BodoeGlimt", "bodo/glimt": "BodoeGlimt"}
_ALIAS = {"man": "manchester", "utd": "united", "internazionale": "inter", "atletico": "atletico",
          "psg": "paris", "saint": "st", "munchen": "munchen", "bayern": "bayern"}


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def parse_country(html: str, code: str | None = None) -> list:
    """[{"country", "club", "slug", "elo"}] de la página de un país."""
    i = html.find("const eloData")
    j = html.find("];", i)
    if i < 0 or j < 0:
        return []
    out = []
    for country, slug, club, elo in _ROW.findall(html[i:j]):
        if code and country != code:
            continue
        out.append({"country": country, "club": club.strip(), "slug": slug.strip(), "elo": float(elo)})
    return out


def _tokens(name: str) -> set:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("/", " "))
    return {_ALIAS.get(t, t) for t in s.split() if t and t not in _STOP}


def match_team(name: str, clubs: list) -> dict | None:
    """El club de ClubElo que es nuestro equipo (nombre canónico): mismas
    palabras, o las de uno contenidas en las del otro. Sin un único
    candidato, None (mejor no ajustar que ajustar con el club equivocado)."""
    alias = SLUG_ALIAS.get(str(name).lower())
    if alias:
        return next((c for c in clubs if c["slug"] == alias), None)
    mine = _tokens(name)
    if not mine:
        return None
    exact = [c for c in clubs if _tokens(c["club"]) == mine or _tokens(c["slug"]) == mine]
    if len(exact) == 1:
        return exact[0]
    cands = [c for c in clubs if (_tokens(c["club"]) and (_tokens(c["club"]) <= mine or mine <= _tokens(c["club"])))
             or (_tokens(c["slug"]) and (_tokens(c["slug"]) <= mine or mine <= _tokens(c["slug"])))]
    uniq = {c["slug"]: c for c in cands}
    return next(iter(uniq.values())) if len(uniq) == 1 else None


def elo_expectancy(elo_home: float, elo_away: float, home_adv: float = ELO_HOME_ADV) -> float:
    """Resultado esperado del local (ganar = 1, empatar = 0.5) por la diferencia de Elo."""
    return 1.0 / (1.0 + 10 ** (-(elo_home + home_adv - elo_away) / 400.0))


def _poisson_expectancy(lh: float, la: float, max_goals: int = 12) -> float:
    k = np.arange(max_goals + 1)
    ph = np.exp(-lh) * lh ** k / np.array([math.factorial(int(x)) for x in k])
    pa = np.exp(-la) * la ** k / np.array([math.factorial(int(x)) for x in k])
    m = np.outer(ph, pa)
    i, j = np.indices(m.shape)
    return float(m[i > j].sum() + 0.5 * m[i == j].sum())


def side_factor(lam_home: float, lam_away: float, elo_home: float, elo_away: float) -> float:
    """f tal que con (λ_local·f, λ_visita/f) el resultado esperado del local
    es el que da la diferencia de Elo. El total de goles casi no cambia."""
    target = elo_expectancy(elo_home, elo_away)
    lo, hi = 0.2, 5.0
    for _ in range(60):
        f = math.sqrt(lo * hi)
        if _poisson_expectancy(lam_home * f, lam_away / f) < target:
            lo = f
        else:
            hi = f
    return math.sqrt(lo * hi)


def league_strengths(clubs: list, top: int = TOP_N, min_clubs: int = MIN_CLUBS) -> dict:
    """{país: Elo promedio de sus `top` mejores clubes}. Un país con menos de
    min_clubs clubes en ClubElo no tiene fuerza medida."""
    by = {}
    for c in clubs:
        by.setdefault(c["country"], []).append(float(c["elo"]))
    return {k: float(np.mean(sorted(v, reverse=True)[:top])) for k, v in by.items() if len(v) >= min_clubs}


def league_factor(s_home: float, s_away: float, gamma: float = GAMMA) -> float:
    """f para (λ_local·f, λ_visita/f) por la diferencia de fuerza entre las
    dos ligas: el reparto que cuadra un partido promedio con esa diferencia
    de Elo (sin ventaja local: la de cada equipo ya está en sus goles
    esperados), elevado a gamma. Misma liga: 1."""
    if s_home == s_away:
        return 1.0
    return side_factor(BASE_GOALS, BASE_GOALS, s_home - ELO_HOME_ADV, s_away) ** gamma


def team_strengths(teams, clubs: list) -> dict:
    """{equipo: fuerza de la liga de su club de ClubElo}, de los equipos que
    se pueden emparejar y cuya liga tiene fuerza medida."""
    S = league_strengths(clubs)
    out = {}
    for t in teams:
        c = match_team(t, clubs)
        if c and c["country"] in S:
            out[t] = S[c["country"]]
    return out


def cross_league(league) -> bool:
    return any(k in str(league or "").lower() for k in CROSS_LEAGUE)


# ─────────────────────────────────────────────────────────────────────────────
# Red y base
# ─────────────────────────────────────────────────────────────────────────────

def _get(url: str) -> str | None:
    import urllib.request
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("latin-1")


def refresh_clubelo(engine=None, get=None, pause: float = 2.0, countries=COUNTRIES, last=None) -> int:
    """Guarda el Elo de hoy de los clubes de cada país. Un país que falla se
    salta. Si no se pudo leer ninguno: con la foto anterior (`last`) todavía
    útil por más de WARN_DAYS_LEFT días es WARNING; si vence antes o no hay,
    ERROR (llega a Telegram). Si los primeros BLOCK_PROBE países responden
    403, ClubElo está bloqueando las lecturas y no se piden los demás."""
    if engine is None:
        from config.database import engine
    get = get or _get
    today = date.today()
    rows, failed, blocked = [], [], 0
    for k, code in enumerate(countries):
        try:
            rows += parse_country(get(BASE.format(code=code)) or "", code)
        except Exception as e:
            status = getattr(e, "code", None)
            failed.append(f"{code} ({type(e).__name__}{f' {status}' if status else ''})")
            blocked += status == 403
        if not rows and blocked >= BLOCK_PROBE:
            break                                   # reto anti-bots (29-oct-26): no se insiste
        if pause and k < len(countries) - 1:
            time.sleep(pause)
    if not rows:
        why = (f"ClubElo bloquea las lecturas automáticas ({', '.join(failed[:BLOCK_PROBE])})"
               if blocked >= BLOCK_PROBE else f"no se pudo leer ningún país ({', '.join(failed[:5])}…)")
        _report_stale(why, last, today)
        return 0
    with engine.begin() as conn:
        conn.execute(text(DDL))
        conn.execute(text("""INSERT INTO club_elo (fetched_on, country, club, slug, elo)
                             VALUES (:d, :country, :club, :slug, :elo)
                             ON CONFLICT (fetched_on, slug) DO UPDATE SET
                                 elo = EXCLUDED.elo, club = EXCLUDED.club, country = EXCLUDED.country"""),
                     [{**r, "d": today} for r in rows])
    print(f"🌍 ClubElo: {len(rows)} clubes de {len(countries) - len(failed)} países"
          + (f" (sin leer: {', '.join(failed)})" if failed else ""))
    return len(rows)


def _report_stale(why: str, last, today: date) -> None:
    """Sin foto nueva: WARNING si la anterior sirve todavía más de
    WARN_DAYS_LEFT días; si no, ERROR, con tiempo para buscar otra fuente."""
    if last is not None and not pd.isna(last):
        expires = pd.Timestamp(last).date() + timedelta(days=MAX_AGE_DAYS)
        left = (expires - today).days
        if left > WARN_DAYS_LEFT:
            log.warning(f"⚠️  ClubElo: {why} — sigue la foto del {pd.Timestamp(last):%d-%b}, "
                        f"que sirve hasta el {expires:%d-%b}")
            return
        if left >= 0:
            log.error(f"❌ ClubElo: {why} — la foto del {pd.Timestamp(last):%d-%b} vence el {expires:%d-%b}: "
                      f"desde ahí los partidos entre ligas se quedan sin ajuste")
            return
    log.error(f"❌ ClubElo: {why} — los partidos entre ligas se quedan sin ajuste")


def refresh_if_stale(engine=None, max_age_days: int = 6) -> int:
    """La foto de ClubElo se renueva una vez por semana (el Elo de la liga
    cambia poco de un día a otro): solo si la última tiene más de max_age_days."""
    if engine is None:
        from config.database import engine
    try:
        last = pd.read_sql(text("SELECT MAX(fetched_on) AS d FROM club_elo"), engine).iloc[0]["d"]
    except Exception:
        last = None                                 # la tabla todavía no existe
    if last is not None and not pd.isna(last) and (date.today() - pd.Timestamp(last).date()).days <= max_age_days:
        print(f"🌍 ClubElo al día (foto del {pd.Timestamp(last):%d-%b})")
        return 0
    return refresh_clubelo(engine, last=last)


def load_latest(engine=None) -> list:
    """Los clubes de la foto más reciente (con su fecha, fetched_on), si no
    tiene más de MAX_AGE_DAYS."""
    if engine is None:
        from config.database import engine
    try:
        df = pd.read_sql(text(f"""
            SELECT fetched_on, country, club, slug, elo FROM club_elo
            WHERE fetched_on = (SELECT MAX(fetched_on) FROM club_elo)
              AND fetched_on >= CURRENT_DATE - {MAX_AGE_DAYS}
        """), engine)
    except Exception:
        return []                                   # la tabla nace con la primera lectura
    return df.to_dict("records")
