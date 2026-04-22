"""Detecta colisiones de unique constraint que bloquearían las UPDATEs.

Para cada mapeo (old_name -> new_name), busca filas donde:
  EXISTE match con (date, old_name, X)  Y  TAMBIÉN match con (date, new_name, X)
Esos son duplicados literales — hay que decidir cuál mantener y cuál borrar.
"""
import sys, io
sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from config.database import engine
import pandas as pd
from sqlalchemy import text

MAPPINGS = [
    ("inter milan (MLS)", "inter miami", "league='soccer_usa_mls'"),
    ("guadalajara chivas", "chivas", "1=1"),
    ("estudiantes lp", "estudiantes", "1=1"),
    ("estudiantes la plata", "estudiantes", "1=1"),
    ("Independiente Rivadavia", "ind rivadavia", "1=1"),
    ("CA Tigre BA", "tigre", "1=1"),
]

with engine.connect() as c:
    for old, new, cond in MAPPINGS:
        print()
        print(f"=== Colisiones al mapear '{old}' -> '{new}' ===")

        # Caso 1: old como home_team, new como home_team en misma fecha/away
        dup_home = pd.read_sql(text(f"""
            SELECT a.id as id_old, a.home_team as h_old, a.away_team, a.date,
                   b.id as id_new, b.home_team as h_new
            FROM matches a
            JOIN matches b ON a.date = b.date
                          AND a.away_team = b.away_team
                          AND a.id != b.id
            WHERE a.home_team = :old
              AND b.home_team = :new
              AND {cond.replace('league', 'a.league')}
        """), c, params={"old": old, "new": new})
        if len(dup_home):
            print(f"  -- colisión HOME_TEAM: {len(dup_home)} pares --")
            print(dup_home.to_string())

        # Caso 2: old como away_team, new como away_team en misma fecha/home
        dup_away = pd.read_sql(text(f"""
            SELECT a.id as id_old, a.home_team, a.away_team as a_old, a.date,
                   b.id as id_new, b.away_team as a_new
            FROM matches a
            JOIN matches b ON a.date = b.date
                          AND a.home_team = b.home_team
                          AND a.id != b.id
            WHERE a.away_team = :old
              AND b.away_team = :new
              AND {cond.replace('league', 'a.league')}
        """), c, params={"old": old, "new": new})
        if len(dup_away):
            print(f"  -- colisión AWAY_TEAM: {len(dup_away)} pares --")
            print(dup_away.to_string())

        if not len(dup_home) and not len(dup_away):
            print("  (sin colisiones)")
