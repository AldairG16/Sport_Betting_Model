"""
Picks del programa guardados y medidos + dashboard v1.9.0 (21-oct-26).

El dueño: "cómo nos fue con las apuestas de nuestros modelos, no los veo en
el dashboard y ahorita el dashboard es un desastre". El pick de cada partido
no se guardaba en ningún lado; ahora se guarda (morning, evening y cada
closing), se liquida en la noche y la página tiene pestañas en lenguaje
simple. Sin DB.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from dashboard import visibility as V
from src.models import program_picks as PP
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine

KICK = pd.Timestamp("2030-01-05 19:00", tz="UTC")


def _mvs(match="atlas vs club america", model=(0.30, 0.22, 0.48), pin=(0.27, 0.24, 0.49),
         over=0.67, btts=0.55, created="2030-01-05 06:00"):
    return {"match": match, "match_date": KICK, "league": "soccer_mexico_ligamx",
            "p_home": model[0], "p_draw": model[1], "p_away": model[2],
            "pin_home": pin[0], "pin_draw": pin[1], "pin_away": pin[2],
            "lam_home": 1.6, "lam_away": 2.0, "p_over25": over, "p_btts": btts,
            "created_at": pd.Timestamp(created, tz="UTC")}


UP = {"home_team_norm": "atlas", "away_team_norm": "club america", "home_team": "Atlas", "away_team": "América",
      "home_odds": 4.05, "draw_odds": 3.90, "away_odds": 2.22, "pin_home_odds": None, "pin_draw_odds": None,
      "pin_away_odds": None, "over25_odds": 1.80, "under25_odds": 2.05, "consensus_over25_odds": 1.72,
      "consensus_under25_odds": 2.00, "btts_yes_odds": 1.70, "btts_no_odds": 2.10,
      "consensus_home_odds": 3.80, "consensus_draw_odds": 3.70, "consensus_away_odds": 2.10}


# ─── la regla ────────────────────────────────────────────────────────────────

def test_pick_carries_the_market_to_settle_and_the_playdoit_price():
    pk = PP.pick([0.30, 0.22, 0.48], [0.27, 0.24, 0.49], 0.67, 0.55, SimpleNamespace(**UP))
    # el mercado da ~53% a más de 2.5: el programa ve 14 pts más
    assert (pk["market"], pk["label"], pk["lean"]) == ("over25", "Más de 2.5 goles", True)
    assert pk["playdoit_odds"] == pytest.approx(1.72)              # el promedio de las casas
    assert pk["value"] is False and pk["playdoit_value"] is False   # -125 no paga el mínimo
    ratio = PP.pick([0.30, 0.22, 0.48], [0.27, 0.24, 0.49], 0.67, 0.55, SimpleNamespace(**UP), ratio=1.10)
    assert ratio["playdoit_odds"] == pytest.approx(1.892)          # lo aprendido de las capturas


def test_the_dashboard_shows_the_same_pick_that_is_saved():
    u = SimpleNamespace(**UP)
    saved = PP.pick([0.30, 0.22, 0.48], [0.27, 0.24, 0.49], 0.67, 0.55, u)
    shown = V.program_pick([0.30, 0.22, 0.48], [0.27, 0.24, 0.49], 0.67, 0.55, u)
    assert (shown["label"], shown["market_key"], shown["model"]) == (saved["label"], saved["market"], saved["model"])


# ─── guardar ─────────────────────────────────────────────────────────────────

def test_build_picks_uses_the_latest_analysis_and_canonical_names():
    mvs = pd.DataFrame([_mvs(model=(0.60, 0.20, 0.20), created="2030-01-04 06:00"),   # corrida vieja
                        _mvs(created="2030-01-05 06:00")])
    up = pd.DataFrame([{**UP, "away_team_norm": "america"}])                         # alias de hoy
    rows = PP.build_picks(mvs, up, normalize=lambda t: {"america": "club america"}.get(t, t))
    (r,) = rows
    assert r["match"] == "atlas vs club america" and r["display"] == "Atlas vs América"
    assert r["market"] == "over25" and r["model_prob"] == 0.67
    assert r["playdoit_odds"] == pytest.approx(1.72) and r["min_us"] < 0


def test_snapshot_upserts_only_matches_that_have_not_started(monkeypatch):
    eng = FakeEngine()
    reads = {"model_vs_sharp": pd.DataFrame([_mvs()]), "upcoming_matches": pd.DataFrame([UP]),
             "playdoit_prices": pd.DataFrame()}

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = str(sql)
        return next(df for k, df in reads.items() if k in s)
    monkeypatch.setattr(PP.pd, "read_sql", fake_read_sql)
    assert PP.snapshot_picks(engine=eng) == 1
    (sql, params), = eng.statements("INSERT INTO program_picks")
    assert "ON CONFLICT (match, match_date) DO UPDATE" in sql
    assert "WHERE program_picks.result IS NULL AND program_picks.match_date > NOW()" in sql
    assert params["market0"] == "over25" and params["display0"] == "Atlas vs América"


def test_a_failed_snapshot_is_an_error_that_reaches_telegram(monkeypatch):
    RUN_ISSUES.reset()

    def boom(*a, **k):
        raise RuntimeError("neon caído")
    monkeypatch.setattr(PP.pd, "read_sql", boom)
    assert PP.snapshot_picks(engine=FakeEngine()) == 0
    assert any("Picks del programa no guardados" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


# ─── liquidar ────────────────────────────────────────────────────────────────

def test_picks_settle_at_the_playdoit_price_and_without_it_only_count(monkeypatch):
    import src.models.save_bets as sb
    eng = FakeEngine()
    pending = pd.DataFrame([
        {"id": 1, "match": "atlas vs club america", "match_date": KICK, "market": "over25", "playdoit_odds": 1.72},
        {"id": 2, "match": "atlas vs club america", "match_date": KICK, "market": "btts", "playdoit_odds": None},
    ])
    matches = pd.DataFrame([{"home_team_l": "atlas", "away_team_l": "club america", "date": KICK.tz_convert(None),
                             "home_goals": 1, "away_goals": 2, "home_corners": None, "away_corners": None,
                             "home_yellow": None, "away_yellow": None, "home_goals_ht": None, "away_goals_ht": None,
                             "home_goals_h2": None, "away_goals_h2": None, "home_shots_target": None,
                             "away_shots_target": None}])
    monkeypatch.setattr(PP.pd, "read_sql", lambda *a, **k: pending)
    monkeypatch.setattr(sb, "_preload_matches", lambda dates: matches)
    monkeypatch.setattr(sb, "normalize_team", lambda t: t)
    out = PP.resolve_picks(engine=eng)
    assert out["resolved"] == 2
    (sql, plan), = eng.statements("UPDATE program_picks SET result = :result")
    by_id = {p["id"]: p for p in plan}
    assert by_id[1]["result"] == "win" and by_id[1]["profit"] == pytest.approx(0.72)
    assert by_id[2]["result"] == "win" and by_id[2]["profit"] is None       # sin momio de PlayDoit


# ─── el orquestador ─────────────────────────────────────────────────────────

def _steps(monkeypatch, fn):
    import scripts.odds_history as oh
    from scripts import orchestrator as O
    monkeypatch.setattr(oh, "capture_snapshot", lambda verbose=False: None)   # el morning la toma fuera de los pasos
    ran = []
    monkeypatch.setattr(O, "run_step", lambda logger, name, f, *a, **k: ran.append((name, f)) or True)
    getattr(O, fn)(type("L", (), {"log": lambda self, msg: None})())
    return [n for n, _ in ran], dict(ran), O


@pytest.mark.parametrize("fn,after", [("run_morning", "Predictions"), ("run_evening", "Predictions (mañana)"),
                                      ("run_closing", "Pre-kickoff closing odds")])
def test_picks_are_saved_after_every_analysis_and_every_closing(monkeypatch, fn, after):
    names, steps, O = _steps(monkeypatch, fn)
    assert names.index("Picks del programa") == names.index(after) + 1
    assert steps["Picks del programa"] is O.step_program_picks


def test_picks_are_settled_with_the_shadow_bets(monkeypatch):
    import src.models.proposals as P
    import src.models.save_bets as sb
    from scripts import orchestrator as O
    called = []
    monkeypatch.setattr(sb, "resolve_shadow_outcomes", lambda: called.append("shadow"))
    monkeypatch.setattr(P, "resolve_proposals", lambda: called.append("proposals"))
    monkeypatch.setattr(PP, "resolve_picks", lambda: called.append("picks"))
    O.step_shadow_results()
    assert called == ["shadow", "proposals", "picks"]


# ─── el tablero ──────────────────────────────────────────────────────────────

def _pick_row(result, profit, lean=True, value=False, odds=1.72, display="Atlas vs América"):
    return {"match": "atlas vs club america", "match_date": KICK, "league": "soccer_mexico_ligamx",
            "display": display, "label": "Más de 2.5 goles", "model_prob": 0.67, "market_prob": 0.53,
            "lean": lean, "min_us": -132, "playdoit_odds": odds, "value": value, "result": result, "profit": profit}


def test_picks_board_counts_wins_and_units_by_group():
    df = pd.DataFrame([_pick_row("win", 0.72), _pick_row("loss", -1.0, lean=False),
                       _pick_row("win", None, odds=None), _pick_row(None, None, value=True)])
    b = V.picks_board(df)
    assert b["all"] == {"n": 3, "won": 2, "lost": 1, "profit": -0.28, "priced": 2, "roi": -0.14}
    assert b["lean"]["n"] == 2 and b["lean"]["profit"] == 0.72
    assert b["value"] is None                                    # el que pagaba aún no se juega
    assert b["rows"][0]["playdoit_us"] == "-139" and b["rows"][3]["result"] == "Pendiente"


def test_real_money_by_stage():
    df = pd.DataFrame([
        {"match_date": "2026-09-01T18:00:00Z", "result": "win", "stake": 1.0, "profit": 0.8},
        {"match_date": "2026-09-01T18:00:00Z", "result": "stale", "stake": 1.0, "profit": 0.0},
        {"match_date": "2026-09-20T18:00:00Z", "result": "loss", "stake": 0.5, "profit": -0.5},
        {"match_date": "2026-10-02T18:00:00Z", "result": "stale", "stake": 1.0, "profit": 0.0},
    ])
    first, second, collection = V.real_periods(df)
    assert (first["n"], first["profit"], first["cancelled"]) == (1, 0.8, 1)
    assert (second["n"], second["lost"], second["staked"]) == (1, 1, 0.5)
    assert (collection["n"], collection["cancelled"]) == (0, 1)


def test_proposal_weeks_split_the_broken_model_from_the_fixed_one():
    df = pd.DataFrame([
        {"match_date": "2026-10-13T18:00:00Z", "market": "home_win", "odds": 2.0, "result": "win"},
        {"match_date": "2026-10-14T18:00:00Z", "market": "under_3.5", "odds": 1.65, "result": "win"},   # momio inventado
        {"match_date": "2026-10-20T18:00:00Z", "market": "draw", "odds": 3.4, "result": "loss"},
        {"match_date": "2026-10-23T18:00:00Z", "market": "over25", "odds": 1.9, "result": "win"},
    ])
    weeks = V.proposal_weeks(df)
    assert [(w["week"], w["old_model"], w["n"], w["profit_1u"]) for w in weeks] == [
        ("2026-10-12", True, 1, 1.0), ("2026-10-19", False, 1, 0.9), ("2026-10-19", True, 1, -1.0)]


def test_dashboard_endpoints_and_tabs(monkeypatch):
    import dashboard.app as dash

    def fake_q(sql, params=None):
        s = " ".join(str(sql).split())
        if "FROM program_picks" in s:
            return pd.DataFrame([_pick_row("win", 0.72)])
        if "FROM bets_history" in s:
            return pd.DataFrame([{"match_date": "2026-09-01T18:00:00Z", "result": "win", "stake": 1.0, "profit": 0.8}])
        if "FROM model_proposals" in s:
            return pd.DataFrame([{"match_date": "2026-10-13T18:00:00Z", "market": "home_win", "odds": 2.0,
                                  "result": "win"}])
        raise AssertionError(s[:80])
    monkeypatch.setattr(dash, "_q", fake_q)
    c = dash.app.test_client()
    p = c.get("/api/picks").get_json()
    assert p["ok"] and p["all"]["won"] == 1 and p["rows"][0]["match"] == "Atlas vs América"
    sb = c.get("/api/scoreboard").get_json()
    assert sb["ok"] and sb["real"][0]["profit"] == 0.8 and sb["weeks"][0]["old_model"] is True
    assert sb["last_real_bet"].startswith("2026-09-01")
    html = c.get("/").get_data(as_text=True)
    for tab in ("proximos", "anteriores", "equipos", "graficas"):                     # v2.0
        assert f'data-tab="{tab}"' in html and f'id="tab-{tab}"' in html
    for el in ("uplist", "daybar", "pkpis", "pastbody", "teamlist", "cbank", "cacc", "cpicks", "cprops"):
        assert f'id="{el}"' in html


def test_without_saved_picks_the_page_explains_when_they_arrive(monkeypatch):
    import dashboard.app as dash
    monkeypatch.setattr(dash, "_q", lambda sql, params=None: pd.DataFrame())
    p = dash.app.test_client().get("/api/picks").get_json()
    assert p["ok"] is False and "después de que termina su partido" in p["msg"]


def test_daily_check_skips_the_picks_table_until_it_exists(monkeypatch):
    import src.utils.output_checks as oc
    now = pd.Timestamp.now(tz="UTC")

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = " ".join(str(sql).split())
        if "FROM program_picks" in s:
            raise RuntimeError('relation "program_picks" does not exist')
        if "AS bets" in s:
            return pd.DataFrame([{"bets": 0, "shadow": 0, "props": 0}])
        if "model_state" in s:
            return pd.DataFrame([{"key": k, "updated_at": now} for k in oc.WEEKLY_KEYS])
        if "team_xg_real" in s:
            return pd.DataFrame([{"m": 2.0}])
        return pd.DataFrame(columns=["id", "match", "match_date", "market", "result", "h", "a", "d"])
    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    assert oc.run_output_checks(engine=object(), normalize=lambda t: t) == []


# ─── picks reconstruidos (22-oct-26) ─────────────────────────────────────────

def test_past_picks_use_only_the_last_odds_before_kickoff():
    mvs = pd.DataFrame([{**_mvs(), "p_over25": None, "p_btts": None}])
    snap = {**UP, "match": "Atlas vs América", "kickoff": KICK, "pin_home_odds": 3.6, "pin_draw_odds": 3.9,
            "pin_away_odds": 2.0}
    snaps = pd.DataFrame([
        {**snap, "captured_at": KICK - pd.Timedelta(hours=5), "consensus_away_odds": 2.05},
        {**snap, "captured_at": KICK - pd.Timedelta(hours=1), "consensus_away_odds": 2.10},       # la última antes
        {**snap, "captured_at": KICK + pd.Timedelta(minutes=30), "consensus_away_odds": 9.0},     # en vivo: no
    ])
    (r,) = PP.build_past_picks(mvs, snaps, normalize=lambda t: {"américa": "club america"}.get(t.lower(), t.lower()))
    assert r["source"] == "reconstruido" and r["display"] == "Atlas vs América"
    assert r["market"] in ("home_win", "draw", "away_win")                  # sin goles guardados: solo 1X2
    if r["market"] == "away_win":
        assert r["playdoit_odds"] == pytest.approx(2.10)
    # sin foto de momios igual hay pick (contra el Pinnacle guardado), sin precio
    (r2,) = PP.build_past_picks(mvs, snaps.iloc[:0])
    assert r2["display"] is None and r2["playdoit_odds"] is None


def test_backfill_never_overwrites_a_saved_pick(monkeypatch):
    eng = FakeEngine()
    reads = {"model_vs_sharp": pd.DataFrame([{**_mvs(), "p_over25": None, "p_btts": None}]),
             "odds_history": pd.DataFrame(columns=["match", "kickoff", "captured_at"])}
    monkeypatch.setattr(PP.pd, "read_sql", lambda sql, con=None, params=None, **k: next(
        df for key, df in reads.items() if key in str(sql)))
    monkeypatch.setattr(PP, "resolve_picks", lambda engine=None: {})
    assert PP.backfill_picks(engine=eng) == 1
    (sql, params), = eng.statements("INSERT INTO program_picks")
    assert "ON CONFLICT (match, match_date) DO NOTHING" in sql and params["source0"] == "reconstruido"
