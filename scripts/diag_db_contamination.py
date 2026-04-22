"""Diagnóstico: muestra rows contaminadas + estado de team_home_norm/team_away_norm."""
import sys, io
sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from config.database import engine
import pandas as pd
from sqlalchemy import text

with engine.connect() as c:
    print("=" * 70)
    print("Muestra Inter Milan en MLS (home_team / away_team vs norm cols)")
    print("=" * 70)
    r = pd.read_sql(text("""
        SELECT id, home_team, away_team, team_home_norm, team_away_norm, date, league
        FROM matches
        WHERE league='soccer_usa_mls'
          AND (home_team='inter milan' OR away_team='inter milan'
               OR team_home_norm='inter milan' OR team_away_norm='inter milan')
        LIMIT 5
    """), c)
    print(r.to_string() if len(r) else "(sin rows)")

    print()
    print("=" * 70)
    print("Muestra guadalajara chivas")
    print("=" * 70)
    r = pd.read_sql(text("""
        SELECT id, home_team, away_team, team_home_norm, team_away_norm, league
        FROM matches
        WHERE home_team='guadalajara chivas' OR away_team='guadalajara chivas'
           OR team_home_norm='guadalajara chivas' OR team_away_norm='guadalajara chivas'
        LIMIT 5
    """), c)
    print(r.to_string() if len(r) else "(sin rows)")

    print()
    print("=" * 70)
    print("Muestra estudiantes lp + estudiantes la plata")
    print("=" * 70)
    r = pd.read_sql(text("""
        SELECT id, home_team, away_team, team_home_norm, team_away_norm, league
        FROM matches
        WHERE home_team IN ('estudiantes la plata','estudiantes lp')
           OR away_team IN ('estudiantes la plata','estudiantes lp')
        LIMIT 10
    """), c)
    print(r.to_string() if len(r) else "(sin rows)")

    print()
    print("=" * 70)
    print("Muestra Independiente Rivadavia (uppercase)")
    print("=" * 70)
    r = pd.read_sql(text("""
        SELECT id, home_team, away_team, team_home_norm, team_away_norm, league
        FROM matches
        WHERE home_team='Independiente Rivadavia' OR away_team='Independiente Rivadavia'
        LIMIT 5
    """), c)
    print(r.to_string() if len(r) else "(sin rows)")

    print()
    print("=" * 70)
    print("Muestra bets_history con inter milan en MLS")
    print("=" * 70)
    r = pd.read_sql(text("""
        SELECT id, match, market, result, match_date
        FROM bets_history
        WHERE match ILIKE '%inter milan%' AND league='soccer_usa_mls'
        LIMIT 5
    """), c)
    print(r.to_string() if len(r) else "(sin rows)")

    print()
    print("=" * 70)
    print("Muestra upcoming_matches con Independiente Rivadavia")
    print("=" * 70)
    r = pd.read_sql(text("""
        SELECT *
        FROM upcoming_matches
        WHERE home_team='Independiente Rivadavia' OR away_team='Independiente Rivadavia'
        LIMIT 5
    """), c)
    print(r.to_string() if len(r) else "(sin rows)")
