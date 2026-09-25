"""
Referencia Pinnacle (24-sep-26): sus precios (src/features/pinnacle.py), su
cierre, independiente del cierre del mercado (save_bets.closing_updates), y
el reporte semanal (src/models/sharp_reference.py). Solo mide: nada de esto
cambia las probabilidades ni los picks.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.features.pinnacle import (PIN_COLS, extract_pinnacle, pinnacle_prob, pinnacle_probs,
                                   pinnacle_quote_for)

KO = datetime(2030, 1, 5, 15, 0, tzinfo=timezone.utc)


def _t(minutes: int) -> datetime:
    return KO + timedelta(minutes=minutes)


def _book(key, home, draw, away, over, under, point=2.5):
    return {"key": key, "markets": [
        {"key": "h2h", "outcomes": [{"name": "Alpha", "price": home},
                                    {"name": "Beta", "price": away},
                                    {"name": "Draw", "price": draw}]},
        {"key": "totals", "outcomes": [{"name": "Over", "point": point, "price": over},
                                       {"name": "Under", "point": point, "price": under}]}]}


BOOKS = [_book("williamhill", 2.20, 3.50, 3.30, 2.00, 1.85),
         _book("pinnacle", 2.10, 3.40, 3.60, 1.95, 1.93)]
PIN = {"pin_home_odds": 2.10, "pin_draw_odds": 3.40, "pin_away_odds": 3.60,
       "pin_over25_odds": 1.95, "pin_under25_odds": 1.93,
       "pin_total_line": 2.5, "pin_total_over_odds": 1.95, "pin_total_under_odds": 1.93}


# ============================================================
# Precios de Pinnacle
# ============================================================

def test_only_pinnacle_prices_are_taken():
    assert extract_pinnacle(BOOKS, "Alpha", "Beta") == PIN


def test_without_pinnacle_or_at_another_total_line_there_is_no_price():
    assert extract_pinnacle(BOOKS[:1], "Alpha", "Beta") == dict.fromkeys(PIN_COLS)
    other_line = extract_pinnacle([_book("pinnacle", 2.1, 3.4, 3.6, 1.9, 1.9, point=2.75)],
                                  "Alpha", "Beta")
    assert other_line["pin_home_odds"] == 2.1 and other_line["pin_over25_odds"] is None
    assert (other_line["pin_total_line"], other_line["pin_total_over_odds"]) == (2.75, 1.9)


def test_parse_match_keeps_the_best_price_and_adds_pinnacle():
    from scripts.update_upcoming_matches import parse_match
    row = parse_match({"id": "e1", "home_team": "Alpha", "away_team": "Beta",
                       "commence_time": "2030-01-05T15:00:00Z", "bookmakers": BOOKS},
                      "soccer_epl")
    assert (row["home_odds"], row["away_odds"]) == (2.20, 3.60)     # mejor precio (sin cambios)
    assert {c: row[c] for c in PIN_COLS} == PIN


ROW = pd.Series({**PIN, "odds_fetched_at": _t(-30)})


def test_pinnacle_probabilities_have_no_margin():
    p = pinnacle_probs(ROW)
    assert p["home"] + p["draw"] + p["away"] == pytest.approx(1.0)
    io, iu = 1 / 1.95, 1 / 1.93
    assert p["over25"] == pytest.approx(io / (io + iu))
    assert pinnacle_prob("over25", ROW) + pinnacle_prob("under25", ROW) == pytest.approx(1.0)


def test_derived_markets_are_exact_equivalences():
    p = pinnacle_probs(ROW)
    ph, px, pa = p["home"], p["draw"], p["away"]
    assert pinnacle_prob("dnb_home", ROW) == pytest.approx(ph / (ph + pa))    # P(local | no empate)
    assert pinnacle_prob("dnb_away", ROW) == pytest.approx(pa / (ph + pa))
    # la línea embebida es la del local (ah_line)
    assert pinnacle_prob("ah_home_-0.50", ROW) == pytest.approx(ph)           # gana el local
    assert pinnacle_prob("ah_away_-0.50", ROW) == pytest.approx(px + pa)      # visita o empate
    assert pinnacle_prob("ah_home_+0.50", ROW) == pytest.approx(ph + px)      # local o empate
    assert pinnacle_prob("ah_away_+0.50", ROW) == pytest.approx(pa)
    assert pinnacle_prob("dc_1x", ROW) == pytest.approx(ph + px)
    assert pinnacle_prob("dc_x2", ROW) == pytest.approx(px + pa)
    assert pinnacle_prob("dc_12", ROW) == pytest.approx(ph + pa)


@pytest.mark.parametrize("market", ["ah_home_-0.75", "ah_away_-1.50", "ah_home_+0.00", "btts",
                                    "corners_over_9.5", "h1_home", "", None])
def test_markets_without_an_exact_reference(market):
    assert pinnacle_prob(market, ROW) is None


@pytest.mark.parametrize("row", [
    {}, {**PIN, "pin_home_odds": None}, {**PIN, "pin_home_odds": float("nan")},
    {**PIN, "pin_home_odds": 1.0}, {**PIN, "pin_home_odds": "x"},
])
def test_incomplete_prices_give_no_1x2_reference(row):
    assert pinnacle_prob("home_win", row) is None
    assert pinnacle_prob("dnb_home", row) is None


def test_quote_carries_the_fetch_time_and_needs_it():
    prob, at = pinnacle_quote_for("home_win", ROW)
    assert prob == pytest.approx(pinnacle_prob("home_win", ROW)) and at == _t(-30)
    assert pinnacle_quote_for("home_win", pd.Series({**PIN, "odds_fetched_at": pd.NaT})) == (None, None)
    assert pinnacle_quote_for("btts", ROW) == (None, None)


# ============================================================
# Cierre de Pinnacle, independiente del cierre del mercado
# ============================================================

def _odds_row(**kw):
    base = {"home_odds": 2.2, "draw_odds": 3.5, "away_odds": 3.6,
            "btts_yes_odds": 1.8, "btts_no_odds": 2.0,
            "odds_fetched_at": _t(-40), "specialty_fetched_at": _t(-300), **PIN}
    base.update(kw)
    return pd.Series(base)


def _current(**kw):
    base = {"closing_odds": np.nan, "closing_fetched_at": pd.NaT, "pin_close_at": pd.NaT}
    base.update(kw)
    return pd.Series(base)


def test_first_closing_writes_market_and_pinnacle():
    from src.models.save_bets import closing_updates
    sets = closing_updates(_current(), "home_win", _odds_row(), KO)
    assert sets["closing_odds"] == 2.2 and sets["pin_close_prob"] == pytest.approx(
        pinnacle_prob("home_win", ROW))
    assert sets["closing_fetched_at"] == sets["pin_close_at"] == _t(-40)


def test_pinnacle_closing_moves_even_if_the_market_closing_does_not():
    """BTTS llega por evento (hora vieja): su cierre no mejora, pero el de
    Pinnacle (derivado del 1X2, fetch reciente) sí se escribe."""
    from src.models.save_bets import closing_updates
    sets = closing_updates(_current(closing_odds=1.8, closing_fetched_at=_t(-300)),
                           "dnb_home", _odds_row(dnb_home_odds=1.5, dnb_away_odds=2.6), KO)
    assert set(sets) == {"pin_close_prob", "pin_close_at"}


def test_older_or_live_pinnacle_quotes_do_not_replace_a_closer_one():
    from src.models.save_bets import closing_updates
    current = _current(closing_odds=2.2, closing_fetched_at=_t(-10), pin_close_at=_t(-10))
    assert closing_updates(current, "home_win", _odds_row(), KO) == {}             # -40 es más viejo
    assert closing_updates(_current(), "home_win", _odds_row(odds_fetched_at=_t(15)), KO) == {}


def test_markets_without_pinnacle_reference_only_close_the_market():
    from src.models.save_bets import closing_updates
    sets = closing_updates(_current(), "btts", _odds_row(), KO)
    assert set(sets) == {"closing_odds", "closing_fetched_at"}


def test_update_writes_only_the_given_columns():
    from src.models.save_bets import apply_closing_updates
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    with eng.begin() as conn:
        apply_closing_updates(conn, "shadow_bets", 7, {"pin_close_prob": 0.5, "pin_close_at": _t(-5)})
        with pytest.raises(AssertionError):
            apply_closing_updates(conn, "shadow_bets", 7, {"result": "win"})
    (sql, params), = eng.statements("UPDATE shadow_bets")
    assert sql == "UPDATE shadow_bets SET pin_close_prob = :pin_close_prob, pin_close_at = :pin_close_at WHERE id = :id"
    assert params["id"] == 7


def _fake_closing_db(monkeypatch, module, table_rows: pd.DataFrame):
    import src.models.save_bets as sb
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    monkeypatch.setattr(module, "engine", eng)
    monkeypatch.setattr(pd, "read_sql", lambda *a, **k: table_rows.copy())
    monkeypatch.setattr(sb, "_nearest_market_row", lambda h, a, d: pd.DataFrame([_odds_row()]))
    return eng


def test_shadow_closing_stores_pinnacle(monkeypatch):
    import src.models.save_bets as sb
    rows = pd.DataFrame([{"id": 3, "match": "alpha vs beta", "market": "away_win",
                          "match_date": KO, "closing_odds": np.nan,
                          "closing_fetched_at": pd.NaT, "pin_close_at": pd.NaT}])
    eng = _fake_closing_db(monkeypatch, sb, rows)
    sb._update_shadow_closing()
    (_, params), = eng.statements("UPDATE shadow_bets")
    assert params["closing_odds"] == 3.6
    assert params["pin_close_prob"] == pytest.approx(pinnacle_prob("away_win", ROW))


def test_bets_closing_stores_pinnacle(monkeypatch):
    import scripts.update_closing_odds as uco
    rows = pd.DataFrame([{"id": 9, "match": "alpha vs beta", "market": "home_win", "odds": 2.3,
                          "match_date": KO.replace(tzinfo=None), "closing_odds": np.nan,
                          "closing_fetched_at": pd.NaT, "pin_close_at": pd.NaT}])
    eng = _fake_closing_db(monkeypatch, uco, rows)
    monkeypatch.setattr(uco, "_close_shadow", lambda: None)
    uco.update_closing_odds()
    (_, params), = eng.statements("UPDATE bets_history")
    assert params["id"] == 9 and params["closing_odds"] == 2.2
    assert params["pin_close_prob"] == pytest.approx(pinnacle_prob("home_win", ROW))


def test_shadow_insert_carries_pinnacle_probability(monkeypatch):
    import src.models.save_bets as sb
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    monkeypatch.setattr(sb, "engine", eng)
    base = {"match": "a vs b", "match_date": KO, "league": "soccer_epl", "p_final": 0.5,
            "p_ref": 0.48, "deviation": 0.02, "odds": 2.1, "edge_market": 0.02, "reason": "sweep"}
    sb.persist_shadow_bets([{**base, "market": "home_win", "pin_prob": 0.47},
                            {**base, "market": "btts"}])
    inserts = eng.statements("INSERT INTO shadow_bets")
    assert [p["pin_prob"] for _, p in inserts] == [0.47, None]


def test_upsert_writes_pinnacle_only_with_its_fetch_time(monkeypatch):
    """Cada fetch con hora escribe el precio de Pinnacle tal cual (NULL si ya
    no cotiza); una fila sin hora no lo toca."""
    import scripts.update_upcoming_matches as uum
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    monkeypatch.setattr(uum, "engine", eng)
    uum.upsert_matches([{"match_key": "k", "home_odds": 2.0}])
    (sql, params), = eng.statements("INSERT INTO upcoming_matches")
    assert all(params[c] is None for c in PIN_COLS)
    assert ("pin_home_odds = CASE WHEN EXCLUDED.odds_fetched_at IS NOT NULL "
            "THEN EXCLUDED.pin_home_odds ELSE upcoming_matches.pin_home_odds END") in sql


# ============================================================
# Reporte semanal
# ============================================================

def _rows(n_matches=40, per_match=3, p_close=0.55, odds=2.0, close_at=-30, p_final=0.60,
          p_open=0.50, result="win", edge=0.08):
    rows = []
    for i in range(n_matches):
        for j in range(per_match):
            rows.append({"match": f"m{i} vs x{i}", "match_date": KO, "created_at": _t(-600),
                         "market": ["home_win", "over25", "dnb_home"][j % 3],
                         "p_final": p_final, "odds": odds, "edge_market": edge,
                         "pin_prob": p_open, "pin_close_prob": p_close,
                         "pin_close_at": _t(close_at) if close_at is not None else pd.NaT,
                         "result": result})
    return pd.DataFrame(rows)


def test_value_against_pinnacle_close():
    from src.models.sharp_reference import build_sharp_report
    rep = build_sharp_report(_rows(), _rows(), min_edge=0.05)
    bv = rep["bets_value"]
    assert bv["mean"] == pytest.approx(0.55 * 2.0 - 1)        # +10% por unidad
    assert (bv["n"], bv["clusters"], bv["verdict"]) == (120, 40, "a favor")


def test_invalid_closes_do_not_count():
    """Un 'cierre' de 5 h antes del kickoff es casi la apertura, y uno previo
    a abrir la fila no mide nada: fuera, como en closing_quality."""
    from src.models.sharp_reference import build_sharp_report
    early = _rows(close_at=-300)
    before_open = _rows(close_at=-30).assign(created_at=_t(-10))
    rep = build_sharp_report(pd.concat([early, before_open]), _rows(close_at=None), min_edge=0.05)
    assert rep["shadow_value"]["n"] == 0 and rep["bets_value"]["n"] == 0
    assert rep["coverage"]["shadow_valid_close"] == 0


def test_pinnacle_moving_toward_the_model():
    """El modelo dice 60% con Pinnacle en 50%; Pinnacle cierra en 53%: se
    movió 3pt hacia el modelo. Con el modelo por debajo, el signo se alinea."""
    from src.models.sharp_reference import build_sharp_report
    up = _rows(p_final=0.60, p_open=0.50, p_close=0.53)
    down = _rows(p_final=0.40, p_open=0.50, p_close=0.47).assign(
        match=lambda d: "z" + d["match"])
    mv = build_sharp_report(pd.concat([up, down]), _rows(), min_edge=0.05)["move_toward_model"]
    assert mv["mean"] == pytest.approx(0.03) and mv["verdict"] == "a favor"


def test_model_vs_pinnacle_only_on_decided_results():
    from src.models.sharp_reference import build_sharp_report
    shadow = pd.concat([_rows(result="win"), _rows(result="push").assign(match="p")])
    st = build_sharp_report(shadow, _rows(), min_edge=0.05)["model_vs_pinnacle"]
    assert st["n"] == 120                          # los push no cuentan
    # todas ganadas (y=1): el modelo (0.60) acierta más que Pinnacle (0.55) → negativo
    assert st["mean"] == pytest.approx((0.60 - 1) ** 2 - (0.55 - 1) ** 2) and st["mean"] < 0


def test_bettable_uses_the_edge_threshold():
    from src.models.sharp_reference import build_sharp_report
    shadow = pd.concat([_rows(edge=0.08), _rows(edge=0.01).assign(match="low")])
    rep = build_sharp_report(shadow, _rows(), min_edge=0.05)
    assert rep["bettable_value"]["n"] == 120 and rep["shadow_value"]["n"] == 240


def test_report_without_data_says_so():
    from src.models.sharp_reference import build_sharp_report, format_report, has_data
    rep = build_sharp_report(pd.DataFrame(), pd.DataFrame(), min_edge=0.05)
    assert not has_data(rep) and rep["coverage"]["shadow"] == 0
    assert "Aún no hay cierres de Pinnacle válidos" in format_report(rep)


def test_report_reads_plainly():
    from src.models.sharp_reference import build_sharp_report, format_report
    text = format_report(build_sharp_report(_rows(), _rows(), min_edge=0.05), html=True)
    assert "Tus apuestas vs su cierre: +10.0%" in text and "a favor" in text
    assert "no cambia tus picks" in text and "<b>" in text


def test_weekly_step_sends_only_with_data(monkeypatch):
    import scripts.orchestrator as orch
    import scripts.notify_telegram as nt
    import src.models.sharp_reference as sr
    sent = []
    monkeypatch.setattr(nt, "send_message", lambda m: sent.append(m) or True)
    monkeypatch.setattr(sr, "run_sharp_reference",
                        lambda verbose=True: sr.build_sharp_report(pd.DataFrame(), pd.DataFrame(), 0.05))
    orch.step_sharp_reference()
    assert sent == []
    monkeypatch.setattr(sr, "run_sharp_reference",
                        lambda verbose=True: sr.build_sharp_report(_rows(), _rows(), 0.05))
    orch.step_sharp_reference()
    assert len(sent) == 1 and "REFERENCIA PINNACLE" in sent[0]


# ============================================================
# El pipeline pasa el precio de Pinnacle a candidatas y apuestas
# ============================================================

def test_pipeline_records_pinnacle_at_open(monkeypatch):
    from tests.pipeline_harness import default_matches, run_pipeline
    matches = default_matches()
    first = matches.index[0]                        # alpha vs beta (M1)
    for col, v in {"pin_home_odds": 1.85, "pin_draw_odds": 3.60, "pin_away_odds": 4.50,
                   "pin_over25_odds": 1.97, "pin_under25_odds": 1.90}.items():
        matches[col] = None
        matches.at[first, col] = v
    captured = run_pipeline(monkeypatch, matches)
    alpha = {s["market"]: s for s in captured["shadow"] if s["match"] == "alpha vs beta"}
    covered = [m for m in alpha if m in ("home_win", "draw", "away_win", "over25", "under25")]
    assert covered, "el barrido no produjo candidatas 1X2/goles para alpha vs beta"
    row = matches.loc[first]
    for m in covered:
        assert alpha[m]["pin_prob"] == pytest.approx(pinnacle_prob(m, row))
    others = [s for s in captured["shadow"] if s["match"] != "alpha vs beta"]
    assert others and all(s["pin_prob"] is None for s in others)
