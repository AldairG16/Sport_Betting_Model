"""
scripts/backfill_espn_results.py
================================
Backfill de resultados desde la API pública de ESPN (gratis, sin key).

Cubre las ligas que football-data.co.uk NO tiene y que dejaron bets
'stale' (Brasil, Liga MX, Libertadores, Champions, Suecia, MLS, China,
Japón, Noruega, Escocia, Eredivisie, Argentina...).

Para cada bet stale cuyo mercado solo necesita GOLES (over/under, 1X2,
BTTS, DNB, DC, AH) o goles HT (h1_*/h2_*):
  1. Consulta el scoreboard de ESPN de esa liga ±1 día
  2. Matchea equipos por nombre normalizado + similitud
  3. Inserta el resultado en matches (con HT si ESPN lo trae)
  4. Re-ejecuta el resolvedor estándar

Los mercados de córners/tarjetas siguen sin fuente — esas quedan stale.

Costo: 0 créditos de odds API. ~1 request HTTP por liga-fecha.
"""

import sys
import unicodedata
import urllib.request
from datetime import timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"

# sport_key (The Odds API) → slug ESPN
ESPN_LEAGUES = {
    "soccer_epl":                            "eng.1",
    "soccer_efl_champ":                      "eng.2",
    "soccer_spain_la_liga":                  "esp.1",
    "soccer_germany_bundesliga":              "ger.1",
    "soccer_italy_serie_a":                   "ita.1",
    "soccer_france_ligue_one":                "fra.1",
    "soccer_netherlands_eredivisie":          "ned.1",
    "soccer_belgium_first_div":               "bel.1",
    "soccer_portugal_primeira_liga":          "por.1",
    "soccer_turkey_super_league":             "tur.1",
    "soccer_greece_super_league":             "gre.1",
    "soccer_spl":                             "sco.1",
    "soccer_sweden_allsvenskan":              "swe.1",
    "soccer_norway_eliteserien":              "nor.1",
    "soccer_mexico_ligamx":                   "mex.1",
    "soccer_brazil_campeonato":               "bra.1",
    "soccer_argentina_primera_division":      "arg.1",
    "soccer_usa_mls":                         "usa.1",
    "soccer_china_superleague":               "chi.1",
    "soccer_japan_j_league":                  "jpn.1",
    "soccer_korea_kleague1":                  "kor.1",
    "soccer_uefa_champs_league":              "uefa.champions",
    "soccer_uefa_europa_league":              "uefa.europa",
    "soccer_conmebol_copa_libertadores":      "conmebol.libertadores",
}


def _clean(name: str) -> str:
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    n = n.lower().replace("'", "").replace(".", "").replace("-", " ")
    return " ".join(n.split())


def _similarity(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def fetch_espn_scoreboard(slug: str, date: pd.Timestamp) -> list[dict]:
    """Scoreboard de ESPN para una fecha. Devuelve partidos finalizados."""
    url = f"{ESPN_BASE.format(slug=slug)}?dates={date.strftime('%Y%m%d')}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = __import__("json").loads(r.read())
    except Exception:
        return []
    out = []
    for ev in data.get("events", []):
        comp = ev.get("competitions", [{}])[0]
        status = (comp.get("status", {}) or {}).get("type", {}).get("name", "")
        if status != "STATUS_FULL_TIME":
            continue
        teams = {}
        for c in comp.get("competitors", []):
            ls = c.get("linescores") or []
            ht = None
            if len(ls) >= 1 and isinstance(ls[0], dict):
                ht = ls[0].get("value")
            teams[c.get("homeAway")] = {
                "name": c.get("team", {}).get("displayName", ""),
                "score": c.get("score"),
                "ht": ht,
            }
        if "home" in teams and "away" in teams:
            out.append({
                "home": teams["home"]["name"], "away": teams["away"]["name"],
                "home_goals": int(teams["home"]["score"]),
                "away_goals": int(teams["away"]["score"]),
                "home_goals_ht": teams["home"]["ht"],
                "away_goals_ht": teams["away"]["ht"],
                "date": str(ev.get("date", ""))[:10],
            })
    return out


def backfill_espn_results(verbose: bool = True) -> dict:
    # Stale bets cuyos mercados se pueden resolver con goles (+HT)
    bets = pd.read_sql(text("""
        SELECT id, match, market, match_date, league
        FROM bets_history
        WHERE result = 'stale'
        ORDER BY league, match_date
    """), engine)
    if bets.empty:
        print("Sin stale.")
        return {}

    stats = {"backfilled": 0, "matched": 0, "no_espn_league": 0, "not_found": 0, "errors": 0}
    # Cache: (liga, fecha) → partidos ESPN (evita repetir requests)
    board_cache: dict = {}

    with engine.begin() as conn:
        for _, b in bets.iterrows():
            league = str(b["league"] or "")
            slug = ESPN_LEAGUES.get(league)
            if not slug:
                stats["no_espn_league"] += 1
                continue
            try:
                home_raw, away_raw = str(b["match"]).split(" vs ")
            except ValueError:
                continue
            home_n, away_n = _clean(home_raw), _clean(away_raw)
            md = pd.to_datetime(b["match_date"])

            found = None
            for delta in (0, -1, 1):
                d = md + timedelta(days=delta)
                key = (slug, d.strftime("%Y%m%d"))
                if key not in board_cache:
                    board_cache[key] = fetch_espn_scoreboard(slug, d)
                for m in board_cache[key]:
                    eh, ea = _clean(m["home"]), _clean(m["away"])
                    ok_h = _similarity(home_n, eh) > 0.62 or home_n in eh or eh in home_n
                    ok_a = _similarity(away_n, ea) > 0.62 or away_n in ea or ea in away_n
                    if ok_h and ok_a:
                        found = (m, d)
                        break
                if found:
                    break

            if not found:
                stats["not_found"] += 1
                continue
            m, d = found
            stats["matched"] += 1

            # Upsert en matches (misma política que el resto del sistema)
            league_col = league if league else None
            r = conn.execute(text("""
                INSERT INTO matches (date, league, season, home_team, away_team,
                                     home_goals, away_goals,
                                     home_goals_ht, away_goals_ht)
                VALUES (:d, :lg, :season, :h, :a, :hg, :ag, :hht, :aht)
                ON CONFLICT (date, home_team, away_team) DO UPDATE SET
                    home_goals    = COALESCE(matches.home_goals,    EXCLUDED.home_goals),
                    away_goals    = COALESCE(matches.away_goals,    EXCLUDED.away_goals),
                    home_goals_ht = COALESCE(matches.home_goals_ht, EXCLUDED.home_goals_ht),
                    away_goals_ht = COALESCE(matches.away_goals_ht, EXCLUDED.away_goals_ht)
            """), {
                "d": d.strftime("%Y-%m-%d"), "lg": league_col,
                "season": d.year if d.month >= 8 else d.year - 1,
                "h": home_raw, "a": away_raw,
                "hg": m["home_goals"], "ag": m["away_goals"],
                "hht": m["home_goals_ht"], "aht": m["away_goals_ht"],
            })
            stats["backfilled"] += 1
            if verbose:
                ht = f" (HT {m['home_goals_ht']}-{m['away_goals_ht']})" if m["home_goals_ht"] is not None else ""
                print(f"   ✓ {home_raw} vs {away_raw} [{d.date()}] "
                      f"{m['home_goals']}-{m['away_goals']}{ht}")

    if verbose:
        print(f"\nESPN backfill: {stats['backfilled']} partidos insertados "
              f"({stats['matched']} matcheados, {stats['not_found']} no encontrados, "
              f"{stats['no_espn_league']} sin liga ESPN)")

    if stats["backfilled"]:
        from scripts.resolve_stale_bets import resolve_stale_bets
        resolve_stale_bets(apply=True, verbose=True)

    return stats


if __name__ == "__main__":
    backfill_espn_results()
