"""
Identidad de los datos (1-oct-26): partidos guardados con la liga de otra y
el mismo club escrito distinto según la fuente.

  - football-data.co.uk redirige un archivo que no existe a otro parecido:
    KOR → NOR (Noruega como K-League), ECL → EC (la 5ª inglesa como
    Champions) y, con la temporada sin publicar, E0 → EC y SP1 → SC1 / P1.
  - "man united" / "manchester united": el mismo partido dos veces y la
    historia del club repartida en dos nombres.
"""

from collections import Counter

import pandas as pd
import pytest

from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine, FakeResult


@pytest.fixture(autouse=True)
def _clean_issues():
    RUN_ISSUES.reset()
    yield
    RUN_ISSUES.reset()


def _errors(text: str) -> list:
    return [e for e in RUN_ISSUES.errors() if text in e]


# ============================================================
# Descarga segura de football-data
# ============================================================

class _R:
    def __init__(self, url, status=200, content=b""):
        self.url, self.status_code, self.content = url, status, content


CSV = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\n"
       + "E0,16/08/2025,Liverpool,Bournemouth,4,2\n" * 10).encode()
E0 = "https://www.football-data.co.uk/mmz4281/2526/E0.csv"


def test_a_redirect_to_another_file_means_the_file_does_not_exist():
    from src.utils.football_data import fetch_csv, is_missing
    url = "https://www.football-data.co.uk/mmz4281/2526/ECL.csv"
    df, why = fetch_csv(url, get=lambda u, timeout: _R(
        "https://football-data.co.uk/mmz4281/2526/EC.csv", 200, CSV))
    assert df is None and "redirige ECL.csv a EC.csv" in why
    assert is_missing(why)                       # igual que un 404: no es una falla


def test_the_host_redirect_to_the_same_file_is_fine():
    from src.utils.football_data import fetch_csv
    df, why = fetch_csv(E0, get=lambda u, timeout: _R(
        "https://football-data.co.uk/mmz4281/2526/E0.csv", 200, CSV))
    assert why == "" and len(df) == 10 and set(df["Div"]) == {"E0"}


@pytest.mark.parametrize("resp,reason,missing", [
    (_R(E0, 404, b"x" * 500), "HTTP 404", True),
    (_R(E0, 500, b"x" * 500), "HTTP 500", False),
    (_R(E0, 200, b"Div"), "archivo vacío", True),
])
def test_fetch_reasons(resp, reason, missing):
    from src.utils.football_data import fetch_csv, is_missing
    df, why = fetch_csv(E0, get=lambda u, timeout: resp)
    assert df is None and why == reason and is_missing(why) is missing


def test_a_failed_download_is_a_failure_not_a_missing_file():
    from src.utils.football_data import fetch_csv, is_missing

    def boom(u, timeout):
        raise TimeoutError("read timed out")
    df, why = fetch_csv(E0, get=boom)
    assert df is None and why.startswith("descarga falló: TimeoutError") and not is_missing(why)


def test_check_division():
    from src.utils.football_data import check_division
    assert check_division(pd.DataFrame({"Div": ["E0", "E0"]}), "E0") is None
    assert check_division(pd.DataFrame({"Div": ["EC"]}), "E0") == "E0.csv trae Div=['EC']"
    assert "no trae la columna Div" in check_division(pd.DataFrame({"x": [1]}), "E0")


def test_parse_dates_both_formats():
    from src.utils.football_data import parse_dates
    d = parse_dates(pd.Series(["16/08/2025", "08/08/15", None]))
    assert list(d[:2].dt.strftime("%Y-%m-%d")) == ["2025-08-16", "2015-08-08"] and pd.isna(d[2])


# ============================================================
# Cargadores
# ============================================================

def test_no_loader_asks_for_files_football_data_does_not_have():
    from scripts.fetch_results_backup_fbdata import LEAGUES
    from scripts.load_extra_leagues import EXTRA_LEAGUES
    from scripts.load_historical_data import leagues
    assert "soccer_uefa_champs_league" not in leagues.values() and "ECL" not in leagues
    assert LEAGUES == leagues                     # una sola fuente para los dos cargadores
    assert "KOR" not in EXTRA_LEAGUES


def test_historical_loader_skips_a_wrong_division_and_reports_failed_downloads(monkeypatch):
    import scripts.load_historical_data as lhd
    inserted = []
    monkeypatch.setattr(lhd, "_ensure_cards_schema", lambda: None)
    monkeypatch.setattr(lhd, "engine", FakeEngine(lambda sql, p: FakeResult(rows=[])))
    monkeypatch.setattr(lhd, "leagues", {"E0": "soccer_epl", "SP1": "soccer_spain_la_liga",
                                         "D1": "soccer_germany_bundesliga"})
    monkeypatch.setattr(lhd, "seasons", ["2627"])
    answers = {"E0": (pd.DataFrame({"Div": ["EC"]}), ""),
               "SP1": (None, "descarga falló: ReadTimeout: read timed out"),
               "D1": (None, "no existe: el servidor redirige D1.csv a D2.csv")}
    monkeypatch.setattr(lhd, "fetch_csv", lambda url: answers[url.rsplit("/", 1)[-1][:-4]])
    monkeypatch.setattr(lhd, "insert_ignore_conflicts", lambda *a, **k: inserted.append(a))
    lhd.load_historical_data()
    assert inserted == []
    assert _errors("soccer_epl 2627: E0.csv trae Div=['EC'] — no se carga")
    (msg,) = _errors("descargas fallaron")       # la temporada sin publicar (D1) no es falla
    assert msg.startswith("❌ Histórico: 1 descargas fallaron") and "SP1 2627" in msg


def test_extra_league_redirect_or_other_country_is_an_error(monkeypatch):
    import scripts.load_extra_leagues as lel
    monkeypatch.setattr(lel, "fetch_csv", lambda url: (None, "no existe: el servidor redirige KOR.csv a NOR.csv"))
    assert lel._download_csv("KOR") is None and _errors("Liga extra KOR: no existe")
    monkeypatch.setattr(lel, "fetch_csv", lambda url: (pd.DataFrame({"Country": ["Norway"]}), ""))
    assert lel._download_csv("SWE") is None
    assert _errors("SWE.csv trae Country=['Norway'] (se esperaba Sweden)")
    monkeypatch.setattr(lel, "fetch_csv", lambda url: (pd.DataFrame({"Country": ["Sweden"]}), ""))
    assert lel._download_csv("SWE") is not None


def test_daily_backup_skips_an_unpublished_season_quietly(monkeypatch):
    import scripts.fetch_results_backup_fbdata as fb
    monkeypatch.setattr(fb, "fetch_csv", lambda url: (None, "no existe: el servidor redirige SP1.csv a SC1.csv"))
    assert fb._download_csv("2627", "SP1") is None and RUN_ISSUES.errors() == []
    monkeypatch.setattr(fb, "fetch_csv", lambda url: (pd.DataFrame({"Div": ["SC1"]}), ""))
    assert fb._download_csv("2627", "SP1") is None and _errors("SP1.csv trae Div=['SC1']")


def test_league_factors_after_the_label_fixes():
    from src.features.league_calibration import DEFAULT_FACTORS, LEAGUE_FACTORS, get_btts_rate
    assert LEAGUE_FACTORS["soccer_korea_kleague1"] == DEFAULT_FACTORS          # neutra: sin muestra
    nor = LEAGUE_FACTORS["soccer_norway_eliteserien"]                          # lo que r16 midió
    assert (nor["tempo"], nor["over25_rate"], nor["btts_rate"]) == (1.212, 0.582, 0.557)
    assert get_btts_rate("soccer_uefa_champs_league") == DEFAULT_FACTORS["btts_rate"]


# ============================================================
# Reetiquetado de ligas (scripts/fix_league_labels.py)
# ============================================================

def test_relabel_uses_the_real_league_csv():
    from scripts.fix_league_labels import match_keys, plan_relabels
    nor = pd.DataFrame({"Date": ["25/03/2012", "26/03/2012"], "Home": ["Brann", "Viking"],
                        "Away": ["Molde", "Start"]})
    rows = pd.DataFrame([
        {"id": 1, "d": "2012-03-25", "league": "soccer_korea_kleague1", "home_team": "brann", "away_team": "molde"},
        {"id": 2, "d": "2026-05-01", "league": "soccer_korea_kleague1", "home_team": "ulsan", "away_team": "jeonbuk"}])
    assert plan_relabels(rows, {"soccer_norway_eliteserien": match_keys(nor, "Home", "Away")}) == \
        {1: "soccer_norway_eliteserien"}


def test_relabel_never_moves_a_match_found_in_its_own_league():
    from scripts.fix_league_labels import plan_relabels
    key = ("2026-08-09", "porto", "alverca")
    rows = pd.DataFrame([{"id": 7, "d": "2026-08-09", "league": "soccer_spain_la_liga",
                          "home_team": "porto", "away_team": "alverca"}])
    sources = {"soccer_portugal_primeira_liga": {key}, "soccer_spain_la_liga": {key}}
    assert plan_relabels(rows, sources, own={"soccer_spain_la_liga": {key}}) == {}
    assert plan_relabels(rows, sources, own={"soccer_spain_la_liga": set()}) == \
        {7: "soccer_portugal_primeira_liga"}


def test_rows_are_backed_up_before_being_touched():
    from src.utils.match_backup import backup_matches
    eng = FakeEngine()
    with eng.begin() as conn:
        backup_matches(conn, [3, 1], "relabel_league", delete_ids=[1])
    assert eng.executed[0][0].startswith("CREATE TABLE IF NOT EXISTS matches_identity_backup")
    sql, params = eng.executed[1]
    assert sql.startswith("INSERT INTO matches_identity_backup SELECT m.*")
    assert params == {"ids": [3, 1], "del": [1], "action": "relabel_league"}


# ============================================================
# Respaldo ESPN: completa la fila existente, no crea repetidos
# ============================================================

def _espn_world(monkeypatch, existing):
    import scripts.backfill_espn_results as be
    import scripts.resolve_stale_bets as rsb

    def respond(sql, params):
        if sql.startswith("SELECT id, home_goals, away_goals FROM matches"):
            return FakeResult(rows=[existing] if existing else [])
        return FakeResult()
    eng = FakeEngine(respond)
    monkeypatch.setattr(be, "engine", eng)
    monkeypatch.setattr(be.pd, "read_sql", lambda *a, **k: pd.DataFrame([{
        "id": 1, "match": "gais vs kalmar ff", "market": "h1_home",
        "match_date": pd.Timestamp("2026-05-30 15:00"), "league": "soccer_sweden_allsvenskan"}]))
    monkeypatch.setattr(be, "fetch_espn_scoreboard", lambda slug, d: [] if d.day != 31 else [{
        "home": "GAIS", "away": "Kalmar FF", "home_goals": 3, "away_goals": 0,
        "home_goals_ht": 1, "away_goals_ht": 0, "date": "2026-05-31"}])
    monkeypatch.setattr(rsb, "resolve_stale_bets", lambda **k: {})
    return be, eng


def test_espn_fills_the_existing_row_of_the_day_before(monkeypatch):
    """ESPN listaba el partido el 31 y la fila está el 30: antes se insertaba
    un repetido con fecha 31."""
    from types import SimpleNamespace
    be, eng = _espn_world(monkeypatch, SimpleNamespace(id=55, home_goals=3, away_goals=0))
    stats = be.backfill_espn_results(verbose=False)
    assert stats["backfilled"] == 1 and stats["score_mismatch"] == 0
    (sql, params), = eng.statements("UPDATE matches SET")
    assert params["id"] == 55 and params["hht"] == 1
    assert not eng.statements("INSERT INTO matches")


def test_espn_with_another_score_does_not_touch_the_row(monkeypatch):
    from types import SimpleNamespace
    be, eng = _espn_world(monkeypatch, SimpleNamespace(id=55, home_goals=2, away_goals=2))
    stats = be.backfill_espn_results(verbose=False)
    assert stats["score_mismatch"] == 1 and stats["backfilled"] == 0
    assert not eng.statements("UPDATE matches") and not eng.statements("INSERT INTO matches")


def test_espn_inserts_only_when_there_is_no_row(monkeypatch):
    be, eng = _espn_world(monkeypatch, None)
    be.backfill_espn_results(verbose=False)
    (sql, params), = eng.statements("INSERT INTO matches")
    assert params["h"] == "gais" and params["a"] == "kalmar ff" and params["d"] == "2026-05-31"


# ============================================================
# Identidad de equipos (src/utils/team_identity.py)
# ============================================================

def _rows(*t):
    return pd.DataFrame(t, columns=["id", "d", "h", "a", "hg", "ag"])


def test_compatible_names():
    from src.utils.team_identity import compatible
    assert compatible("leeds", "leeds united") and compatible("goztep", "goztepe")
    assert compatible("sheffield weds", "sheffield wednesday")
    assert not compatible("real madrid", "real betis")


def test_the_same_match_reveals_the_other_name():
    from src.utils.team_identity import find_alias_pairs
    pairs = find_alias_pairs(_rows(
        (1, "2026-01-10", "leeds", "derby", 2, 1),
        (2, "2026-01-10", "leeds", "derby county", 2, 1),
        (3, "2026-01-11", "x", "leeds united", 0, 0),
        (4, "2026-01-12", "x", "leeds", 0, 0),          # ±1 día (zona horaria)
        (5, "2026-01-10", "y", "z", 3, 3)))
    assert pairs == Counter({("derby", "derby county"): 1, ("leeds", "leeds united"): 1})


def test_classify_pairs_with_deny_allow_and_review():
    from src.utils.team_identity import classify_pairs
    accepted, review = classify_pairs(Counter({
        ("leeds", "leeds united"): 5, ("paris fc", "paris saint germain"): 27,
        ("ath bilbao", "athletic club"): 3, ("arsenal sarandi", "atl rafaela"): 1}))
    assert accepted == [("ath bilbao", "athletic club"), ("leeds", "leeds united")]
    assert review == [(("arsenal sarandi", "atl rafaela"), 1)]      # PSG / Paris FC: DENY


def test_canonical_is_the_odds_api_name_else_the_most_frequent():
    from src.utils.team_identity import build_alias_map
    alias, _, conflicts = build_alias_map(
        [("leeds", "leeds united"), ("man united", "manchester united")],
        {"leeds united", "manchester united"}, {"leeds": 500, "leeds united": 20}, lambda n: n)
    assert alias == {"leeds": "leeds united", "man united": "manchester united"} and conflicts == []
    alias, _, _ = build_alias_map([("sheffield wednesday", "sheffield weds")], set(),
                                  {"sheffield weds": 400, "sheffield wednesday": 30}, lambda n: n)
    assert alias == {"sheffield wednesday": "sheffield weds"}


def test_missing_aliases_file_means_no_learned_aliases(tmp_path):
    from src.utils.team_identity import load_learned_aliases
    assert load_learned_aliases(tmp_path / "no_existe.json") == {}


def test_a_second_round_finds_matches_where_both_names_differed():
    """"stoke / sheffield weds" y "stoke city / sheffield wednesday": sin
    ningún lado igual no hay evidencia en la primera ronda."""
    from scripts.learn_team_aliases import learn
    rows = _rows((1, "2026-04-03", "stoke", "sheffield weds", 2, 0),
                 (2, "2026-04-03", "stoke city", "sheffield wednesday", 2, 0),
                 (3, "2026-04-10", "stoke", "hull", 1, 1),
                 (4, "2026-04-10", "stoke city", "hull", 1, 1))
    freq = Counter(list(rows["h"]) + list(rows["a"]))
    _, accepted, _, alias_map, _, conflicts = learn(rows, ["stoke city"], freq)
    assert accepted == [("sheffield wednesday", "sheffield weds"), ("stoke", "stoke city")]
    assert alias_map["stoke"] == "stoke city" and conflicts == []


def test_uncovered_lists_only_pairs_the_normalizer_does_not_join():
    from scripts.learn_team_aliases import uncovered
    assert uncovered([("leeds", "leeds united"), ("club nuevo", "club nuevo fc")]) == \
        [("club nuevo", "club nuevo fc")]


def test_normalize_team_applies_the_learned_aliases_last():
    from src.utils.team_normalizer import LEARNED_TEAM_ALIASES, base_normalize_team, normalize_team
    assert normalize_team("Man United") == normalize_team("Manchester United") == "manchester united"
    assert normalize_team("Fatih Karagümrük") == normalize_team("Karagumruk")
    assert base_normalize_team("man united") == "man united"
    for canonical in set(LEARNED_TEAM_ALIASES.values()):           # el canónico no se mueve
        assert normalize_team(canonical) == canonical


# ============================================================
# Reparación de identidades (scripts/fix_team_identities.py)
# ============================================================

def test_paris_labels_follow_the_original_csv():
    from scripts.fix_team_identities import paris_corrections, paris_truth
    truth = paris_truth(pd.DataFrame({
        "Date": ["17/08/2025", "04/01/2026"], "HomeTeam": ["Angers", "Paris SG"],
        "AwayTeam": ["Paris FC", "Paris FC"], "FTHG": [1, 2], "FTAG": [0, 1]}))
    rows = pd.DataFrame([
        {"id": 1, "date": "2025-08-17", "home_team": "angers", "away_team": "paris saint germain",
         "home_goals": 1, "away_goals": 0},
        {"id": 2, "date": "2025-08-17", "home_team": "angers", "away_team": "paris",
         "home_goals": 1, "away_goals": 0},
        {"id": 3, "date": "2026-01-04", "home_team": "paris sg", "away_team": "paris saint germain",
         "home_goals": 2, "away_goals": 1},                          # el derbi, mal etiquetado
        {"id": 4, "date": "2025-08-18", "home_team": "angers", "away_team": "paris fc",
         "home_goals": 1, "away_goals": 0}])                         # ±1 día, ya estaba bien
    fixes, unresolved = paris_corrections(rows, truth)
    assert fixes == {1: {"away_team": "paris fc"}, 2: {"away_team": "paris fc"},
                     3: {"home_team": "paris saint germain", "away_team": "paris fc"}}
    assert unresolved == 0


def test_rename_map_canonical_for_clubs_existing_spelling_for_national_teams():
    from scripts.fix_team_identities import rename_map
    rm = rename_map({"man united": 460, "manchester united": 300, "China": 19, "China PR": 400,
                     "Japan": 500, "Korea Republic": 10})
    assert rm == {"man united": "manchester united", "China": "China PR"}
    # "Korea Republic" → canónico "south korea" sin grafía guardada: no se inventa una


def _plan_rows(rows):
    from scripts.fix_team_identities import FILL_COLS
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for c in FILL_COLS:
        if c not in df.columns:
            df[c] = None
    return df


def test_duplicates_keep_the_row_with_most_data_and_take_the_rest():
    from scripts.fix_team_identities import plan_merges, plan_names
    rows = _plan_rows([
        {"id": 10, "date": "2026-04-25", "home_team": "stoke city", "away_team": "portsmouth",
         "home_goals": 1, "away_goals": 3, "home_goals_ht": 1, "away_goals_ht": 1},
        {"id": 20, "date": "2026-04-25", "home_team": "stoke", "away_team": "portsmouth",
         "home_goals": 1, "away_goals": 3, "season": 2025, "home_corners": 12, "away_corners": 4},
        {"id": 30, "date": "2026-04-26", "home_team": "stoke", "away_team": "portsmouth",
         "home_goals": 1, "away_goals": 3, "season": 2025},          # ±1 día: el mismo partido
        {"id": 40, "date": "2025-11-01", "home_team": "stoke", "away_team": "portsmouth",
         "home_goals": 1, "away_goals": 3, "season": 2025}])         # mismo marcador, otro partido
    plan = plan_names(rows, {"stoke": "stoke city"}, {})
    keep, delete = plan_merges(plan)
    assert delete == [10, 30]
    assert list(keep) == [20] and {k: float(v) for k, v in keep[20].items()} == \
        {"home_goals_ht": 1.0, "away_goals_ht": 1.0}


def test_a_rename_that_would_face_itself_is_not_done():
    from scripts.fix_team_identities import plan_names
    rows = _plan_rows([{"id": 1, "date": "2026-01-04", "home_team": "paris sg",
                        "away_team": "paris saint germain", "home_goals": 2, "away_goals": 1}])
    plan = plan_names(rows, {"paris sg": "paris saint germain"}, {})
    assert (plan.loc[0, "new_home"], plan.loc[0, "new_away"]) == ("paris sg", "paris saint germain")


def test_same_day_same_teams_other_score_is_left_untouched():
    from scripts.fix_team_identities import blocked_ids, plan_merges, plan_names
    rows = _plan_rows([
        {"id": 1, "date": "2026-03-31", "home_team": "Czech Republic", "away_team": "Denmark",
         "home_goals": 2, "away_goals": 2},
        {"id": 2, "date": "2026-03-31", "home_team": "Czechia", "away_team": "Denmark",
         "home_goals": 1, "away_goals": 1}])
    plan = plan_names(rows, {"Czech Republic": "Czechia"}, {})
    keep, delete = plan_merges(plan)
    assert delete == [] and blocked_ids(plan, delete) == {1, 2}


def test_renames_go_in_batches():
    from scripts.fix_team_identities import update_names
    eng = FakeEngine()
    with eng.begin() as conn:
        update_names(conn, [(i, "h", "a", "h", "a") for i in range(2500)], chunk=1000)
    assert len(eng.statements("UPDATE matches m SET home_team = v.h")) == 3
