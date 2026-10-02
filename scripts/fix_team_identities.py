"""
scripts/fix_team_identities.py
==============================
Une en `matches` los nombres distintos del mismo club y los partidos
repetidos (1-oct-26). Tres fases en UNA transacción:

  1. PSG / Paris FC: en 2025-26 cada partido de Paris FC entró hasta tres
     veces desde football-data, con tres etiquetas según la versión del
     normalizador: "paris fc", "paris" y "paris saint germain". Las filas de
     Ligue 1 desde jul-2025 con cualquier etiqueta de París se corrigen con
     el CSV original (fecha ±1 día, mismo rival, mismo marcador).
  2. Nombres: todo nombre guardado que normalize_team cambia pasa a su
     canónico (los alias aprendidos de config/team_aliases.json y los de los
     mapas a mano que quedaron sin aplicar). Las búsquedas de historia usan
     el canónico: una fila con otro nombre no se encuentra. Las selecciones
     se guardan con mayúsculas ("China PR"): ahí solo se usa una grafía que
     ya exista, para que el cargador internacional no vuelva a duplicarla.
  3. Repetidos: el mismo partido (mismos equipos, mismo marcador, fecha ±1
     día) queda en UNA fila —la de más estadísticas; ante empate la de
     football-data (trae la fecha local)— completada con lo que tenga la
     otra.

Respaldo: antes de tocar nada, cada fila modificada o eliminada se copia tal
cual a matches_identity_backup ('update_identity' / 'delete_duplicate').

Correr DESPUÉS de scripts/fix_league_labels.py --apply.

Uso:
    python scripts/fix_team_identities.py            # ensayo: muestra qué haría
    python scripts/fix_team_identities.py --apply
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from sqlalchemy import text

from src.utils.football_data import SEASON_URL, check_division, fetch_csv, parse_dates
from src.utils.team_normalizer import normalize_team

PSG, PFC = "paris saint germain", "paris fc"
PARIS_LABELS = {PSG, PFC, "paris sg", "paris"}
PARIS_SINCE = "2025-07-01"           # Paris FC subió a la Ligue 1 en 2025-26
LIGUE_1 = "soccer_france_ligue_one"
FD_SEASONS = ("2526", "2627")
STAT_COLS = ["home_shots", "away_shots", "home_shots_target", "away_shots_target",
             "home_corners", "away_corners", "home_yellow", "away_yellow",
             "home_red", "away_red", "home_goals_ht", "away_goals_ht",
             "home_goals_h2", "away_goals_h2"]
FILL_COLS = STAT_COLS + ["season", "league", "sport_key", "neutral", "aet"]


# ─────────────────────────────────────────────────────────────────────────────
# Fase 1 — París (funciones puras)
# ─────────────────────────────────────────────────────────────────────────────

def paris_truth(csv_df: pd.DataFrame) -> dict:
    """{(fecha, lado de París, rival normalizado, gl, gv): club} desde el CSV."""
    out = {}
    dates = parse_dates(csv_df["Date"].astype(str).str.strip())
    for d, h, a, hg, ag in zip(dates, csv_df["HomeTeam"], csv_df["AwayTeam"],
                               csv_df["FTHG"], csv_df["FTAG"]):
        if pd.isna(d) or pd.isna(hg) or pd.isna(ag):
            continue
        for side, team, rival in (("h", h, a), ("a", a, h)):
            club = {"Paris SG": PSG, "Paris FC": PFC}.get(str(team).strip())
            if club:
                out[(d.date(), side, normalize_team(str(rival)), int(hg), int(ag))] = club
    return out


def _rivals(name: str) -> list:
    """Normalizaciones posibles del rival. En el derbi el rival también es
    una etiqueta de París, y puede ser la equivocada."""
    return [PSG, PFC] if str(name).lower() in PARIS_LABELS else [normalize_team(str(name))]


def paris_corrections(rows: pd.DataFrame, truth: dict) -> tuple[dict, int]:
    """({id: {"home_team"|"away_team": club}}, lados de París sin partido en el CSV)."""
    fixes, unresolved = {}, 0
    for r in rows.itertuples():
        if pd.isna(r.home_goals) or pd.isna(r.away_goals):
            continue
        d = pd.Timestamp(r.date).date()
        for side, col, team, rival in (("h", "home_team", r.home_team, r.away_team),
                                       ("a", "away_team", r.away_team, r.home_team)):
            if str(team).lower() not in PARIS_LABELS:
                continue
            club = next((truth[k] for delta in (0, -1, 1) for rv in _rivals(rival)
                         if (k := (d + pd.Timedelta(days=delta), side, rv,
                                   int(r.home_goals), int(r.away_goals))) in truth), None)
            if club is None:
                unresolved += 1
            elif club != str(team).lower():
                fixes.setdefault(int(r.id), {})[col] = club
    return fixes, unresolved


# ─────────────────────────────────────────────────────────────────────────────
# Fases 2 y 3 — nombres y repetidos (funciones puras)
# ─────────────────────────────────────────────────────────────────────────────

def rename_map(name_counts: dict) -> dict:
    """
    {nombre guardado: nombre nuevo} de los que cambian. En minúsculas
    (clubes): el canónico de normalize_team. Con mayúsculas (selecciones del
    cargador internacional): la grafía guardada más común de su canónico, si
    existe; si no, el nombre queda igual (no se inventa una grafía nueva).
    """
    styles: dict = {}
    for n, _ in sorted(name_counts.items(), key=lambda t: -t[1]):
        if n != n.lower():
            styles.setdefault(n.lower(), n)
    out = {}
    for n in name_counts:
        canonical = normalize_team(n)
        new = canonical if n == n.lower() else styles.get(canonical, n)
        if new != n:
            out[n] = new
    return out


def plan_names(rows: pd.DataFrame, renames: dict, paris_fixes: dict) -> pd.DataFrame:
    """Columnas new_home / new_away: la corrección de París, si no el nombre
    nuevo, si no el nombre tal cual. Un renombre que deja al equipo contra
    sí mismo no se hace."""
    out = rows.copy()
    new_h, new_a = [], []
    for r in out.itertuples():
        fix = paris_fixes.get(int(r.id), {})
        h = fix.get("home_team") or renames.get(str(r.home_team), str(r.home_team))
        a = fix.get("away_team") or renames.get(str(r.away_team), str(r.away_team))
        if h.lower() == a.lower():
            h, a = str(r.home_team), str(r.away_team)
        new_h.append(h)
        new_a.append(a)
    out["new_home"], out["new_away"] = new_h, new_a
    return out


def _n_data(r) -> int:
    return sum(0 if pd.isna(r.get(c)) else 1 for c in STAT_COLS)


def plan_merges(rows: pd.DataFrame) -> tuple[dict, list]:
    """
    Agrupa el mismo partido (new_home, new_away, marcador, fecha ±1 día).
    Devuelve ({id que se queda: {columna: valor completado}}, ids a borrar).
    """
    keep, delete = {}, []
    scored = rows.dropna(subset=["home_goals", "away_goals"]).copy()
    scored["_h"] = scored["new_home"].str.lower()
    scored["_a"] = scored["new_away"].str.lower()
    for _, g in scored.groupby(["_h", "_a", "home_goals", "away_goals"]):
        if len(g) < 2:
            continue
        g = g.sort_values(["date", "id"])
        clusters, cluster = [], [g.iloc[0]]
        for _, r in list(g.iterrows())[1:]:
            if (pd.Timestamp(r["date"]) - pd.Timestamp(cluster[-1]["date"])).days <= 1:
                cluster.append(r)
            else:
                clusters.append(cluster)
                cluster = [r]
        clusters.append(cluster)
        for c in clusters:
            if len(c) < 2:
                continue
            ranked = sorted(c, key=lambda r: (_n_data(r), not pd.isna(r.get("season")), -int(r["id"])),
                            reverse=True)
            keeper, donors = ranked[0], ranked[1:]
            filled = {}
            for col in FILL_COLS:
                if col in keeper.index and pd.isna(keeper[col]):
                    for d in donors:
                        if col in d.index and not pd.isna(d[col]):
                            filled[col] = d[col]
                            break
            keep[int(keeper["id"])] = filled
            delete.extend(int(d["id"]) for d in donors)
    return keep, delete


def blocked_ids(plan: pd.DataFrame, delete: list) -> set:
    """Filas que tras el plan chocarían con otra en la clave única (fecha,
    local, visitante): el mismo día y los mismos equipos con OTRO marcador.
    Esas no se tocan (dato a revisar)."""
    final = plan[~plan["id"].isin(delete)]
    dup = final.groupby([final["date"].dt.date, "new_home", "new_away"]).filter(lambda g: len(g) > 1)
    return set(int(i) for i in dup["id"])


# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────

def _paris_csv() -> pd.DataFrame:
    frames = []
    for season in FD_SEASONS:
        df, why = fetch_csv(SEASON_URL.format(season=season, code="F1"))
        problem = why if df is None else check_division(df, "F1")
        if problem:
            raise RuntimeError(f"Ligue 1 {season}: {problem}")
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def _native(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    return v.item() if hasattr(v, "item") else v


NOT_BASEBALL = "(league IS NULL OR league NOT LIKE 'baseball%%')"


def update_names(conn, updates: list, chunk: int = 1000) -> None:
    """updates: (id, local, visitante, local_norm, visitante_norm). Un UPDATE
    por lote (FROM VALUES): fila por fila eran ~20 mil viajes a la base."""
    for start in range(0, len(updates), chunk):
        part = updates[start:start + chunk]
        values = ", ".join(f"(:i{k}, :h{k}, :a{k}, :hn{k}, :an{k})" for k in range(len(part)))
        params = {}
        for k, (i, h, a, hn, an) in enumerate(part):
            params.update({f"i{k}": int(i), f"h{k}": h, f"a{k}": a, f"hn{k}": hn, f"an{k}": an})
        conn.execute(text(f"""
            UPDATE matches m SET home_team = v.h, away_team = v.a,
                team_home_norm = CASE WHEN m.team_home_norm IS NULL THEN NULL ELSE v.hn END,
                team_away_norm = CASE WHEN m.team_away_norm IS NULL THEN NULL ELSE v.an END
            FROM (VALUES {values}) AS v(id, h, a, hn, an)
            WHERE m.id = v.id
        """), params)


def main(apply: bool) -> int:
    from config.database import engine
    from src.utils.match_backup import backup_matches
    # La descarga va antes que la base: una conexión inactiva mientras tanto
    # la corta el servidor (2-oct-26)
    truth = paris_truth(_paris_csv())
    counts = pd.read_sql(text(f"""
        SELECT t AS name, COUNT(*) AS n FROM (
            SELECT home_team AS t FROM matches WHERE {NOT_BASEBALL}
            UNION ALL SELECT away_team FROM matches WHERE {NOT_BASEBALL}) x
        GROUP BY 1
    """), engine).set_index("name")["n"].to_dict()
    renames = rename_map(counts)
    names = sorted(set(renames) | set(renames.values())
                   | {n for n in counts if n.lower() in PARIS_LABELS})
    rows = pd.read_sql(text(f"""
        SELECT * FROM matches
        WHERE (home_team = ANY(:n) OR away_team = ANY(:n)) AND {NOT_BASEBALL}
    """), engine, params={"n": names})
    rows["date"] = pd.to_datetime(rows["date"])
    paris_rows = rows[(rows["date"] >= PARIS_SINCE) & (rows["league"] == LIGUE_1)
                      & (rows["home_team"].str.lower().isin(PARIS_LABELS)
                         | rows["away_team"].str.lower().isin(PARIS_LABELS))]
    paris_fixes, paris_unresolved = paris_corrections(paris_rows, truth)
    plan = plan_names(rows, renames, paris_fixes)
    keep, delete = plan_merges(plan)
    blocked = blocked_ids(plan, delete)
    keep = {k: v for k, v in keep.items() if k not in blocked}
    renamed = plan[((plan["new_home"] != plan["home_team"]) | (plan["new_away"] != plan["away_team"]))
                   & ~plan["id"].isin(delete) & ~plan["id"].isin(blocked)]
    to_update = sorted(set(int(i) for i in renamed["id"]) | set(keep))
    club_fixes = sum(1 for fx in paris_fixes.values() for c in fx.values() if c == PFC)

    print(f"Filas leídas: {len(rows)} ({len(renames)} nombres cambian + sus canónicos + París)")
    print(f"1. París: {club_fixes} etiquetas pasan a Paris FC según el CSV · "
          f"{paris_unresolved} lados sin partido en el CSV (quedan igual)")
    print(f"2. Nombres: {len(renamed)} filas pasan al nombre canónico")
    for old, new in sorted(renames.items(), key=lambda t: -counts[t[0]]):
        if old != old.lower() or counts[old] >= 150:
            print(f"   {counts[old]:>5}  {old} → {new}")
    print(f"3. Repetidos: {len(delete)} filas sobran (el mismo partido dos o más veces) · "
          f"{sum(1 for v in keep.values() if v)} de las que se quedan se completan con la otra")
    per_league = Counter(plan.set_index("id").loc[delete, "league"].fillna("(sin liga)"))
    for lg, n in per_league.most_common(12):
        print(f"   {n:>5}  {lg}")
    if blocked:
        print(f"   ⚠️  {len(blocked)} filas no se tocan: mismo día y equipos con otro marcador")
    if not apply:
        print("EN SECO: no se escribió nada. Repetir con --apply.")
        return 0

    by_id = plan.set_index("id")
    updates = [(i, by_id.at[i, "new_home"], by_id.at[i, "new_away"],
                normalize_team(by_id.at[i, "new_home"]), normalize_team(by_id.at[i, "new_away"]))
               for i in to_update]
    engine.dispose()             # conexión nueva: la del plan quedó inactiva mientras se calculaba
    with engine.begin() as conn:
        backup_matches(conn, sorted(set(delete) | set(to_update)), "update_identity", delete_ids=delete)
        if delete:
            conn.execute(text("DELETE FROM matches WHERE id = ANY(:ids)"), {"ids": delete})
        update_names(conn, updates)
        for i, filled in keep.items():
            if filled:
                assign = ", ".join(f"{c} = :{c}" for c in filled)
                conn.execute(text(f"UPDATE matches SET {assign} WHERE id = :id"),
                             {**{c: _native(v) for c, v in filled.items()}, "id": i})
    print(f"✅ Aplicado: {len(delete)} repetidos fuera, {len(to_update)} filas actualizadas "
          f"(respaldo en matches_identity_backup)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="escribe los cambios")
    sys.exit(main(ap.parse_args().apply))
