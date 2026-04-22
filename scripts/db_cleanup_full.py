"""
DB Cleanup FULL en transacción única.

FASE 1: DELETE 719 rows duplicados (los NO canónicos)
FASE 2: UPDATE formas solitarias no-canónicas
FASE 3: UPDATE bets_history (inter milan MLS → inter miami)
FASE 4: UPDATE upcoming_matches (Independiente Rivadavia → ind rivadavia)
FASE 5: verificación
"""
import sys, io
sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from config.database import engine
import pandas as pd
from sqlalchemy import text
from src.utils.team_normalizer import normalize_team

# === FASE 0: recolectar ids a borrar (en memoria, fuera de tx) ===
print("=" * 70)
print("FASE 0: identificando duplicados canónicos...")
print("=" * 70)
with engine.connect() as c:
    df = pd.read_sql(text("""
        SELECT id, date, home_team, away_team, league, home_goals, away_goals
        FROM matches
        WHERE league LIKE 'soccer_%'
    """), c)

df["home_norm"] = df["home_team"].apply(normalize_team)
df["away_norm"] = df["away_team"].apply(normalize_team)
df["key"] = df["date"].astype(str) + "|" + df["home_norm"] + "|" + df["away_norm"]
df["canonical_row"] = (df["home_team"] == df["home_norm"]) & (df["away_team"] == df["away_norm"])

ids_to_delete = []
for k, g in df.groupby("key"):
    if len(g) < 2:
        continue
    canonical = g[g["canonical_row"]]
    if len(canonical) >= 1:
        keep_id = canonical.iloc[0]["id"]
        for _, row in g.iterrows():
            if row["id"] != keep_id:
                ids_to_delete.append(int(row["id"]))

print(f"  Rows a eliminar: {len(ids_to_delete)}")

# === EJECUCIÓN EN TRANSACCIÓN ===
with engine.begin() as c:
    print()
    print("=" * 70)
    print("FASE 1: DELETE duplicados no-canónicos")
    print("=" * 70)
    if ids_to_delete:
        # Borrar en chunks de 500 para evitar queries gigantes
        total_deleted = 0
        for i in range(0, len(ids_to_delete), 500):
            chunk = ids_to_delete[i:i+500]
            r = c.execute(text("DELETE FROM matches WHERE id = ANY(:ids)"), {"ids": chunk})
            total_deleted += r.rowcount
        print(f"  ✓ Eliminadas {total_deleted} rows duplicadas")
    else:
        print("  (nada que borrar)")

    print()
    print("=" * 70)
    print("FASE 2: UPDATE formas no-canónicas solitarias")
    print("=" * 70)
    updates = [
        ("matches.home_team inter milan (MLS)",
         "UPDATE matches SET home_team='inter miami' WHERE home_team='inter milan' AND league='soccer_usa_mls'"),
        ("matches.away_team inter milan (MLS)",
         "UPDATE matches SET away_team='inter miami' WHERE away_team='inter milan' AND league='soccer_usa_mls'"),
        ("matches.team_home_norm inter milan (MLS)",
         "UPDATE matches SET team_home_norm='inter miami' WHERE team_home_norm='inter milan' AND league='soccer_usa_mls'"),
        ("matches.team_away_norm inter milan (MLS)",
         "UPDATE matches SET team_away_norm='inter miami' WHERE team_away_norm='inter milan' AND league='soccer_usa_mls'"),

        ("matches.home_team guadalajara chivas",
         "UPDATE matches SET home_team='chivas' WHERE home_team='guadalajara chivas'"),
        ("matches.away_team guadalajara chivas",
         "UPDATE matches SET away_team='chivas' WHERE away_team='guadalajara chivas'"),
        ("matches.team_home_norm guadalajara chivas",
         "UPDATE matches SET team_home_norm='chivas' WHERE team_home_norm='guadalajara chivas'"),
        ("matches.team_away_norm guadalajara chivas",
         "UPDATE matches SET team_away_norm='chivas' WHERE team_away_norm='guadalajara chivas'"),

        ("matches.home_team estudiantes variants",
         "UPDATE matches SET home_team='estudiantes' WHERE home_team IN ('estudiantes lp','estudiantes la plata')"),
        ("matches.away_team estudiantes variants",
         "UPDATE matches SET away_team='estudiantes' WHERE away_team IN ('estudiantes lp','estudiantes la plata')"),
        ("matches.team_home_norm estudiantes variants",
         "UPDATE matches SET team_home_norm='estudiantes' WHERE team_home_norm IN ('estudiantes lp','estudiantes la plata')"),
        ("matches.team_away_norm estudiantes variants",
         "UPDATE matches SET team_away_norm='estudiantes' WHERE team_away_norm IN ('estudiantes lp','estudiantes la plata')"),

        ("matches.home_team Independiente Rivadavia (upper)",
         "UPDATE matches SET home_team='ind rivadavia' WHERE home_team='Independiente Rivadavia'"),
        ("matches.away_team Independiente Rivadavia (upper)",
         "UPDATE matches SET away_team='ind rivadavia' WHERE away_team='Independiente Rivadavia'"),

        ("matches.home_team CA Tigre BA (upper)",
         "UPDATE matches SET home_team='tigre' WHERE home_team='CA Tigre BA'"),
        ("matches.away_team CA Tigre BA (upper)",
         "UPDATE matches SET away_team='tigre' WHERE away_team='CA Tigre BA'"),
    ]
    for label, sql in updates:
        r = c.execute(text(sql))
        print(f"  ✓ {label:55s} -> {r.rowcount} rows")

    print()
    print("=" * 70)
    print("FASE 3: UPDATE bets_history (inter milan MLS)")
    print("=" * 70)
    r = c.execute(text("UPDATE bets_history SET match=REPLACE(match,'inter milan','inter miami') WHERE match ILIKE '%inter milan%' AND league='soccer_usa_mls'"))
    print(f"  ✓ bets_history.match inter milan -> {r.rowcount} rows")

    print()
    print("=" * 70)
    print("FASE 4: UPDATE upcoming_matches (Independiente Rivadavia)")
    print("=" * 70)
    r1 = c.execute(text("UPDATE upcoming_matches SET home_team='ind rivadavia' WHERE home_team='Independiente Rivadavia'"))
    r2 = c.execute(text("UPDATE upcoming_matches SET away_team='ind rivadavia' WHERE away_team='Independiente Rivadavia'"))
    print(f"  ✓ upcoming_matches.home_team -> {r1.rowcount} rows")
    print(f"  ✓ upcoming_matches.away_team -> {r2.rowcount} rows")

    print()
    print("=" * 70)
    print("FASE 5: VERIFICACIÓN POST-CLEANUP")
    print("=" * 70)
    checks = [
        ("matches inter milan MLS residual",
         "SELECT COUNT(*) FROM matches WHERE league='soccer_usa_mls' AND (home_team='inter milan' OR away_team='inter milan')"),
        ("matches guadalajara chivas residual",
         "SELECT COUNT(*) FROM matches WHERE home_team='guadalajara chivas' OR away_team='guadalajara chivas'"),
        ("matches estudiantes lp/la plata residual",
         "SELECT COUNT(*) FROM matches WHERE home_team IN ('estudiantes lp','estudiantes la plata') OR away_team IN ('estudiantes lp','estudiantes la plata')"),
        ("matches Independiente Rivadavia residual",
         "SELECT COUNT(*) FROM matches WHERE home_team='Independiente Rivadavia' OR away_team='Independiente Rivadavia'"),
        ("matches CA Tigre BA residual",
         "SELECT COUNT(*) FROM matches WHERE home_team='CA Tigre BA' OR away_team='CA Tigre BA'"),
        ("matches atletico madrid residual (debería quedar ath madrid)",
         "SELECT COUNT(*) FROM matches WHERE home_team='atletico madrid' OR away_team='atletico madrid'"),
        ("matches real sociedad residual (debería quedar sociedad)",
         "SELECT COUNT(*) FROM matches WHERE home_team='real sociedad' OR away_team='real sociedad'"),
        ("bets_history inter milan MLS residual",
         "SELECT COUNT(*) FROM bets_history WHERE match ILIKE '%inter milan%' AND league='soccer_usa_mls'"),
        ("upcoming_matches Independiente Rivadavia residual",
         "SELECT COUNT(*) FROM upcoming_matches WHERE home_team='Independiente Rivadavia' OR away_team='Independiente Rivadavia'"),
    ]
    for label, sql in checks:
        n = c.execute(text(sql)).scalar()
        mark = "✓" if n == 0 else "⚠"
        print(f"  {mark} {label:60s} -> {n}")

    # Recuento dup check
    print()
    df2 = pd.read_sql(text("SELECT id, date, home_team, away_team, league FROM matches WHERE league LIKE 'soccer_%'"), c)
    df2["home_norm"] = df2["home_team"].apply(normalize_team)
    df2["away_norm"] = df2["away_team"].apply(normalize_team)
    df2["key"] = df2["date"].astype(str) + "|" + df2["home_norm"] + "|" + df2["away_norm"]
    remaining_dups = df2.groupby("key").filter(lambda g: len(g) > 1)
    print(f"  {'✓' if len(remaining_dups) == 0 else '⚠'} grupos canónicos duplicados residuales: {remaining_dups['key'].nunique()}")
    if len(remaining_dups) > 0:
        print(remaining_dups[["id","date","home_team","away_team"]].head(20).to_string())

print()
print("✅ Cleanup completado (transacción COMMITTED).")
