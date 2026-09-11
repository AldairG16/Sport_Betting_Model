"""
Backfill MANUAL verificado con fuentes web (2026-09-11).
Marcadores de partidos que dejaron bets 'stale' y no tenían fuente
automática. Cada marcador fue confirmado con ESPN/BBC/UEFA/CONMEBOL
por agentes de búsqueda; ver el log de la sesión del 11-sep-2026.

Se insertan con los nombres EXACTOS de las bets para que el resolvedor
estándar los encuentre sin ambigüedad.
"""

MANUAL_RESULTS = [
    # (home, away, gh, ga, date, league)
    ("aldosivi mar del plata", "argentinos jrs", 0, 2, "2026-03-31", "soccer_argentina_primera_division"),
    ("motherwell", "falkirk fc", 2, 3, "2026-04-04", "soccer_spl"),
    ("dundee fc", "celtic", 1, 2, "2026-04-05", "soccer_spl"),
    ("lanus", "platense", 0, 0, "2026-04-05", "soccer_argentina_primera_division"),
    ("sporting lisbon", "arsenal", 0, 1, "2026-04-07", "soccer_uefa_champs_league"),
    ("real madrid", "bayern munchen", 1, 2, "2026-04-07", "soccer_uefa_champs_league"),
    ("wrexham afc", "southampton", 1, 5, "2026-04-07", "soccer_efl_champ"),
    ("ind rivadavia", "club bolivar", 1, 0, "2026-04-07", "soccer_conmebol_copa_libertadores"),
    ("rosario central", "independiente del valle", 0, 0, "2026-04-07", "soccer_conmebol_copa_libertadores"),
    ("queretaro", "juarez", 1, 1, "2026-04-08", "soccer_mexico_ligamx"),
    ("barcelona sc", "cruzeiro", 0, 1, "2026-04-08", "soccer_conmebol_copa_libertadores"),
    ("universidad catolica (chi)", "boca juniors", 1, 2, "2026-04-08", "soccer_conmebol_copa_libertadores"),
    ("pachuca", "santos laguna", 4, 2, "2026-04-12", "soccer_mexico_ligamx"),
    ("atlas", "monterrey", 0, 0, "2026-04-12", "soccer_mexico_ligamx"),
    ("colorado rapids", "houston dynamo", 6, 2, "2026-04-12", "soccer_usa_mls"),
    ("san diego fc", "minnesota united", 1, 2, "2026-04-12", "soccer_usa_mls"),
    ("america", "cruz azul", 1, 1, "2026-04-12", "soccer_mexico_ligamx"),
    ("fortuna sittard", "nac breda", 1, 1, "2026-04-12", "soccer_netherlands_eredivisie"),
    ("nec nijmegen", "feyenoord", 1, 1, "2026-04-12", "soccer_netherlands_eredivisie"),
    ("fc zwolle", "excelsior", 2, 2, "2026-04-12", "soccer_netherlands_eredivisie"),
    ("liverpool", "paris saint germain", 0, 2, "2026-04-14", "soccer_uefa_champs_league"),
    ("ath madrid", "barcelona", 1, 2, "2026-04-14", "soccer_uefa_champs_league"),
    ("lafc", "colorado rapids", 0, 0, "2026-04-23", "soccer_usa_mls"),
    ("hiroshima sanfrecce fc", "vissel kobe", 1, 1, "2026-05-06", "soccer_japan_j_league"),
    ("kashima antlers", "mito hollyhock", 3, 0, "2026-05-06", "soccer_japan_j_league"),
    ("liaoning tieren fc", "yunnan yukun", 1, 2, "2026-05-10", "soccer_china_superleague"),
    ("zhejiang", "tianjin jinmen tiger fc", 1, 1, "2026-05-10", "soccer_china_superleague"),
    ("kilmarnock", "dundee fc", 3, 1, "2026-05-12", "soccer_spl"),
    ("falkirk fc", "rangers", 2, 5, "2026-05-16", "soccer_spl"),
    ("degerfors if", "malmo ff", 0, 1, "2026-07-04", "soccer_sweden_allsvenskan"),
    ("halmstads bk", "vasteras sk", 1, 3, "2026-07-04", "soccer_sweden_allsvenskan"),
    ("ifk goteborg", "aik", 1, 2, "2026-07-05", "soccer_sweden_allsvenskan"),
    ("if elfsborg", "hammarby if", 1, 2, "2026-07-05", "soccer_sweden_allsvenskan"),
    ("if brommapojkarna", "gais", 1, 1, "2026-07-06", "soccer_sweden_allsvenskan"),
    ("fluminense", "bragantino", 1, 1, "2026-07-22", "soccer_brazil_campeonato"),
    ("internacional", "cruzeiro", 1, 2, "2026-07-22", "soccer_brazil_campeonato"),
    # Nottingham Forest: marcador + HT (para bets h1_*)
    ("nottm forest", "newcastle", 1, 1, "2026-05-10", "soccer_epl"),
]

# HT goals donde se conocen (pares home/away → (hht, aht))
MANUAL_HT = {
    ("nottm forest", "newcastle"): (0, 0),
}


def apply_manual_backfill():
    from sqlalchemy import text
    from config.database import engine

    inserted = updated = 0
    with engine.begin() as conn:
        for home, away, gh, ga, date, league in MANUAL_RESULTS:
            hht, aht = MANUAL_HT.get((home, away), (None, None))
            r = conn.execute(text("""
                INSERT INTO matches (date, league, season, home_team, away_team,
                                     home_goals, away_goals, home_goals_ht, away_goals_ht)
                VALUES (:d, :lg, :season, :h, :a, :hg, :ag, :hht, :aht)
                ON CONFLICT (date, home_team, away_team) DO UPDATE SET
                    home_goals    = COALESCE(matches.home_goals, EXCLUDED.home_goals),
                    away_goals    = COALESCE(matches.away_goals, EXCLUDED.away_goals),
                    home_goals_ht = COALESCE(matches.home_goals_ht, EXCLUDED.home_goals_ht),
                    away_goals_ht = COALESCE(matches.away_goals_ht, EXCLUDED.away_goals_ht)
            """), {
                "d": date, "lg": league,
                "season": int(date[:4]) if int(date[5:7]) >= 8 else int(date[:4]) - 1,
                "h": home, "a": away, "hg": gh, "ag": ga, "hht": hht, "aht": aht,
            })
            if r.rowcount:
                inserted += 1
    print(f"Backfill manual: {len(MANUAL_RESULTS)} partidos procesados")

    # Resolver con la lógica estándar
    from scripts.resolve_stale_bets import resolve_stale_bets
    resolve_stale_bets(apply=True, verbose=True)


if __name__ == "__main__":
    apply_manual_backfill()
