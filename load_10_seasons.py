from src.data.collector import download_season
from config.database import LEAGUES, SEASONS


for league_name, league_id in LEAGUES.items():

    for season in SEASONS:

        print("Downloading", league_name, season)

        download_season(
            league_id,
            league_name,
            season
        )