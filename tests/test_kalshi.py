"""
tests/test_kalshi.py
====================
Kalshi como segunda referencia sin margen (src/features/kalshi.py, 14-oct-26)
para los mercados que Pinnacle no publica en el feed (ambos anotan, primer
tiempo), medidos en el laboratorio contra su cierre. Solo mide.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.features import kalshi as K

END = "2026-10-17T17:00:00Z"        # Kalshi publica el FIN estimado


def _m(ticker, sub, bid, ask, strike=None, end=END):
    return {"ticker": ticker, "yes_sub_title": sub, "yes_bid_dollars": str(bid),
            "yes_ask_dollars": str(ask), "volume_fp": "1000", "occurrence_datetime": end,
            "floor_strike": strike}


def test_game_event_becomes_a_margin_free_1x2_with_the_real_kickoff():
    ev = {"title": "Everton vs Chelsea", "markets": [
        _m("X-EVE", "Everton", 0.30, 0.32), _m("X-CFC", "Chelsea", 0.44, 0.46),
        _m("X-TIE", "Tie", 0.25, 0.27)]}
    rows = {r["market"]: r for r in K.parse_event("GAME", "soccer_epl", ev)}
    assert set(rows) == {"home_win", "draw", "away_win"}
    assert abs(sum(r["prob"] for r in rows.values()) - 1) < 1e-9          # sin margen
    assert rows["home_win"]["home"] == "everton" and rows["home_win"]["away"] == "chelsea"
    assert rows["home_win"]["kickoff"].startswith("2026-10-17T14:00")      # fin − 3 h


def test_btts_total_and_first_half_map_to_system_markets():
    btts = K.parse_event("BTTS", "soccer_epl", {"title": "Everton vs Chelsea: BTTS", "markets": [
        _m("X-BTTS", "Both Teams To Score", 0.60, 0.62, end="2026-10-17T18:00:00Z")]})
    assert {r["market"]: round(r["prob"], 2) for r in btts} == {"btts": 0.61, "btts_no": 0.39}
    assert btts[0]["kickoff"].startswith("2026-10-17T14:00")               # fin − 4 h
    tot = K.parse_event("TOTAL", "soccer_epl", {"title": "Everton vs Chelsea: Total Goals", "markets": [
        _m("X-3", "Over 2.5", 0.56, 0.57, strike=2.5), _m("X-4", "Over 3.5", 0.34, 0.35, strike=3.5)]})
    assert {r["market"] for r in tot} == {"over25", "under25"}
    h1 = K.parse_event("1H", "soccer_uefa_champs_league", {"title": "Aston Villa vs Fenerbahce: First Half Winner",
        "markets": [_m("Y-AVL", "Aston Villa wins 1st Half", 0.43, 0.44),
                    _m("Y-FEN", "Fenerbahce wins 1st Half", 0.18, 0.19),
                    _m("Y-TIE", "Tie 1st Half", 0.37, 0.38)]})
    assert {r["market"] for r in h1} == {"h1_home", "h1_draw", "h1_away"}


def test_wide_or_missing_quotes_are_not_prices():
    ev = {"title": "A vs B", "markets": [_m("X-A", "A", 0.30, 0.45), _m("X-B", "B", 0.4, 0.42),
                                         _m("X-TIE", "Tie", 0.2, 0.22)]}
    assert K.parse_event("GAME", "soccer_epl", ev) == []                   # 15 c de diferencia
    assert K.parse_event("BTTS", "soccer_epl", {"title": "A vs B: BTTS",
                                                "markets": [_m("X", "BTTS", 0, 0.5)]}) == []


KO = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)


def test_kalshi_abbreviations_find_the_system_fixture():
    fixtures = [("soccer_spain_la_liga", "valencia", "ath bilbao", KO),
                ("soccer_spain_la_liga", "valencia", "ath bilbao", KO),        # ficha repetida
                ("soccer_spain_la_liga", "getafe", "vallecano", KO),
                ("soccer_germany_bundesliga", "gladbach", "hoffenheim", KO)]
    ev = {"league": "soccer_spain_la_liga", "home": "valencia", "away": "bilbao", "kick": KO + timedelta(minutes=40)}
    assert K.match_fixture(ev, fixtures) == ("valencia", "ath bilbao", KO)
    ev = {"league": "soccer_germany_bundesliga", "home": "m gladbach", "away": "hoffenheim", "kick": KO}
    assert K.match_fixture(ev, fixtures)[:2] == ("gladbach", "hoffenheim")


def test_no_guessing_between_two_similar_fixtures_or_far_kickoffs():
    fixtures = [("soccer_usa_mls", "sporting kc", "real salt lake", KO),
                ("soccer_usa_mls", "nashville", "sporting kc", KO)]
    ev = {"league": "soccer_usa_mls", "home": "kansas city", "away": "salt lake", "kick": KO}
    assert K.match_fixture(ev, fixtures) is None
    far = {"league": "soccer_usa_mls", "home": "nashville", "away": "sporting kc", "kick": KO + timedelta(hours=3)}
    assert K.match_fixture(far, fixtures) is None


def test_closing_capture_stores_every_price_and_the_rest_only_changes(monkeypatch):
    from tests.fake_db import FakeEngine, FakeResult
    import config.database as db
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[("soccer_epl", "everton", "chelsea",
                                                      datetime.now(timezone.utc) + timedelta(hours=1))])
                     if sql.startswith("SELECT DISTINCT sport_key") else FakeResult())
    monkeypatch.setattr(db, "engine", eng)
    end = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(K, "fetch_league", lambda lg: K.parse_event("GAME", lg, {
        "title": "Everton vs Chelsea", "markets": [_m("X-EVE", "Everton", 0.3, 0.32, end=end),
                                                   _m("X-CFC", "Chelsea", 0.44, 0.46, end=end),
                                                   _m("X-TIE", "Tie", 0.25, 0.27, end=end)]}))
    assert K.capture_kalshi(horizon_hours=3, every=True, verbose=False) == 1   # rowcount falso
    sql, params = eng.statements("INSERT INTO kalshi_history")[0]
    assert "WHERE :every OR prev.prob IS NULL" in sql and params["every"] is True
    assert params["h0"] == "everton" and params["m0"] in ("home_win", "draw", "away_win")


# ============================================================
# Laboratorio: mercados sin Pinnacle contra el cierre de Kalshi
# ============================================================

def _shadow(n):
    ko = pd.Timestamp("2026-10-17 14:00", tz="UTC")
    return pd.DataFrame([{"match": f"local {i} vs visita {i}", "match_date": ko + pd.Timedelta(hours=i),
                          "created_at": ko - pd.Timedelta(days=2), "market": "btts", "p_final": 0.60,
                          "odds": 1.80, "edge_market": 0.04, "pin_prob": None, "pin_close_prob": None,
                          "pin_close_at": None, "result": "win", "profit": 0.8, "odds_avg": None}
                         for i in range(n)])


def _kalshi(n, open_p, close_p):
    ko = pd.Timestamp("2026-10-17 14:00", tz="UTC")
    rows = []
    for i in range(n):
        k = ko + pd.Timedelta(hours=i)
        rows += [{"home": f"local {i}", "away": f"visita {i}", "kickoff": k, "market": "btts",
                  "prob": open_p, "captured_at": k - pd.Timedelta(days=3)},
                 {"home": f"local {i}", "away": f"visita {i}", "kickoff": k, "market": "btts",
                  "prob": close_p + (0.002 if i % 2 else -0.002), "captured_at": k - pd.Timedelta(minutes=20)}]
    return pd.DataFrame(rows)


def test_markets_without_pinnacle_are_judged_against_the_kalshi_close():
    from src.models.strategy_lab import build_lab, format_lines, with_kalshi
    s, kh = _shadow(150), _kalshi(150, open_p=0.55, close_p=0.60)
    w = with_kalshi(s, kh)
    assert len(w) == 150 and w["pin_prob"].iloc[0] == 0.55
    lab = build_lab(s, kh)
    v = lab["kalshi"]["k_precio"]["value"]                  # 0.55 − 1/1.80 < 0: no entra
    assert lab["kalshi"]["k_precio"]["candidates"] == 0
    v = lab["kalshi"]["k_modelo"]["value"]                  # 0.60 × 1.80 − 1 = +8%
    assert v["verdict"] == "a favor" and round(v["mean"], 2) == 0.08
    text = "\n".join(format_lines(lab))
    assert "Mercados sin Pinnacle, contra el cierre de Kalshi" in text
    assert "✅ le gana al mercado" in text.split("Mercados sin Pinnacle")[1]


def test_lab_says_it_is_collecting_before_kalshi_has_prices():
    from src.models.strategy_lab import build_lab, format_lines
    text = "\n".join(format_lines(build_lab(_shadow(5), pd.DataFrame())))
    assert "juntando precios de Kalshi" in text
