"""Cleanup final: TODAS las formas no-canónicas residuales.

Estrategia: itera sobre cada TEAM_NAME_MAP key y si la DB todavía
tiene rows con esa forma raw, los updatea a la forma canónica.
Solo procede si no hay colisión (verifica que el target canónico
no exista ya para misma fecha/oponente).
"""
import sys, io
sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from config.database import engine
import pandas as pd
from sqlalchemy import text
from src.utils.team_name_map import TEAM_NAME_MAP
from src.utils.team_normalizer import normalize_team

# formas canónicas esperadas por normalize_team
# Para cada mapeo raw→canonical que podría haber llegado a la DB,
# updatea a canonical. El cleanup previo ya eliminó los duplicados,
# así que aquí solo quedan rows únicos con nombre raw.

targets = []
for raw, canonical in TEAM_NAME_MAP.items():
    # Aplicamos normalize_team para obtener la forma FINAL de ambos
    # (por si hay 3er pass de alias que cambia el mapping)
    final_canonical = normalize_team(raw)
    if final_canonical != raw:
        targets.append((raw, final_canonical))

print(f"Targets con mapeo non-trivial: {len(targets)}")

with engine.begin() as c:
    total_home = 0
    total_away = 0
    for raw, canonical in targets:
        # Check if there are any rows with raw form
        n_home = c.execute(text("SELECT COUNT(*) FROM matches WHERE home_team=:raw AND league LIKE 'soccer_%'"), {"raw": raw}).scalar()
        n_away = c.execute(text("SELECT COUNT(*) FROM matches WHERE away_team=:raw AND league LIKE 'soccer_%'"), {"raw": raw}).scalar()
        if n_home == 0 and n_away == 0:
            continue

        # Verificar colisiones antes de updatear
        coll_home = c.execute(text("""
            SELECT COUNT(*) FROM matches a
            JOIN matches b ON a.date=b.date AND a.away_team=b.away_team AND a.id!=b.id
            WHERE a.home_team=:raw AND b.home_team=:canonical
              AND a.league LIKE 'soccer_%'
        """), {"raw": raw, "canonical": canonical}).scalar()
        coll_away = c.execute(text("""
            SELECT COUNT(*) FROM matches a
            JOIN matches b ON a.date=b.date AND a.home_team=b.home_team AND a.id!=b.id
            WHERE a.away_team=:raw AND b.away_team=:canonical
              AND a.league LIKE 'soccer_%'
        """), {"raw": raw, "canonical": canonical}).scalar()

        if coll_home > 0 or coll_away > 0:
            print(f"  ⚠  {raw:40s} → {canonical:30s} | SKIP (colisión home={coll_home} away={coll_away})")
            continue

        rh = c.execute(text("UPDATE matches SET home_team=:can WHERE home_team=:raw AND league LIKE 'soccer_%'"),
                       {"can": canonical, "raw": raw})
        ra = c.execute(text("UPDATE matches SET away_team=:can WHERE away_team=:raw AND league LIKE 'soccer_%'"),
                       {"can": canonical, "raw": raw})
        # También norm cols
        c.execute(text("UPDATE matches SET team_home_norm=:can WHERE team_home_norm=:raw AND league LIKE 'soccer_%'"),
                  {"can": canonical, "raw": raw})
        c.execute(text("UPDATE matches SET team_away_norm=:can WHERE team_away_norm=:raw AND league LIKE 'soccer_%'"),
                  {"can": canonical, "raw": raw})
        total_home += rh.rowcount
        total_away += ra.rowcount
        print(f"  ✓  {raw:40s} → {canonical:30s} | home={rh.rowcount} away={ra.rowcount}")

    print()
    print(f"TOTAL home_team updated: {total_home}")
    print(f"TOTAL away_team updated: {total_away}")

    # Verificación final: ¿hay duplicados post-cleanup?
    print()
    print("=== Verificación final duplicados ===")
    df = pd.read_sql(text("SELECT id, date, home_team, away_team, league FROM matches WHERE league LIKE 'soccer_%'"), c)
    df["home_norm"] = df["home_team"].apply(normalize_team)
    df["away_norm"] = df["away_team"].apply(normalize_team)
    df["key"] = df["date"].astype(str) + "|" + df["home_norm"] + "|" + df["away_norm"]
    dups = df.groupby("key").filter(lambda g: len(g) > 1)
    print(f"  Grupos duplicados residuales: {dups['key'].nunique()}")

    # Cuántos rows tienen home_team != home_norm (aún contaminados)
    off = df[(df["home_team"] != df["home_norm"]) | (df["away_team"] != df["away_norm"])]
    print(f"  Rows con home_team!=home_norm OR away_team!=away_norm: {len(off)}")
    if len(off) > 0:
        print("  Top 20 nombres raw residuales:")
        raw_names = pd.concat([
            off[off["home_team"] != off["home_norm"]][["home_team","home_norm"]].rename(columns={"home_team":"raw","home_norm":"canonical"}),
            off[off["away_team"] != off["away_norm"]][["away_team","away_norm"]].rename(columns={"away_team":"raw","away_norm":"canonical"}),
        ])
        print(raw_names.value_counts().head(20).to_string())

print("\n✅ Cleanup final completado.")
