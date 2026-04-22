"""Verifica si los duplicados tienen scores iguales o diferentes."""
import sys, io
sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from config.database import engine
import pandas as pd
from sqlalchemy import text
from src.utils.team_normalizer import normalize_team

with engine.connect() as c:
    df = pd.read_sql(text("""
        SELECT id, date, home_team, away_team, league, home_goals, away_goals,
               home_shots, away_shots, home_corners, away_corners
        FROM matches
        WHERE league LIKE 'soccer_%'
    """), c)

df["home_norm"] = df["home_team"].apply(normalize_team)
df["away_norm"] = df["away_team"].apply(normalize_team)
df["key"] = df["date"].astype(str) + "|" + df["home_norm"] + "|" + df["away_norm"]
df["canonical_row"] = (df["home_team"] == df["home_norm"]) & (df["away_team"] == df["away_norm"])

groups = df.groupby("key").filter(lambda g: len(g) > 1).groupby("key")
categories = {
    "both_canonical": 0,
    "one_canonical_same_score": 0,
    "one_canonical_diff_score": 0,
    "none_canonical_same_score": 0,
    "none_canonical_diff_score": 0,
    "more_than_2_rows": 0,
}
diff_score_samples = []
none_canonical_samples = []
for k, g in groups:
    if len(g) > 2:
        categories["more_than_2_rows"] += 1
        continue

    r1, r2 = g.iloc[0], g.iloc[1]
    scores_match = (r1["home_goals"] == r2["home_goals"]) and (r1["away_goals"] == r2["away_goals"])
    if pd.isna(r1["home_goals"]) and pd.isna(r2["home_goals"]):
        scores_match = True  # both NULL
    elif pd.isna(r1["home_goals"]) != pd.isna(r2["home_goals"]):
        scores_match = False  # one NULL, one not

    n_canonical = int(r1["canonical_row"]) + int(r2["canonical_row"])
    if n_canonical == 2:
        categories["both_canonical"] += 1
    elif n_canonical == 1:
        if scores_match:
            categories["one_canonical_same_score"] += 1
        else:
            categories["one_canonical_diff_score"] += 1
            if len(diff_score_samples) < 10:
                diff_score_samples.append((k, r1, r2))
    else:
        if scores_match:
            categories["none_canonical_same_score"] += 1
        else:
            categories["none_canonical_diff_score"] += 1
        if len(none_canonical_samples) < 10:
            none_canonical_samples.append((k, r1, r2))

print("=== Categorías de duplicados ===")
for cat, n in categories.items():
    print(f"  {cat:35s} -> {n}")

print(f"\nTotal grupos dup: {sum(categories.values())}")

print("\n=== Muestra grupos con 'one_canonical_diff_score' (PROBLEMA POTENCIAL) ===")
for k, r1, r2 in diff_score_samples:
    print(f"-- {k} --")
    print(f"  id={r1['id']:>7} | {r1['home_team']:25} vs {r1['away_team']:25} | {r1['home_goals']}-{r1['away_goals']} | canonical={r1['canonical_row']}")
    print(f"  id={r2['id']:>7} | {r2['home_team']:25} vs {r2['away_team']:25} | {r2['home_goals']}-{r2['away_goals']} | canonical={r2['canonical_row']}")
    print()

print("\n=== Muestra grupos con 'none_canonical' (necesitan UPDATE) ===")
for k, r1, r2 in none_canonical_samples:
    print(f"-- {k} --")
    print(f"  id={r1['id']:>7} | {r1['home_team']:25} vs {r1['away_team']:25} | {r1['home_goals']}-{r1['away_goals']}")
    print(f"  id={r2['id']:>7} | {r2['home_team']:25} vs {r2['away_team']:25} | {r2['home_goals']}-{r2['away_goals']}")
    print()
