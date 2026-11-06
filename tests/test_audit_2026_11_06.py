"""
Auditoría del 6-nov-26 (v2.9.1). El dueño: "corre una auditoría completa del
sistema, necesito que me entregues el sistema 100% funcional sin ningún bug
silencioso". Lo que encontró, cada uno con su prueba:

  1. Tigres vs Cruz Azul pasó de las 23:00 a la 01:00: el horario viejo, ya
     anulado, seguía en Próximos, en "Partidos de hoy" y en el conteo de
     picks; en una liga con pago anticipado habría entrado al parlay (dos
     veces el mismo partido). En Anteriores, Lyon vs Angers, PSG vs Lyon y
     América vs Pachuca salían con el pick anulado en vez del de verdad.
  2. Antes del partido, la anulación decidía el horario bueno por lo último
     escrito; el closing reescribe los dos a la vez y ganaba el más tardío:
     un partido adelantado habría perdido su horario real.
  3. Preston vs "sheffield utd" entró junto a Preston vs Sheffield United
     (3-nov): la fusión de repetidos solo corría los lunes.
  4. El pick de antes de la alineación se liquidaba sin el pago anticipado.
Sin DB.
"""


import pandas as pd
import pytest

from src.models import early_payout as EP
from src.models import program_parlays as PL
from src.models import program_picks as PP
from src.models import rescheduled as R
from src.models import side_bias as SB
from tests.fake_db import FakeEngine
from tests.test_program_parlays import _mvs, _up

OLD = pd.Timestamp("2030-01-05 19:45", tz="UTC")
NEW = pd.Timestamp("2030-01-05 21:45", tz="UTC")
VOIDED = pd.Timestamp("2030-01-05 06:00", tz="UTC")


# ─── 1. el horario viejo de un reprogramado ──────────────────────────────────

def test_a_voided_old_slot_is_dropped_only_when_the_match_has_a_valid_one():
    latest = {("napoli vs lecce", str(OLD)): {"skipped_at": VOIDED},
              ("napoli vs lecce", str(NEW)): {"skipped_at": None},
              ("roma vs genoa", str(OLD)): {"skipped_at": VOIDED}}         # la corrida ya no lo evaluó
    kept = SB.drop_phantoms(latest)
    assert set(kept) == {("napoli vs lecce", str(NEW)), ("roma vs genoa", str(OLD))}


def test_the_pick_is_saved_once_at_the_new_time():
    mvs = pd.DataFrame([_mvs(kick="2030-01-05 19:45", skipped_at=VOIDED),
                        _mvs(kick="2030-01-05 21:45", skipped_at=None)])
    rows = PP.build_picks(mvs, pd.DataFrame([_up()]))
    assert [(r["match"], pd.Timestamp(r["match_date"])) for r in rows] == [("napoli vs lecce", NEW)]
    # sin otra fila, el que ya no se evalúa se sigue viendo con su motivo (nunca se esconde)
    (r,) = PP.build_picks(pd.DataFrame([_mvs(skipped_at=VOIDED)]), pd.DataFrame([_up()]))
    assert r["market"] is None and r["reason"] == PP.STALE


def test_an_analysis_that_no_longer_counts_is_never_a_parlay_leg():
    up = pd.DataFrame([_up(), _up(home="roma", away="genoa"), _up(home="inter", away="lecce"),
                       _up(home="milan", away="como")])
    games = [("napoli vs lecce", "2030-01-05 19:45"), ("roma vs genoa", "2030-01-05 19:45"),
             ("inter vs lecce", "2030-01-05 19:45"), ("milan vs como", "2030-01-05 19:45")]
    mvs = [_mvs(match=m, kick=k) for m, k in games]
    built = PL.build_parlays(pd.DataFrame(mvs), up)
    assert {x["match"] for p in built for x in p["legs"]} == {m for m, _ in games}
    # el horario viejo de Napoli (anulado) junto al nuevo: Napoli una sola vez
    phantom = pd.DataFrame(mvs + [_mvs(kick="2030-01-05 17:00", skipped_at=VOIDED)])
    for p in PL.build_parlays(phantom, up):
        names = [x["match"] for x in p["legs"]]
        assert len(names) == len(set(names))
        assert all(pd.Timestamp(x["match_date"]) != pd.Timestamp("2030-01-05 17:00", tz="UTC") for x in p["legs"])


def test_proximos_shows_a_rescheduled_match_once():
    from dashboard import visibility as V
    base = {"league": "soccer_mexico_ligamx", "p_home": 0.4, "p_draw": 0.3, "p_away": 0.3,
            "pin_home": 0.38, "pin_draw": 0.28, "pin_away": 0.34, "lam_home": 1.4, "lam_away": 1.2,
            "p_over25": 0.55, "p_btts": 0.55, "created_at": pd.Timestamp("2030-01-04", tz="UTC")}
    mvs = pd.DataFrame([{**base, "match": "tigres uanl vs cruz azul", "match_date": OLD, "skipped_at": VOIDED},
                        {**base, "match": "tigres uanl vs cruz azul", "match_date": NEW, "skipped_at": None}])
    up = pd.DataFrame(columns=["home_team_norm", "away_team_norm"])
    (m,) = V.predictions(mvs, up, set())
    assert m["match_date"].startswith("2030-01-05T21:45") and not m["skipped"]


def test_the_boards_and_the_lineup_alert_skip_the_voided_slot():
    import inspect
    from src.models import lineup_alerts as LA
    from src.models import today_board as TB
    assert "result IS DISTINCT FROM 'stale'" in TB._SQL
    assert "result IS DISTINCT FROM 'stale'" in inspect.getsource(LA.load_pick)
    import dashboard.app as app
    assert "result IS DISTINCT FROM 'stale'" in inspect.getsource(app.api_predictions)


def test_anteriores_shows_the_real_pick_not_the_voided_one():
    from dashboard import board as B
    matches = pd.DataFrame([{"date": pd.Timestamp("2026-11-01"), "league": "soccer_france_ligue_one", "h": "lyon",
                             "a": "angers", "home_goals": 2, "away_goals": 0, "home_corners": 5, "away_corners": 2,
                             "home_yellow": 1, "away_yellow": 2, "home_red": 0, "away_red": 0}])
    pick = {"match": "lyon vs angers", "label": "Gana el local", "market": "home_win", "family": "Ganador",
            "reason": None, "model_prob": 0.6, "market_prob": 0.62, "source": None, "playdoit_odds": 1.55}
    for order in ((0, 1), (1, 0)):                    # sin importar el orden en que lleguen de la base
        rows = [{**pick, "match_date": pd.Timestamp("2026-10-31 19:45", tz="UTC"), "result": "stale", "profit": None},
                {**pick, "match_date": pd.Timestamp("2026-11-01 16:15", tz="UTC"), "result": "win", "profit": 0.55}]
        picks = pd.DataFrame([rows[i] for i in order])
        (r,) = B.past_matches(matches, pd.DataFrame(), picks, pd.DataFrame(), normalize=lambda t: str(t).lower())
        assert r["pick"]["result"] == "win"


# ─── 2. qué horario vale antes del partido ───────────────────────────────────

def test_before_kickoff_the_odds_feed_decides_not_the_tie_in_what_was_written():
    m = "tigres uanl vs cruz azul"
    late, early = pd.Timestamp("2026-11-08 01:00", tz="UTC"), pd.Timestamp("2026-11-07 23:00", tz="UTC")
    same = pd.Timestamp("2026-11-06 18:01", tz="UTC")                 # el closing escribió los dos a la vez
    rows = [{"match": m, "match_date": late, "written": same}, {"match": m, "match_date": early, "written": same}]
    before = pd.Timestamp("2026-11-07 12:00", tz="UTC")
    # adelantado: el feed ya lo trae a las 23:00 → el de la 01:00 es el viejo
    assert R.phantom_dates(rows, feed={m: [early]}, now=before) == {(m, late)}
    # ESPN manda sobre el feed
    assert R.phantom_dates(rows, espn={m: [late]}, feed={m: [early]}, now=before) == {(m, early)}
    # antes del partido, sin ESPN y con el feed en duda (dos horarios en la misma foto), no se anula nada:
    # no tiene vuelta. Ya jugado, como antes: lo escrito, y en empate la más tardía
    assert R.phantom_dates(rows, feed={m: [early, late]}, now=before) == set()
    assert R.phantom_dates(rows, now=before) == set()
    assert R.phantom_dates(rows, now=pd.Timestamp("2026-11-09", tz="UTC")) == {(m, early)}


def test_the_closing_voids_the_old_slot_before_saving_the_picks(monkeypatch):
    from scripts import orchestrator as O
    import src.models.program_parlays as PLm
    import src.models.program_picks as PPm
    calls = []
    monkeypatch.setattr(O, "_void_rescheduled", lambda: calls.append("void"))
    monkeypatch.setattr(PPm, "snapshot_picks", lambda: calls.append("picks"))
    monkeypatch.setattr(PLm, "snapshot_parlays", lambda: calls.append("parlays"))
    O.step_program_picks()
    assert calls == ["void", "picks", "parlays"]


# ─── 3. el mismo partido con un equipo escrito distinto ──────────────────────

def test_sheffield_utd_is_sheffield_united():
    from src.utils.team_normalizer import normalize_team
    assert normalize_team("Sheffield Utd") == normalize_team("Sheffield United") == "sheffield united"
    assert normalize_team("Sheffield Weds") == "sheffield weds"                  # el otro club no se toca


@pytest.mark.parametrize("fn", ["run_evening", "run_results_only"])
def test_duplicates_are_merged_every_time_results_arrive_before_settling(monkeypatch, fn):
    from tests.test_program_picks import _steps
    names, steps, O = _steps(monkeypatch, fn)
    assert names.index("Partidos repetidos") < names.index("Results")
    assert names.index("Partidos repetidos") > names.index("Fetch results (fbdata backup)")
    assert steps["Partidos repetidos"] is O.step_merge_duplicates


def test_a_match_with_one_team_renamed_is_flagged():
    from src.utils.output_checks import DATA_HEALTH_SQL, data_health
    assert "renamed" in DATA_HEALTH_SQL
    (msg,) = data_health({"mirror": 0, "future": 0, "duplicate": 0, "renamed": 1}, {})
    assert "un equipo escrito distinto" in msg
    assert data_health({"mirror": 0, "future": 0, "duplicate": 0, "renamed": 0}, {}) == []


# ─── 4. el pick de antes de la alineación con el pago anticipado ─────────────

def test_the_pre_lineup_pick_gets_the_early_payout_too(monkeypatch):
    import src.models.save_bets as sb
    eng = FakeEngine()
    kick = pd.Timestamp("2030-01-05 19:45", tz="UTC")
    pending = pd.DataFrame([{"id": 9, "match": "napoli vs lecce", "match_date": kick, "market": "home_win",
                             "league": "soccer_italy_serie_a", "playdoit_odds": 1.36}])
    matches = pd.DataFrame([{"home_team_l": "napoli", "away_team_l": "lecce", "date": kick.tz_convert(None),
                             "home_goals": 2, "away_goals": 3, "home_corners": None, "away_corners": None,
                             "home_yellow": None, "away_yellow": None, "home_goals_ht": None, "away_goals_ht": None,
                             "home_goals_h2": None, "away_goals_h2": None, "home_shots_target": None,
                             "away_shots_target": None}])
    monkeypatch.setattr(PP.pd, "read_sql", lambda *a, **k: pending)
    monkeypatch.setattr(sb, "_preload_matches", lambda dates: matches)
    monkeypatch.setattr(sb, "normalize_team", lambda t: t)
    monkeypatch.setattr(EP, "load_leads", lambda *a, **k: {"napoli vs lecce": [(kick, 2, 1)]})
    assert PP._resolve_pre_lineup(eng) == 1
    (sql, plan), = eng.statements("UPDATE program_picks SET pre_lineup_result = :result")
    assert plan[0]["result"] == "win" and plan[0]["profit"] == pytest.approx(0.36)


# ─── 5. el mismo partido con el nombre de antes de un alias ──────────────────

def test_the_row_with_the_old_name_of_a_club_is_voided_when_the_new_one_exists():
    kick = pd.Timestamp("2026-10-22 03:10", tz="UTC")
    rows = [{"table": "program_picks", "match": "santos laguna vs pumas unam", "match_date": kick},
            {"table": "program_picks", "match": "santos laguna vs unam pumas", "match_date": kick},
            # en las propuestas solo está el nombre viejo: es la única y se queda
            {"table": "model_proposals", "match": "santos laguna vs pumas unam", "match_date": kick},
            {"table": "program_picks", "match": "atlante fc vs pumas unam", "match_date": kick},   # sin gemelo
            {"table": "program_picks", "match": "roma vs genoa", "match_date": kick}]
    alias = {"pumas unam": "unam pumas", "atlante fc": "atlante"}
    assert R.alias_twins(rows, lambda t: alias.get(t, t)) == {("program_picks", "santos laguna vs pumas unam", kick)}


def test_void_also_voids_the_alias_twin(monkeypatch):
    import contextlib
    from src.utils import team_normalizer as TN
    kick = pd.Timestamp("2026-10-22 03:10", tz="UTC")
    rows = [{"match": "santos laguna vs pumas unam", "match_date": kick, "written": kick},
            {"match": "santos laguna vs unam pumas", "match_date": kick, "written": kick}]

    def fake_read_sql(sql, engine, params=None):
        if "FROM program_picks " in str(sql):
            return pd.DataFrame(rows)
        if "FROM model_proposals " in str(sql):                 # solo el nombre viejo: no se toca
            return pd.DataFrame(rows[:1])
        raise RuntimeError('relation "x" does not exist')
    log = []

    class _Conn:
        def execute(self, sql, params=None):
            log.append((str(sql), params))
            return type("Res", (), {"rowcount": 1})()

    class _Eng:
        @contextlib.contextmanager
        def begin(self):
            yield _Conn()
    monkeypatch.setattr(R.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(TN, "normalize_team", lambda t: {"pumas unam": "unam pumas"}.get(t, t))
    out = R.void_rescheduled(engine=_Eng())
    assert out["twins"] == ["santos laguna vs pumas unam"] and out["program_picks"] == 1
    assert out["model_proposals"] == 0
    assert [(sql.split()[1], p["match"]) for sql, p in log] == [("program_picks", "santos laguna vs pumas unam")]


# ─── 6. xG real con Understat caído: el respaldo de FotMob ───────────────────

def _fm(name, team_id, value, matches=9, team=None, pid=0):
    return {"ParticipantName": name, "TeamId": team_id, "ParticiantId": pid, "StatValue": value,
            "MatchesPlayed": matches, **({"TeamName": team} if team else {})}


def test_fotmob_lists_become_season_xg_rows():
    from src.features import soccerdata_feed as SF
    teams, players = SF.parse_fotmob(
        [_fm("Arsenal", 1, 14.9), _fm("Chelsea", 2, 12.0, matches=0)],          # sin partidos: no sirve
        [_fm("Arsenal", 1, 7.2), _fm("Chelsea", 2, 9.0)],
        [_fm("Bukayo Saka", 1, 5, team="Arsenal", pid=7), _fm("Sin goles", 1, 0, team="Arsenal", pid=8)],
        [_fm("Bukayo Saka", 1, 4.1, pid=7)])
    assert teams == [{"team": "Arsenal", "xg_for": 14.9, "xg_against": 7.2, "matches": 9}]
    assert players == [{"player": "Bukayo Saka", "team": "Arsenal", "goals": 5, "matches": 9, "xg": 4.1}]


def test_fotmob_refuses_another_season():
    from src.features import soccerdata_feed as SF

    class R:
        def __init__(self, d): self.d = d
        def raise_for_status(self): pass
        def json(self): return self.d
    league = {"details": {"selectedSeason": "2025/2026"}, "stats": {"teams": [], "players": []}}
    with pytest.raises(ValueError):
        SF.fetch_fotmob(47, "2026", get=lambda url: R(league))
    league["details"]["selectedSeason"] = "2026/2027"
    league["stats"]["teams"] = [{"name": "expected_goals_team", "fetchAllUrl": "for"},
                                {"name": "expected_goals_conceded_team", "fetchAllUrl": "against"}]
    lists = {"for": [_fm("Arsenal", 1, 14.9)], "against": [_fm("Arsenal", 1, 7.2)]}
    teams, players = SF.fetch_fotmob(47, "2026", get=lambda url: R(league if "fotmob.com/api" in url
                                                                    else {"TopLists": [{"StatList": lists[url]}]}))
    assert teams[0]["xg_for"] == 14.9 and players == []


def test_a_league_understat_did_not_give_comes_from_fotmob(monkeypatch):
    from src.features import soccerdata_feed as SF
    saved = []
    monkeypatch.setattr(SF, "fetch_understat", lambda league, season: (_ for _ in ()).throw(ValueError("html")))
    monkeypatch.setattr(SF, "_save", lambda sk, season, teams, players, source: saved.append((sk, source)) or (1, 0))
    monkeypatch.setattr(SF, "current_season", lambda: "2026")
    res = SF.refresh_understat(seasons=["2026"], verbose=False,
                               fotmob=lambda lid, season: ([{"team": "x", "xg_for": 1, "xg_against": 1, "matches": 1}], []))
    assert res["status"] == "ok" and sorted(res["via_fotmob"]) == sorted(SF.FOTMOB_LEAGUES)
    assert {s for _, s in saved} == {"fotmob"}
    # si FotMob tampoco responde, sigue "failed" y el aviso de vencimiento de siempre
    res = SF.refresh_understat(seasons=["2026"], verbose=False,
                               fotmob=lambda lid, season: (_ for _ in ()).throw(ConnectionError()))
    assert res["status"] == "failed" and res["via_fotmob"] == []


def test_the_freshest_row_of_a_team_wins_between_sources():
    from src.features import soccerdata_feed as SF
    from src.utils.team_normalizer import normalize_team
    now = pd.Timestamp("2026-11-20", tz="UTC")
    df = pd.DataFrame([
        {"team": "Deportivo La Coruna", "season": "2026", "xg_for": 15.0, "xg_against": 12.0, "matches": 10,
         "updated_at": pd.Timestamp("2026-10-26", tz="UTC"), "source": "understat"},          # ya vencido
        {"team": "Deportivo A Coruña", "season": "2026", "xg_for": 14.0, "xg_against": 13.0, "matches": 11,
         "updated_at": pd.Timestamp("2026-11-19", tz="UTC"), "source": "fotmob"}])
    x = SF.real_xg_of(df, "la coruna", normalize_team, now=now)
    assert x["source"] == "fotmob" and x["matches"] == 11
    assert normalize_team("Deportivo Alavés") == "alaves"


def test_fotmob_xg_counts_as_real_xg_in_the_pipeline():
    from src.pipeline.prediction_pipeline import xg_source_label
    fm, us = {"source": "fotmob"}, {"source": "understat"}
    assert xg_source_label(fm, fm) == "fotmob" and xg_source_label(us, us) == "understat"
    assert xg_source_label(us, fm) == "fotmob+understat"
    assert xg_source_label(fm, {"source": "proxy"}) == "proxy" and xg_source_label(None, fm) == "proxy"
