"""Encuentra TODOS los duplicados en matches usando normalize_team."""
import sys, io
sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from config.database import engine
import pandas as pd
from sqlalchemy import text
from src.utils.team_normalizer import normalize_team

with engine.connect() as c:
    # Traigo todos los matches (solo id, date, home_team, away_team, league)
    df = pd.read_sql(text("""
        SELECT id, date, home_team, away_team, league, home_goals, away_goals
        FROM matches
        WHERE league LIKE 'soccer_%'
    """), c)

print(f"Total matches: {len(df)}")
df["home_norm"] = df["home_team"].apply(normalize_team)
df["away_norm"] = df["away_team"].apply(normalize_team)
df["key"] = df["date"].astype(str) + "|" + df["home_norm"] + "|" + df["away_norm"]

dups = df.groupby("key").filter(lambda g: len(g) > 1)
print(f"Rows en grupos duplicados: {len(dups)}")
print(f"Grupos duplicados distintos: {dups['key'].nunique()}")
print()

# Muestro 20 grupos
for k, g in list(dups.groupby("key"))[:30]:
    print(f"-- {k} --")
    for _, row in g.iterrows():
        score = f"{row['home_goals']}-{row['away_goals']}" if pd.notna(row['home_goals']) else "(sin score)"
        print(f"  id={row['id']:>7} | {row['home_team']:30s} vs {row['away_team']:30s} | {score} | {row['league']}")
    print()

# Salvo todos los ids a eliminar (el que tenga nombres raw / uppercase / variantes)
# Estrategia: mantener el row con home_team == home_norm AND away_team == away_norm (ya canónico).
# Si ninguno cumple, mantener el que tenga scores, o simplemente el id menor.
to_delete = []
to_keep = []
for k, g in dups.groupby("key"):
    canonical = g[(g["home_team"] == g["home_norm"]) & (g["away_team"] == g["away_norm"])]
    if len(canonical) >= 1:
        keep_id = canonical.iloc[0]["id"]
    else:
        # Preferir uno con scores
        with_scores = g[g["home_goals"].notna()]
        if len(with_scores):
            keep_id = with_scores.iloc[0]["id"]
        else:
            keep_id = g.iloc[0]["id"]
    for _, row in g.iterrows():
        if row["id"] != keep_id:
            to_delete.append(row["id"])
        else:
            to_keep.append(row["id"])

print(f"\nTotal rows a ELIMINAR: {len(to_delete)}")
print(f"Total rows a MANTENER: {len(to_keep)}")
print(f"IDs a eliminar (primeros 30): {to_delete[:30]}")

# Guardo los ids en archivo para el script de cleanup
with open("scripts/_dup_match_ids_to_delete.txt", "w") as f:
    for i in to_delete:
        f.write(str(i) + "\n")
print("\nGuardado en scripts/_dup_match_ids_to_delete.txt")
