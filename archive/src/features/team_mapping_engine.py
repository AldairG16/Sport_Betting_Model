import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from rapidfuzz import process, fuzz
from sqlalchemy import text
from config.database import engine


def get_db_teams():

    df = pd.read_sql("""

    SELECT DISTINCT home_team AS team
    FROM upcoming_matches

    UNION

    SELECT DISTINCT away_team AS team
    FROM upcoming_matches

    """, engine)

    return df["team"].tolist()


def load_mapping():

    df = pd.read_sql("""

    SELECT api_name, db_name
    FROM team_name_mapping

    """, engine)

    return dict(zip(df.api_name, df.db_name))


def save_mapping(api_name, db_name):

    with engine.begin() as conn:

        conn.execute(text("""

        INSERT INTO team_name_mapping (api_name, db_name)

        VALUES (:api_name, :db_name)

        ON CONFLICT (api_name) DO NOTHING

        """), {

            "api_name": api_name,
            "db_name": db_name

        })


def match_team(api_name, db_teams, mapping, threshold=85):

    # 1️⃣ revisar mapping existente
    if api_name in mapping:
        return mapping[api_name]

    # 2️⃣ fuzzy matching
    match, score, _ = process.extractOne(
        api_name,
        db_teams,
        scorer=fuzz.token_sort_ratio
    )

    if score >= threshold:

        print("AUTO MATCH:", api_name, "→", match, "| score:", score)

        save_mapping(api_name, match)

        return match

    # 3️⃣ no match encontrado
    print("NO MATCH FOUND:", api_name)

    return api_name