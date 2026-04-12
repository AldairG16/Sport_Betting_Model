import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team
import difflib


# =========================
# LOAD DATA
# =========================

def load_data():

    upcoming = pd.read_sql("""
        SELECT DISTINCT home_team, away_team
        FROM upcoming_matches
    """, engine)

    matches = pd.read_sql("""
        SELECT home_team, away_team
        FROM matches
    """, engine)

    return upcoming, matches


# =========================
# BUILD TEAM SETS
# =========================

def build_team_sets(upcoming, matches):

    upcoming_teams = set()
    matches_teams = set()

    for _, row in upcoming.iterrows():
        upcoming_teams.add(normalize_team(row.home_team))
        upcoming_teams.add(normalize_team(row.away_team))

    for _, row in matches.iterrows():
        matches_teams.add(normalize_team(row.home_team))
        matches_teams.add(normalize_team(row.away_team))

    return upcoming_teams, matches_teams


# =========================
# 🔥 AUTO MAPPING ENGINE
# =========================

def find_best_match(team, candidates, threshold=0.80):

    matches = difflib.get_close_matches(team, candidates, n=3, cutoff=threshold)

    if not matches:
        return None

    best = matches[0]

    # 🔥 FILTRO INTELIGENTE (evita errores tipo toronto → torino)
    if team.split()[0] != best.split()[0]:
        return None

    return best


def build_auto_mapping(missing_teams, matches_teams):

    print("\n🤖 AUTO-MAPPING SUGGESTIONS:\n")

    mapping = {}

    for team in sorted(missing_teams):

        best_match = find_best_match(team, matches_teams)

        if best_match:
            mapping[team] = best_match
            print(f"✅ {team}  →  {best_match}")

        else:
            print(f"❌ {team}  →  NO MATCH")

    return mapping


# =========================
# ANALYSIS
# =========================

def analyze():

    print("\n🔍 ANALYZING DATA COVERAGE...\n")

    upcoming, matches = load_data()

    upcoming_teams, matches_teams = build_team_sets(upcoming, matches)

    missing_teams = upcoming_teams - matches_teams
    covered_teams = upcoming_teams & matches_teams

    print(f"📊 Upcoming teams: {len(upcoming_teams)}")
    print(f"📊 Historical teams: {len(matches_teams)}")
    print(f"❌ Missing teams: {len(missing_teams)}")
    print(f"✅ Covered teams: {len(covered_teams)}")

    print("\n❌ TEAMS WITH NO DATA:\n")

    for t in sorted(missing_teams):
        print("-", t)

    # 🔥 NUEVO: AUTO MAPPING
    auto_map = build_auto_mapping(missing_teams, matches_teams)

    return missing_teams, auto_map


# =========================
# MATCH-LEVEL COVERAGE
# =========================

def analyze_match_level(missing_teams):

    df = pd.read_sql("""
        SELECT match_key, home_team, away_team
        FROM upcoming_matches
    """, engine)

    missing_matches = []

    for _, row in df.iterrows():

        home = normalize_team(row.home_team)
        away = normalize_team(row.away_team)

        if home in missing_teams or away in missing_teams:
            missing_matches.append(row["match_key"])

    print(f"\n🚨 Matches affected: {len(missing_matches)}")

    return missing_matches


# =========================
# MAIN
# =========================

if __name__ == "__main__":

    missing_teams, auto_map = analyze()
    analyze_match_level(missing_teams)

    print("\n📌 COPY THIS INTO team_name_map.py:\n")

    for k, v in auto_map.items():
        print(f'"{k}": "{v}",')