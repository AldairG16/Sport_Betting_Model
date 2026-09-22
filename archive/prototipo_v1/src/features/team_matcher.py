from rapidfuzz import process, fuzz


def match_team_name(api_team, db_teams, threshold=85):

    """
    Encuentra el equipo más parecido en la base de datos
    """

    match, score, _ = process.extractOne(
        api_team,
        db_teams,
        scorer=fuzz.token_sort_ratio
    )

    if score >= threshold:
        return match

    return api_team

import pandas as pd
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