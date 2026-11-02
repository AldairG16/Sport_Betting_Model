"""
src/features/espn_stats.py
==========================
Tiros, tiros a puerta y córners de ESPN para las ligas que football-data no
publica (2-nov-26).

El dueño: "arranca con los tiros y córners de ESPN". En `matches` la MLS no
tenía un solo tiro en 512 partidos del último año, Argentina el 5%, y Brasil
y México solo ceros (0 tiros, 0 a puerta y 0 córners de los dos equipos, de
un cargador viejo). Suecia, Noruega, Japón y China, nada. Sin tiros el
modelo no tiene xG estimado, tiros ni córners en esas ligas, y ahí el 🎯
perdía: MLS 8 de 23 (−10.03u), Argentina −2.30u, México −1.35u, contra +5u
de la EPL y de la Serie A, que sí los tienen.

ESPN los publica en su scoreboard público (sin llave, una petición por liga
y mes): totalShots, shotsOnTarget y wonCorners de cada equipo. Se completan
las filas de `matches` sin tiros (NULL o puros ceros): el partido de ESPN se
empareja con su fila por fecha ±1 día, mismo marcador y los dos nombres
parecidos (espn_lineups.pair_score); si dos filas se parecen casi igual no
se adivina. Un dato que ya existe nunca se pisa, y cada fila que se cambia
se copia antes a matches_identity_backup.

xg_proxy, shots_stats y corners_stats leen `matches`: con estas filas el xG
estimado, los tiros y los córners se encienden solos en esas ligas
(leagues_with_shots se mide con los datos).

Uso:
    python -m src.features.espn_stats --months 26            # en seco
    python -m src.features.espn_stats --months 26 --apply    # la historia, una vez
"""

import argparse
import time
from datetime import timedelta

import pandas as pd
from sqlalchemy import text

from src.features.espn_history import _int, months_back
from src.utils.log import get_logger

log = get_logger(__name__)

# las ligas que predecimos sin tiros en football-data (sport_key de The Odds API)
STATS_LEAGUES = ("soccer_usa_mls", "soccer_argentina_primera_division", "soccer_brazil_campeonato",
                 "soccer_mexico_ligamx", "soccer_sweden_allsvenskan", "soccer_norway_eliteserien",
                 "soccer_japan_j_league", "soccer_china_superleague")
DAILY_MONTHS = 2                 # el evening: el mes en curso y el anterior
HISTORY_MONTHS = 26              # la carga inicial: dos temporadas y un poco más
MIN_EVENTS_TO_JUDGE = 20         # con tantos partidos con datos y ninguno emparejado, se rompió el emparejamiento
STATS = ("shots", "shots_target", "corners")


def _slugs() -> dict:
    from scripts.backfill_espn_results import ESPN_LEAGUES
    return {k: ESPN_LEAGUES[k] for k in STATS_LEAGUES}


def _day(kickoff):
    t = pd.Timestamp(kickoff)
    return (t.tz_convert("UTC") if t.tzinfo else t).date()


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def parse_stats(data: dict | None) -> list:
    """Partidos terminados de un scoreboard con marcador, tiros y tiros a
    puerta de los dos equipos; córners si vienen. Sin tiros de los dos (o con
    0 y 0, que es como ESPN marca "sin estadística"), el partido no va."""
    out = []
    for ev in (data or {}).get("events", []):
        c = (ev.get("competitions") or [{}])[0]
        if not c.get("status", {}).get("type", {}).get("completed"):
            continue
        side = {t.get("homeAway"): t for t in c.get("competitors", [])}
        if "home" not in side or "away" not in side:
            continue
        row = {"espn_id": str(ev.get("id")), "kickoff": ev.get("date")}
        for s in ("home", "away"):
            t = side[s]
            stats = {x.get("name"): x.get("displayValue") for x in t.get("statistics", []) or []}
            score = t.get("score")
            row.update({f"{s}_name": t.get("team", {}).get("displayName", ""),
                        f"{s}_goals": _int(score.get("value") if isinstance(score, dict) else score),
                        f"{s}_shots": _int(stats.get("totalShots")),
                        f"{s}_shots_target": _int(stats.get("shotsOnTarget")),
                        f"{s}_corners": _int(stats.get("wonCorners"))})
        need = [row[f"{s}_{k}"] for s in ("home", "away") for k in ("goals", "shots", "shots_target")]
        if any(v is None for v in need) or row["home_shots"] + row["away_shots"] == 0:
            continue
        if row["home_shots_target"] > row["home_shots"] or row["away_shots_target"] > row["away_shots"]:
            continue                                   # dato roto
        out.append(row)
    return out


def pair_rows(events: list, matches: list) -> list:
    """[(id de matches, partido de ESPN)]. matches: filas de la liga {id,
    date, h, a, home_goals, away_goals}. Misma fecha ±1 día, mismo marcador
    y los dos nombres parecidos (pair_score); si dos filas se parecen casi
    igual (menos de 0.3) no se adivina, y cada fila va con un solo partido."""
    from src.features.espn_lineups import pair_score
    from src.utils.team_normalizer import normalize_team
    by_score = {}
    for m in matches:
        if m.get("home_goals") is None or pd.isna(m["home_goals"]):
            continue
        by_score.setdefault((int(m["home_goals"]), int(m["away_goals"])), []).append(m)
    out, used = [], set()
    for ev in events:
        day = _day(ev["kickoff"])
        h, a = normalize_team(ev["home_name"]), normalize_team(ev["away_name"])
        scored = sorted(((sc, m["id"]) for m in by_score.get((ev["home_goals"], ev["away_goals"]), [])
                         if abs((pd.Timestamp(m["date"]).date() - day).days) <= 1
                         for sc in [pair_score(h, a, m["h"], m["a"])] if sc is not None), reverse=True)
        if not scored or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.3) or scored[0][1] in used:
            continue
        used.add(scored[0][1])
        out.append((scored[0][1], ev))
    return out


def _empty(v) -> bool:
    return v is None or pd.isna(v) or int(v) == 0


def _int_or_none(v):
    """Para una columna INTEGER: pandas trae los NULL como NaN, y un NaN en un
    UPDATE revienta la transacción entera (docs/OPERACION.md §5)."""
    return None if v is None or pd.isna(v) else int(v)


def fill_params(pairs: list, rows: dict) -> list:
    """Los UPDATE de las filas que no tienen tiros (NULL o los cuatro en 0).
    rows: {id: fila actual}. Los córners solo si la fila no los tiene (NULL o
    los dos en 0) y ESPN los trae; un dato que ya existe no se pisa."""
    out = []
    for mid, ev in pairs:
        cur = rows[mid]
        if not all(_empty(cur.get(f"{s}_{k}")) for s in ("home", "away") for k in ("shots", "shots_target")):
            continue
        p = {"id": int(mid), **{f"{s}_{k}": ev[f"{s}_{k}"] for s in ("home", "away") for k in ("shots", "shots_target")}}
        corners_empty = _empty(cur.get("home_corners")) and _empty(cur.get("away_corners"))
        has_corners = ev["home_corners"] is not None and ev["away_corners"] is not None
        for s in ("home", "away"):
            p[f"{s}_corners"] = ev[f"{s}_corners"] if corners_empty and has_corners else _int_or_none(cur.get(f"{s}_corners"))
        out.append(p)
    return out


def proxy_ratio(events: list) -> float | None:
    """xG estimado (xg_proxy) / goles reales de los partidos: cerca de 1 si
    los tiros de ESPN miden lo mismo que los de football-data."""
    from src.features.xg_proxy import SHOT_ON_TARGET_RATE, SHOT_RATE
    xg = goals = 0.0
    for e in events:
        for s in ("home", "away"):
            xg += e[f"{s}_shots_target"] * SHOT_ON_TARGET_RATE + (e[f"{s}_shots"] - e[f"{s}_shots_target"]) * SHOT_RATE
            goals += e[f"{s}_goals"]
    return xg / goals if goals else None


# ─────────────────────────────────────────────────────────────────────────────
# Red y base
# ─────────────────────────────────────────────────────────────────────────────

def load_espn_stats(engine=None, months: int = DAILY_MONTHS, apply: bool = True, get=None,
                    pause: float = 0.3, today=None, verbose: bool = True) -> dict:
    """Completa tiros, tiros a puerta y córners de STATS_LEAGUES en `matches`
    con los últimos `months` meses de ESPN. Una liga que falla se salta; si
    no se pudo leer ninguna es ERROR (llega a Telegram)."""
    from src.features.espn_lineups import _get
    if engine is None:
        from config.database import engine
    yms = months_back(today, months)
    report, updates, reads = {}, [], 0
    for league, slug in _slugs().items():
        events = []
        for ym in yms:
            data = _get(slug, "scoreboard", f"dates={ym}&limit=500", get)
            if pause:
                time.sleep(pause)
            if data is not None:
                reads += 1
            events += parse_stats(data)
        if not events:
            report[league] = {"events": 0, "paired": 0, "filled": 0, "ratio": None}
            continue
        days = [_day(e["kickoff"]) for e in events]
        ms = pd.read_sql(text(f"""
            SELECT id, date, LOWER(home_team) AS h, LOWER(away_team) AS a, home_goals, away_goals,
                   {", ".join(f"{s}_{k}" for s in ("home", "away") for k in STATS)}
            FROM matches WHERE league = :l AND date BETWEEN :d0 AND :d1
        """), engine, params={"l": league, "d0": min(days) - timedelta(days=2),
                               "d1": max(days) + timedelta(days=2)})
        recs = ms.to_dict("records")
        pairs = pair_rows(events, recs)
        params = fill_params(pairs, {r["id"]: r for r in recs})
        updates += params
        report[league] = {"events": len(events), "paired": len(pairs), "filled": len(params),
                          "ratio": proxy_ratio(events)}
        if len(events) >= MIN_EVENTS_TO_JUDGE and not pairs:
            log.warning(f"⚠️  Tiros y córners (ESPN): {league} — {len(events)} partidos de ESPN y ninguno "
                        f"emparejado con matches (¿cambiaron los nombres?)")
    if not reads:
        log.error("❌ Tiros y córners (ESPN): no se pudo leer ninguna liga — MLS, Argentina, Brasil y México "
                  "se quedan sin tiros nuevos")
        return {"filled": 0, "reads": 0, "leagues": report}
    if apply and updates:
        cols = [f"{s}_{k}" for s in ("home", "away") for k in STATS]
        with engine.begin() as conn:
            from src.utils.match_backup import backup_matches
            backup_matches(conn, [u["id"] for u in updates], "espn_stats")
            conn.execute(text(f"""UPDATE matches SET {", ".join(f"{c} = :{c}" for c in cols)}
                                  WHERE id = :id"""), updates)
    if verbose:
        done = {k: v for k, v in report.items() if v["events"]}
        print(f"📊 Tiros y córners (ESPN): {len(updates)} partidos {'completados' if apply else 'por completar (en seco)'} "
              f"({reads} lecturas) · " + ", ".join(
                  f"{k.replace('soccer_', '')} {v['filled']}/{v['paired']}/{v['events']}"
                  + (f" xG/goles {v['ratio']:.2f}" if v["ratio"] else "") for k, v in done.items()))
    return {"filled": len(updates), "reads": reads, "leagues": report}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--months", type=int, default=DAILY_MONTHS)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    load_espn_stats(months=a.months, apply=a.apply)
