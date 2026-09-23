"""
Tests de resolución de mercados (liquidación de bets).

Hasta el 22-sep-26 este archivo REIMPLEMENTABA la lógica dentro de cada
test ("testeo indirectamente ... ejecutando la lógica inline"): probaba
una copia, no el código que liquida el dinero, e incluía tautologías como
`assert hy is None`. Ahora que la liquidación es una función pura
(save_bets.resolve_market) y el buscador de partidos también
(save_bets.make_match_lookup), se ejecuta el código de producción con las
mismas tablas de casos. Sin DB real — datos 100% sintéticos.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pandas as pd
import pytest

from src.models.save_bets import resolve_market, make_match_lookup, plan_bet_settlements


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _match_row(**kwargs) -> pd.Series:
    """Fila sintética de matches (como la precarga de update_bet_results)."""
    defaults = {
        "home_team_l": "brazil",
        "away_team_l": "argentina",
        "date": pd.Timestamp("2026-06-15", tz="UTC"),
        "home_goals": None, "away_goals": None,
        "home_corners": None, "away_corners": None,
        "home_yellow": None, "away_yellow": None,
        "home_goals_ht": None, "away_goals_ht": None,
        "home_goals_h2": None, "away_goals_h2": None,
        "home_shots_target": None, "away_shots_target": None,
    }
    defaults.update(kwargs)
    return pd.Series(defaults)


def _resolve(market, hg, ag, stake=1.0, odds=1.90, **stats):
    return resolve_market(market, _match_row(home_goals=hg, away_goals=ag, **stats), odds, stake)


# ─────────────────────────────────────────────────────────────────────────────
# Buscador de partidos (make_match_lookup)
# ─────────────────────────────────────────────────────────────────────────────

class TestLookupMatch:

    def test_finds_exact_date_match(self):
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(home_goals=2, away_goals=1)]))
        row = lookup("brazil", "argentina", pd.Timestamp("2026-06-15 20:00", tz="UTC"))
        assert row is not None and row["home_goals"] == 2

    def test_returns_none_outside_two_day_window(self):
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(date=pd.Timestamp("2026-06-10", tz="UTC"), home_goals=2, away_goals=1)]))
        assert lookup("brazil", "argentina", pd.Timestamp("2026-06-15 20:00", tz="UTC")) is None

    def test_picks_closest_when_two_matches(self):
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(home_team_l="a", away_team_l="b",
                       date=pd.Timestamp("2026-06-15 18:00", tz="UTC"), home_goals=1, away_goals=0),
            _match_row(home_team_l="a", away_team_l="b",
                       date=pd.Timestamp("2026-06-14 20:00", tz="UTC"), home_goals=3, away_goals=3)]))
        row = lookup("a", "b", pd.Timestamp("2026-06-15 19:00", tz="UTC"))
        assert row["home_goals"] == 1

    def test_unknown_teams_return_none(self):
        lookup = make_match_lookup(pd.DataFrame([_match_row(home_goals=1, away_goals=0)]))
        assert lookup("x", "y", pd.Timestamp("2026-06-15", tz="UTC")) is None

    def test_falls_back_to_normalized_names(self):
        """matches con el nombre crudo del CSV ("nott'm forest") y búsqueda
        con el nombre normalizado de la API: el fallback los empareja."""
        from src.utils.team_normalizer import normalize_team
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(home_team_l="nott'm forest", away_team_l="man united",
                       home_goals=2, away_goals=2)]))
        home, away = normalize_team("Nott'm Forest"), normalize_team("Man United")
        assert home != "nott'm forest"          # de verdad ejercita el fallback
        row = lookup(home, away, pd.Timestamp("2026-06-15 16:00", tz="UTC"))
        assert row is not None and row["home_goals"] == 2

    def test_naive_dates_in_matches_against_aware_kickoff(self):
        """matches.date es DATE (sin zona) y shadow_bets.match_date TIMESTAMPTZ."""
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(date="2026-06-15", home_goals=1, away_goals=0)]))
        assert lookup("brazil", "argentina", pd.Timestamp("2026-06-15 20:00", tz="UTC")) is not None
        assert lookup("brazil", "argentina", pd.Timestamp("2026-06-15 20:00")) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Mercados de goles (1X2, O/U, BTTS, DC)
# ─────────────────────────────────────────────────────────────────────────────

class TestGoalMarkets:

    MARKET_CASES = [
        # (market, hg, ag, expected_outcome)
        ("home_win",  2, 1, "win"),
        ("home_win",  1, 1, "loss"),
        ("home_win",  0, 1, "loss"),
        ("draw",      1, 1, "win"),
        ("draw",      2, 1, "loss"),
        ("away_win",  0, 1, "win"),
        ("away_win",  1, 1, "loss"),
        ("over25",    3, 0, "win"),
        ("over25",    1, 1, "loss"),   # 2 goles = no over25
        ("under25",   1, 1, "win"),
        ("under25",   2, 1, "loss"),
        ("btts",      1, 1, "win"),
        ("btts",      2, 0, "loss"),
        ("btts_no",   2, 0, "win"),
        ("btts_no",   1, 1, "loss"),
        ("over_1.5",  2, 0, "win"),
        ("over_1.5",  1, 0, "loss"),
        ("under_1.5", 1, 0, "win"),
        ("under_1.5", 2, 0, "loss"),
        ("over_3.5",  4, 0, "win"),
        ("over_3.5",  2, 1, "loss"),
        ("under_3.5", 2, 1, "win"),
        ("under_3.5", 4, 0, "loss"),
        ("dc_1x",     2, 0, "win"),
        ("dc_1x",     1, 1, "win"),
        ("dc_1x",     0, 1, "loss"),
        ("dc_x2",     0, 1, "win"),
        ("dc_x2",     1, 1, "win"),
        ("dc_x2",     2, 0, "loss"),
        ("dc_12",     2, 0, "win"),
        ("dc_12",     0, 2, "win"),
        ("dc_12",     1, 1, "loss"),
    ]

    @pytest.mark.parametrize("market,hg,ag,expected", MARKET_CASES)
    def test_goal_market_outcome(self, market, hg, ag, expected):
        outcome, profit = _resolve(market, hg, ag, stake=2.0, odds=1.80)
        assert outcome == expected
        assert profit == pytest.approx(2.0 * 0.80 if expected == "win" else -2.0)

    @pytest.mark.parametrize("market,hg,ag,expected,profit", [
        ("dnb_home", 2, 1, "win", 0.9),
        ("dnb_home", 1, 1, "push", 0.0),
        ("dnb_home", 0, 1, "loss", -1.0),
        ("dnb_away", 1, 1, "push", 0.0),
        ("dnb_away", 0, 2, "win", 0.9),
    ])
    def test_draw_no_bet(self, market, hg, ag, expected, profit):
        assert _resolve(market, hg, ag) == (expected, pytest.approx(profit))

    def test_market_without_resolver_branch_is_unresolved_not_a_loss(self):
        """Un nombre sin rama no se liquida como pérdida inventada (D10)."""
        assert _resolve("mercado_inventado", 1, 0) == ("unresolved", 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Asian Handicap — la línea embebida es SIEMPRE la del local
# ─────────────────────────────────────────────────────────────────────────────

class TestAsianHandicap:

    @pytest.mark.parametrize("market,hg,ag,expected", [
        ("ah_home_-0.5", 1, 0, "win"),
        ("ah_home_-0.5", 1, 1, "loss"),
        ("ah_home_+0.5", 1, 1, "win"),
        ("ah_away_-0.5", 0, 1, "win"),
        ("ah_away_+0.5", 1, 1, "loss"),   # el local recibe +0.5: en empate cubre él
        ("ah_away_-0.5", 1, 1, "win"),    # el local da 0.5: en empate cubre el visitante
        ("ah_home_-0.75", 2, 0, "win"),
    ])
    def test_half_and_whole_lines(self, market, hg, ag, expected):
        assert _resolve(market, hg, ag)[0] == expected

    def test_whole_line_exact_is_push(self):
        assert _resolve("ah_home_-1.0", 2, 1, stake=1.0) == ("push", 0.0)

    def test_quarter_line_win(self):
        outcome, profit = _resolve("ah_home_-0.25", 1, 0, stake=2.0, odds=1.90)
        assert outcome == "win" and profit == pytest.approx(2.0 * 0.90, abs=0.01)

    def test_quarter_line_draw_is_half_loss(self):
        outcome, profit = _resolve("ah_home_-0.25", 1, 1, stake=2.0, odds=1.90)
        assert outcome == "half_loss" and profit == pytest.approx(-1.0, abs=0.01)

    def test_quarter_line_draw_is_half_win(self):
        outcome, profit = _resolve("ah_home_+0.25", 1, 1, stake=2.0, odds=1.90)
        assert outcome == "half_win" and profit == pytest.approx(0.90, abs=0.01)

    def test_three_quarter_line_win_by_one_is_half_win(self):
        assert _resolve("ah_home_-0.75", 1, 0, stake=2.0, odds=1.90)[0] == "half_win"

    def test_profit_win_and_loss(self):
        assert _resolve("ah_home_+0.5", 0, 0, stake=3.0, odds=1.85)[1] == pytest.approx(2.55)
        assert _resolve("ah_home_-1.5", 1, 0, stake=3.0, odds=1.85)[1] == pytest.approx(-3.0)


# ─────────────────────────────────────────────────────────────────────────────
# Mercados de estadísticas (córners, tarjetas, tiros, 1T/2T)
# ─────────────────────────────────────────────────────────────────────────────

class TestStatsMarkets:

    @pytest.mark.parametrize("market,h,a,expected", [
        ("corners_over_9.5", 6, 5, "win"),
        ("corners_over_9.5", 4, 4, "loss"),
        ("corners_under_9.5", 4, 4, "win"),
    ])
    def test_corners(self, market, h, a, expected):
        assert _resolve(market, 1, 0, home_corners=h, away_corners=a)[0] == expected

    def test_cards(self):
        assert _resolve("cards_over_4.5", 1, 0, home_yellow=3, away_yellow=2)[0] == "win"
        assert _resolve("cards_under_4.5", 1, 0, home_yellow=3, away_yellow=2)[0] == "loss"

    @pytest.mark.parametrize("market,stats,expected", [
        ("cards_under_4.0", dict(home_yellow=2, away_yellow=2), "push"),
        ("cards_over_4.0", dict(home_yellow=2, away_yellow=2), "push"),
        ("cards_under_4.0", dict(home_yellow=1, away_yellow=2), "win"),
        ("corners_over_10.0", dict(home_corners=6, away_corners=4), "push"),
        ("corners_under_10.0", dict(home_corners=7, away_corners=4), "loss"),
        ("shots_over_7.0", dict(home_shots_target=3, away_shots_target=4), "push"),
    ])
    def test_whole_line_exactly_on_the_line_is_a_push(self, market, stats, expected):
        """Línea entera y total justo en la línea: la casa devuelve el stake.
        Hasta el 22-sep-26 el under se daba ganado y el over perdido."""
        outcome, profit = _resolve(market, 1, 0, stake=2.0, odds=1.9, **stats)
        assert outcome == expected
        assert profit == pytest.approx({"push": 0.0, "win": 1.8, "loss": -2.0}[expected])

    def test_missing_stats_are_unresolved_without_profit(self):
        assert _resolve("cards_over_4.5", 1, 0) == ("unresolved", 0.0)
        assert _resolve("corners_over_9.5", 1, 0) == ("unresolved", 0.0)

    def test_shots(self):
        assert _resolve("shots_over_5.5", 1, 0, home_shots_target=4, away_shots_target=3)[0] == "win"

    @pytest.mark.parametrize("market,gh,ga,expected", [
        ("h1_home", 1, 0, "win"),
        ("h1_home", 0, 0, "loss"),
        ("h1_draw", 0, 0, "win"),
        ("h1_draw", 1, 0, "loss"),
        ("h1_away", 0, 1, "win"),
        ("h1_away", 1, 0, "loss"),
    ])
    def test_first_half(self, market, gh, ga, expected):
        assert _resolve(market, 2, 2, home_goals_ht=gh, away_goals_ht=ga)[0] == expected

    @pytest.mark.parametrize("market,gh,ga,expected", [
        ("h2_home", 2, 0, "win"),
        ("h2_draw", 1, 1, "win"),
        ("h2_away", 0, 1, "win"),
    ])
    def test_second_half(self, market, gh, ga, expected):
        assert _resolve(market, 3, 3, home_goals_h2=gh, away_goals_h2=ga)[0] == expected

    def test_half_time_goals_missing_is_unresolved(self):
        assert _resolve("h1_home", 1, 0, home_goals_ht=None, away_goals_ht=1) == ("unresolved", 0.0)

    @pytest.mark.parametrize("market", ["corners_over_9.5", "corners_under_9.5",
                                        "cards_over_4.5", "cards_under_4.5",
                                        "shots_over_5.5", "shots_under_5.5"])
    def test_missing_stats_as_nan_are_unresolved_not_a_loss(self, market):
        """
        Como llega de pd.read_sql: si otra fila de la precarga tiene córners,
        la columna es float64 y el NULL es NaN, no None. Hasta el 22-sep-26
        el resolver solo miraba `is not None`: con NaN toda comparación es
        False y over Y under se liquidaban "loss" (−stake al bankroll).
        """
        stats = ["home_corners", "away_corners", "home_yellow", "away_yellow",
                 "home_shots_target", "away_shots_target"]
        m = pd.DataFrame([_match_row(home_goals=2, away_goals=1, **{c: 5 for c in stats}),
                          _match_row(home_goals=1, away_goals=0)])
        for c in stats:
            m[c] = pd.to_numeric(m[c])          # float64 con NaN, como en producción
        row = m.iloc[1]
        assert pd.isna(row["home_corners"])
        assert resolve_market(market, row, 1.9, 1.0) == ("unresolved", 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Liquidación completa: plan puro + update_bet_results con base falsa
# ─────────────────────────────────────────────────────────────────────────────

STAT_COLS = ["home_corners", "away_corners", "home_yellow", "away_yellow",
             "home_goals_ht", "away_goals_ht", "home_goals_h2", "away_goals_h2",
             "home_shots_target", "away_shots_target"]


def _matches_like_production(rows):
    """DataFrame como el de la precarga: NULL numéricos → NaN (float64)."""
    df = pd.DataFrame([_match_row(**r) for r in rows])
    for c in ["home_goals", "away_goals"] + STAT_COLS:
        df[c] = pd.to_numeric(df[c])
    return df


MATCHES = [
    dict(home_team_l="alpha", away_team_l="beta", date="2026-09-20",
         home_goals=2, away_goals=1, home_corners=6, away_corners=5),       # 11 córners
    dict(home_team_l="gamma", away_team_l="delta", date="2026-09-14",
         home_goals=0, away_goals=0, home_corners=3, away_corners=4),       # llegó tarde
    dict(home_team_l="eps", away_team_l="zeta", date="2026-09-20",
         home_goals=1, away_goals=1),                                       # sin tarjetas
    dict(home_team_l="eta", away_team_l="theta", date="2026-09-20"),        # sin goles aún
]


def _bet(id_, match, market, result, day, odds=1.8, stake=1.0):
    return {"id": id_, "match": match, "market": market, "result": result,
            "odds": odds, "stake": stake,
            "match_date": pd.Timestamp(f"2026-09-{day} 15:00")}   # TIMESTAMP sin zona


BETS = pd.DataFrame([
    _bet(1, "alpha vs beta", "home_win", "pending", 20),
    _bet(2, "gamma vs delta", "corners_under_9.5", "unresolved", 14),   # 7 córners ≤ 9.5
    _bet(3, "eps vs zeta", "cards_under_4.5", "pending", 20),
    _bet(4, "eps vs zeta", "cards_over_4.5", "unresolved", 20),
    _bet(5, "eta vs theta", "home_win", "pending", 20),
    _bet(6, "iota vs kappa", "home_win", "pending", 20),                 # sin partido
])


class TestPlanBetSettlements:

    def test_plan(self):
        plan = {p["id"]: p for p in plan_bet_settlements(BETS, _matches_like_production(MATCHES))}
        assert plan[1] == {"id": 1, "match": "alpha vs beta", "market": "home_win",
                           "was": "pending", "result": "win", "profit": pytest.approx(0.8)}
        # la unresolved con datos que llegaron tarde se liquida YA, no vuelve a pending
        assert plan[2]["was"] == "unresolved" and plan[2]["result"] == "win"
        # tarjetas NULL: espera sus datos — nunca "loss"
        assert plan[3]["result"] == "unresolved" and plan[3]["profit"] == 0.0
        # ya unresolved y sin datos, sin partido o sin goles: nada que escribir
        assert set(plan) == {1, 2, 3}


def test_update_bet_results_writes_plan_with_bankroll_guard(monkeypatch):
    import src.models.save_bets as sb

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = " ".join(str(sql).split())
        if "FROM bets_history" in s:
            assert "result = 'unresolved'" in s          # también las que esperan datos
            return BETS.copy()
        if "FROM matches" in s:
            assert params["d_from"] <= "2026-09-12"      # ventana cubre la unresolved vieja
            return _matches_like_production(MATCHES)
        raise AssertionError(f"SQL inesperado: {s[:100]}")

    executed = []

    class FakeConn:
        def execute(self, stmt, params=None):
            executed.append((" ".join(str(stmt).split()), params))
            # la bet 2 ya la liquidó otra corrida entre lectura y escritura
            return SimpleNamespace(rowcount=0 if (params or {}).get("id") == 2 else 1)

        @contextmanager
        def begin_nested(self):
            yield

    class FakeEngine:
        @contextmanager
        def begin(self):
            yield FakeConn()

    bank = []
    monkeypatch.setattr(sb.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(sb, "engine", FakeEngine())
    monkeypatch.setattr(sb, "ensure_bankroll_schema", lambda: None)
    monkeypatch.setattr(sb, "update_bankroll",
                        lambda profit, notes, conn: bank.append((round(profit, 6), notes)))

    sb.update_bet_results()

    updates = {p["id"]: p for s, p in executed if s.startswith("UPDATE bets_history SET result = :result")}
    assert set(updates) == {1, 2, 3}
    assert updates[1]["was"] == "pending" and updates[3]["result"] == "unresolved"
    # bankroll: solo resultados finales que SÍ se escribieron (la 2 perdió la carrera)
    assert bank == [(0.8, "alpha vs beta | home_win | win")]
    assert any("SET result = 'stale'" in s for s, _ in executed)
