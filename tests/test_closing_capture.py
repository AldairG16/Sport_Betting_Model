"""
tests/test_closing_capture.py
=============================
Cierres reales (22-sep-26): el CLV y el aprendizaje del peso del modelo
dependen de que la cuota de "cierre" se haya descargado poco antes del
kickoff. Antes, sin recarga cerca del kickoff, el cierre era la misma
cuota de apertura (movimiento 0 falso).

Se prueba sin DB: reglas de validez, captura, selección de partidos a
recargar y la recarga dirigida (con sus topes de créditos).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.closing_quality import is_valid_closing, should_update_closing, valid_closing_sql

KICKOFF = datetime(2030, 1, 5, 15, 0, tzinfo=timezone.utc)


def _t(minutes_from_kickoff: int) -> datetime:
    return KICKOFF + timedelta(minutes=minutes_from_kickoff)


# ============================================================
# Qué es un cierre válido
# ============================================================

@pytest.mark.parametrize("fetched,opened,valid", [
    (_t(-30), _t(-180), True),      # 30 min antes del kickoff, tras la apertura
    (_t(-149), _t(-600), True),
    (_t(-200), _t(-600), False),    # demasiado temprano: no es cierre
    (_t(5), _t(-180), False),       # después del kickoff: cuota en vivo
    (_t(-30), _t(-20), False),      # anterior a la apertura: no mide movimiento
    (None, _t(-180), False),        # frescura desconocida
])
def test_valid_closing_window(fetched, opened, valid):
    assert is_valid_closing(fetched, KICKOFF, opened) is valid


def test_closing_is_replaced_only_by_a_later_pre_kickoff_quote():
    assert should_update_closing(None, _t(-70), KICKOFF)            # primer cierre
    assert should_update_closing(_t(-70), _t(-20), KICKOFF)         # más cercano
    assert not should_update_closing(_t(-20), _t(-70), KICKOFF)     # más viejo
    assert not should_update_closing(_t(-20), _t(10), KICKOFF)      # en vivo
    assert not should_update_closing(_t(-20), None, KICKOFF)


def test_sql_rule_matches_python_rule():
    sql = valid_closing_sql("b")
    assert "b.closing_fetched_at IS NOT NULL" in sql
    assert "150 minutes" in sql and "2 minutes" in sql


# ============================================================
# Cuota de cierre + hora de descarga
# ============================================================

def _row(**kw):
    base = {"home_odds": 2.0, "draw_odds": 3.4, "away_odds": 3.8,
            "over25_odds": 1.9, "under25_odds": 1.95,
            "btts_yes_odds": 1.8, "btts_no_odds": 2.0,
            "ah_line": -0.5, "ah_home_odds": 2.0, "ah_away_odds": 1.85,
            "dnb_home_odds": np.nan, "dnb_away_odds": np.nan,
            "corners_line": 9.5, "corners_over_odds": 1.9, "corners_under_odds": 1.9,
            "odds_fetched_at": _t(-40), "specialty_fetched_at": _t(-300)}
    base.update(kw)
    return pd.Series(base)


def test_featured_market_carries_league_fetch_time():
    from src.models.save_bets import closing_quote_for
    assert closing_quote_for("home_win", _row()) == (2.0, _t(-40))
    assert closing_quote_for("ah_home_-0.50", _row()) == (2.0, _t(-40))


def test_per_event_market_carries_its_own_fetch_time():
    """BTTS llega por evento: su cierre no es fresco porque la liga lo sea."""
    from src.models.save_bets import closing_quote_for
    assert closing_quote_for("btts", _row()) == (1.8, _t(-300))
    assert closing_quote_for("corners_over_9.5", _row()) == (1.9, _t(-300))


def test_dnb_fetch_time_follows_its_source():
    from src.models.save_bets import closing_quote_for
    derived_odds, derived_at = closing_quote_for("dnb_home", _row())
    assert derived_at == _t(-40)                    # derivado del 1X2
    api_odds, api_at = closing_quote_for("dnb_home", _row(dnb_home_odds=1.45, dnb_away_odds=2.6))
    assert (api_odds, api_at) == (1.45, _t(-300))   # cuota por evento


def test_unknown_fetch_time_is_none_not_nat():
    from src.models.save_bets import closing_quote_for
    odds, at = closing_quote_for("home_win", _row(odds_fetched_at=pd.NaT))
    assert odds == 2.0 and at is None
    assert closing_quote_for("ah_home_-0.50", _row(ah_line=-0.75)) == (None, None)


# ============================================================
# Qué partidos recargar
# ============================================================

def _norm(name):
    from src.utils.team_normalizer import normalize_team
    return normalize_team(name).lower().strip()


def test_targets_are_matches_with_bets_or_shadow_and_flag_per_event_markets():
    from scripts.update_closing_odds import plan_closing_targets
    upcoming = pd.DataFrame([
        {"sport_key": "soccer_epl", "home_team_norm": _norm("arsenal"),
         "away_team_norm": _norm("chelsea"), "match_date": KICKOFF},
        {"sport_key": "soccer_epl", "home_team_norm": _norm("everton"),
         "away_team_norm": _norm("fulham"), "match_date": KICKOFF},
        {"sport_key": "soccer_spain_la_liga", "home_team_norm": _norm("getafe"),
         "away_team_norm": _norm("osasuna"), "match_date": KICKOFF},
    ])
    bets = pd.DataFrame([{"match": f"{_norm('arsenal')} vs {_norm('chelsea')}", "market": "btts"}])
    shadow = pd.DataFrame([{"match": f"{_norm('everton')} vs {_norm('fulham')}"}])
    targets = {(t["home_norm"], t["sport_key"]): t for t in plan_closing_targets(upcoming, bets, shadow)}
    assert set(targets) == {(_norm("arsenal"), "soccer_epl"), (_norm("everton"), "soccer_epl")}
    assert targets[(_norm("arsenal"), "soccer_epl")]["specialty"] is True
    assert targets[(_norm("everton"), "soccer_epl")]["specialty"] is False


# ============================================================
# Recarga dirigida
# ============================================================

class _FakeApi:
    """Sustituye la API y la DB de update_upcoming_matches."""

    def __init__(self, monkeypatch, cache: dict, remaining: int = 10_000):
        import scripts.update_upcoming_matches as uum
        self.league_calls, self.event_calls, self.rows = [], [], []
        now = datetime.now(timezone.utc)

        def fetch_data(sport, cache_, force=False):
            self.league_calls.append((sport, force))
            data = [{"id": f"{sport}-1", "home_team": "arsenal", "away_team": "chelsea",
                     "bookmakers": [{"key": "bk", "markets": [{"key": "h2h"}]}]},
                    {"id": f"{sport}-2", "home_team": "everton", "away_team": "fulham",
                     "bookmakers": [{"key": "bk", "markets": [{"key": "h2h"}]}]}]
            cache_[sport] = {"fetched_at": now.isoformat(), "data": data}
            return data

        def fetch_event(sport, event_id, cache_):
            self.event_calls.append(event_id)
            bks = [{"key": "bk", "markets": [{"key": "btts"}]}]
            cache_[uum._enrich_cache_key(event_id)] = {"fetched_at": now.isoformat(), "bookmakers": bks}
            return bks

        def parse(m, sport):
            has_btts = any(mk.get("key") == "btts" for bk in m.get("bookmakers", [])
                           for mk in bk.get("markets", []))
            return {"fixture_id": m["id"], "match_key": m["id"], "home_odds": 2.0,
                    "btts_yes_odds": 1.8 if has_btts else None}

        monkeypatch.setattr(uum, "_preflight_credits_check", lambda: remaining)
        monkeypatch.setattr(uum, "ensure_schema", lambda: None)
        monkeypatch.setattr(uum, "load_cache", lambda: cache)
        monkeypatch.setattr(uum, "save_cache", lambda c: None)
        monkeypatch.setattr(uum, "fetch_data", fetch_data)
        monkeypatch.setattr(uum, "fetch_event_specialty_markets", fetch_event)
        monkeypatch.setattr(uum, "parse_match", parse)
        monkeypatch.setattr(uum, "upsert_matches", lambda rows: self.rows.extend(rows))
        self.uum = uum


def _target(sport, home, away, specialty=False):
    return {"sport_key": sport, "home_norm": _norm(home), "away_norm": _norm(away),
            "match_date": KICKOFF, "specialty": specialty}


def test_only_target_leagues_are_refetched_and_fresh_ones_skipped(monkeypatch):
    now = datetime.now(timezone.utc)
    cache = {"soccer_epl": {"fetched_at": (now - timedelta(minutes=5)).isoformat(),
                            "data": []}}
    api = _FakeApi(monkeypatch, cache)
    stats = api.uum.refresh_for_closing([
        _target("soccer_epl", "arsenal", "chelsea"),            # caché de 5 min: no paga
        _target("soccer_spain_la_liga", "arsenal", "chelsea"),  # se recarga
    ])
    assert api.league_calls == [("soccer_spain_la_liga", True)]
    assert stats["leagues"] == 1 and stats["skipped_fresh"] == 1


def test_per_event_refresh_only_for_flagged_events_and_marked_fresh(monkeypatch):
    api = _FakeApi(monkeypatch, cache={})
    api.uum.refresh_for_closing([_target("soccer_epl", "arsenal", "chelsea", specialty=True),
                                 _target("soccer_epl", "everton", "fulham", specialty=False)])
    assert api.event_calls == ["soccer_epl-1"]
    rows = {r["fixture_id"]: r for r in api.rows}
    assert rows["soccer_epl-1"]["specialty_fetched_at"] is not None
    assert rows["soccer_epl-1"]["odds_fetched_at"] is not None
    assert rows["soccer_epl-2"]["specialty_fetched_at"] is None     # sin mercados por evento


def test_league_cap_per_run(monkeypatch):
    api = _FakeApi(monkeypatch, cache={})
    monkeypatch.setattr(api.uum, "CLOSING_MAX_LEAGUES_PER_RUN", 2)
    api.uum.refresh_for_closing([_target(f"soccer_l{i}", "arsenal", "chelsea") for i in range(5)])
    assert len(api.league_calls) == 2


def test_low_credits_skip_refresh(monkeypatch):
    api = _FakeApi(monkeypatch, cache={}, remaining=100)
    stats = api.uum.refresh_for_closing([_target("soccer_epl", "arsenal", "chelsea")])
    assert api.league_calls == [] and stats.get("skipped_credits")


def test_refreshed_event_replaces_old_markets_instead_of_mixing(monkeypatch):
    """Si la liga venía de caché reciente con mercados viejos del evento, los
    nuevos los REEMPLAZAN (si no, una cuota vieja quedaría marcada fresca)."""
    import scripts.update_upcoming_matches as uum
    m = {"bookmakers": [{"key": "bk", "markets": [{"key": "h2h"}, {"key": "btts", "old": True}]}]}
    uum.strip_specialty(m)
    uum.merge_specialty(m, [{"key": "bk", "markets": [{"key": "btts", "old": False}]}])
    btts = [mk for bk in m["bookmakers"] for mk in bk["markets"] if mk["key"] == "btts"]
    assert len(btts) == 1 and btts[0]["old"] is False


def test_rows_without_per_event_markets_get_no_specialty_time():
    import scripts.update_upcoming_matches as uum
    cache = {"soccer_epl": {"fetched_at": "2030-01-05T14:00:00+00:00"},
             uum._enrich_cache_key("e1"): {"fetched_at": "2030-01-05T14:10:00+00:00"}}
    rows = uum.annotate_fetch_times(
        [{"fixture_id": "e1", "btts_yes_odds": 1.8}, {"fixture_id": "e2", "btts_yes_odds": None}],
        cache, "soccer_epl")
    assert rows[0]["specialty_fetched_at"] == "2030-01-05T14:10:00+00:00"
    assert rows[1]["specialty_fetched_at"] is None
    assert rows[1]["odds_fetched_at"] == "2030-01-05T14:00:00+00:00"
