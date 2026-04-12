import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team


def normalize_tables():

    print("🔧 NORMALIZING TEAMS...")

    # =========================
    # MATCHES
    # =========================
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, home_team, away_team 
            FROM matches
        """))
        matches = result.fetchall()

    with engine.begin() as conn:
        for m in matches:
            conn.execute(text("""
                UPDATE matches
                SET 
                    team_home_norm = :home,
                    team_away_norm = :away
                WHERE id = :id
            """), {
                "id": m.id,
                "home": normalize_team(m.home_team),
                "away": normalize_team(m.away_team)
            })

    # =========================
    # UPCOMING MATCHES
    # =========================
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT fixture_id, home_team, away_team 
            FROM upcoming_matches
        """))
        upcoming = result.fetchall()

    with engine.begin() as conn:
        for m in upcoming:
            conn.execute(text("""
                UPDATE upcoming_matches
                SET 
                    home_team_norm = :home,
                    away_team_norm = :away
                WHERE fixture_id = :id
            """), {
                "id": m.fixture_id,
                "home": normalize_team(m.home_team),
                "away": normalize_team(m.away_team)
            })

    print("✅ NORMALIZATION DONE")


if __name__ == "__main__":
    normalize_tables()