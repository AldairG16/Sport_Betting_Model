"""
Créditos de The Odds API (29-oct-26): el saldo cruzó el umbral de parada el
28-oct, 3.5 días antes de la renovación del 1-nov, con el presupuesto diario
puesto.

- La actualización normal del closing (ligas cada 12 h y mercados por evento
  de las próximas 48 h) no tenía tope: ~85% del gasto del 23-24 oct.
- Morning, evening y resultados (~200 por día) salían del mismo presupuesto.
- Los resultados pedían todas las ligas en cada corrida (~114 créditos al día).
- Con el saldo bajo el umbral, cada corrida mandaba su aviso por Telegram.
- Los cierres no tenían prioridad: del 24 al 28 de oct solo 0-53% de los
  partidos tuvo un cierre válido (antes 98-100%).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# ============================================================
# Presupuesto: lo que no se frena sale aparte
# ============================================================

def test_the_closing_leaves_room_for_morning_evening_and_results():
    from src.utils.credit_budget import day_state, run_budget
    day = day_state(None, 6600, utc(2026, 10, 20, 1))              # (6,600 − 600) / ~11.96 días ≈ 502
    assert run_budget(day, 6600, mandatory=200) == pytest.approx(day["allowance"] - 200)
    assert run_budget(day, 6400, mandatory=200) == pytest.approx(day["allowance"] - 400)
    assert run_budget(day, 6250, mandatory=200) == 0                # el día ya no alcanza: nada para el closing


def test_a_credit_alert_goes_once_per_utc_day(monkeypatch):
    from src.utils import credit_budget as CB
    from src.utils import model_state
    store, sent = {}, []
    monkeypatch.setattr(model_state, "load_state", lambda k: store.get(k))
    monkeypatch.setattr(model_state, "save_state", lambda k, v, **kw: store.__setitem__(k, v) or True)
    for hour in (3, 12, 23):                                        # evening, morning y un closing del mismo día
        CB.alert_once("stop", "sin cuotas", now=utc(2026, 10, 29, hour), send=sent.append)
    assert sent == ["sin cuotas"]
    CB.alert_once("stop", "sin cuotas", now=utc(2026, 10, 30, 0, 30), send=sent.append)
    assert len(sent) == 2                                           # al día siguiente, otra vez

    def broken(k):
        raise RuntimeError("sin base")
    monkeypatch.setattr(model_state, "load_state", broken)
    CB.alert_once("stop", "sin cuotas", now=utc(2026, 10, 30, 1), send=sent.append)
    assert len(sent) == 3                                           # sin base: mejor repetido que callado


def test_below_the_stop_threshold_the_runs_do_not_spam_telegram(monkeypatch):
    import scripts.update_upcoming_matches as uum
    from src.utils import credit_budget as CB
    sent = []
    monkeypatch.setattr(uum, "_preflight_credits_check", lambda: 381)
    monkeypatch.setattr(uum, "API_CREDITS_STOP_THRESHOLD", 500)
    monkeypatch.setattr(CB, "alert_once", lambda kind, html, **kw: sent.append((kind, html)))
    uum.update_all()
    assert len(sent) == 1 and sent[0][0] == "stop"
    assert "Quedan <b>381</b>" in sent[0][1] and "se renueve el" in sent[0][1]
    assert "una vez al día" in sent[0][1]


# ============================================================
# La actualización normal del closing
# ============================================================

class _Api:
    """Sustituye la API, la caché y la DB de update_all."""

    def __init__(self, monkeypatch, cache, budget=float("inf"), remaining=5000, spend=None):
        import scripts.update_upcoming_matches as uum
        self.league_calls, self.event_calls, self.rows = [], [], []
        self.uum = uum
        state = {"remaining": remaining}

        def fetch_data(sport, cache_, force=False):
            if uum.is_cache_valid(cache_, sport) and not force:        # caché vigente: sin llamada
                return cache_[sport]["data"]
            self.league_calls.append(sport)
            state["remaining"] -= 3
            uum._last_known_remaining = state["remaining"]
            cache_[sport] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": cache_[sport]["data"]}
            return cache_[sport]["data"]

        def fetch_event(sport, event_id, cache_):
            self.event_calls.append(event_id)
            state["remaining"] -= 7
            uum._last_known_remaining = state["remaining"]
            return [{"key": "bk", "markets": [{"key": "btts"}]}]

        monkeypatch.setattr(uum, "SPORT_KEYS", list(cache))
        monkeypatch.setattr(uum, "_preflight_credits_check",
                            lambda: setattr(uum, "_last_known_remaining", remaining) or remaining)
        monkeypatch.setattr(uum, "API_CREDITS_STOP_THRESHOLD", 500)
        monkeypatch.setattr(uum, "_budgets", lambda r, c: ({"start": r}, float(budget), float(budget)))
        for name in ("_cleanup_old_matches", "ensure_schema", "_renormalize_names", "_cleanup_stale_duplicates"):
            monkeypatch.setattr(uum, name, lambda: None)
        monkeypatch.setattr(uum, "load_cache", lambda: cache)
        monkeypatch.setattr(uum, "save_cache", lambda c: None)
        monkeypatch.setattr(uum, "fetch_data", fetch_data)
        monkeypatch.setattr(uum, "fetch_event_specialty_markets", fetch_event)
        monkeypatch.setattr(uum, "merge_specialty", lambda m, extra: None)
        monkeypatch.setattr(uum, "_event_already_enriched", lambda m: False)
        monkeypatch.setattr(uum, "parse_match", lambda m, sport: {"fixture_id": m["id"]})
        monkeypatch.setattr(uum, "annotate_fetch_times", lambda rows, cache_, sport: rows)
        monkeypatch.setattr(uum, "upsert_matches", lambda rows: self.rows.extend(rows))
        monkeypatch.setattr(uum.time, "sleep", lambda s: None)
        monkeypatch.setattr(uum, "MAX_ENRICHMENTS_PER_RUN", 40)


def _league(sport, *hours_to_kickoff, fetched_hours_ago=13):
    now = datetime.now(timezone.utc)
    events = [{"id": f"{sport}-{i}", "commence_time": (now + timedelta(hours=h)).isoformat().replace("+00:00", "Z")}
              for i, h in enumerate(hours_to_kickoff)]
    return {"fetched_at": (now - timedelta(hours=fetched_hours_ago)).isoformat(), "data": events}


def test_the_closing_only_refreshes_what_kicks_off_soon(monkeypatch):
    cache = {"soccer_soon": _league("soccer_soon", 2, 30),             # caché vencida, partido en 2 h
             "soccer_later": _league("soccer_later", 20, 40),          # caché vencida, nada en 6 h
             "soccer_fresh": _league("soccer_fresh", 1, fetched_hours_ago=1)}
    api = _Api(monkeypatch, cache)
    api.uum.update_all(budgeted=True)
    assert api.league_calls == ["soccer_soon"]
    # mercados por evento: solo lo que empieza en 6 h (antes: las próximas 48)
    assert api.event_calls == ["soccer_soon-0", "soccer_fresh-0"]
    assert len(api.rows) == 5                                          # todos se procesan con su cuota guardada


def test_the_morning_still_refreshes_everything(monkeypatch):
    cache = {"soccer_soon": _league("soccer_soon", 2, 30), "soccer_later": _league("soccer_later", 20, 40)}
    api = _Api(monkeypatch, cache)
    api.uum.update_all()
    assert api.league_calls == ["soccer_soon", "soccer_later"]
    assert api.event_calls == ["soccer_soon-0", "soccer_soon-1", "soccer_later-0", "soccer_later-1"]


def test_the_closing_update_stops_at_the_daily_budget(monkeypatch, capsys):
    cache = {"soccer_a": _league("soccer_a", 1, 2), "soccer_b": _league("soccer_b", 3)}
    api = _Api(monkeypatch, cache, budget=12)                          # liga a (3) + 1 evento (7) = 10; no cabe más
    api.uum.update_all(budgeted=True)
    assert api.league_calls == ["soccer_a"] and api.event_calls == ["soccer_a-0"]
    assert "presupuesto del día usado" in capsys.readouterr().out
    assert len(api.rows) == 3                                          # soccer_b sigue con su cuota guardada

    api = _Api(monkeypatch, {"soccer_a": _league("soccer_a", 1)}, budget=0)
    api.uum.update_all(budgeted=True)
    assert api.league_calls == [] and api.event_calls == []


# ============================================================
# Resultados: solo las ligas a las que les falta alguno
# ============================================================

def _scores_api(monkeypatch, needed, cost="2"):
    import scripts.fetch_results as FR
    calls = []

    class _R:
        headers = {"x-requests-last": cost} if cost is not None else {}

        def raise_for_status(self):
            pass

        def json(self):
            return []
    monkeypatch.setattr(FR, "SPORT_KEYS", ["soccer_epl", "soccer_usa_mls", "soccer_brazil_campeonato"])
    monkeypatch.setattr(FR, "leagues_needing_results", lambda d: needed)
    monkeypatch.setattr(FR.requests, "get", lambda url, params=None, timeout=None: calls.append(url) or _R())
    return FR, calls


def test_results_are_asked_only_for_leagues_that_miss_one(monkeypatch, capsys):
    FR, calls = _scores_api(monkeypatch, {"soccer_usa_mls"})
    FR.fetch_all_results(days_from=3)
    assert [c.split("/sports/")[1].split("/")[0] for c in calls] == ["soccer_usa_mls"]
    out = capsys.readouterr().out
    assert "Ligas con marcadores pendientes: 1 de 3" in out and "Créditos API usados: 2" in out

    FR, calls = _scores_api(monkeypatch, set())                        # nada pendiente: 0 créditos
    assert FR.fetch_all_results(days_from=3) == 0 and calls == []

    FR, calls = _scores_api(monkeypatch, None, cost=None)              # no se pudo leer: todas, como antes
    FR.fetch_all_results(days_from=3)
    assert len(calls) == 3 and "Créditos API usados: 6" in capsys.readouterr().out


def test_a_league_misses_a_result_until_matches_has_it(monkeypatch):
    import scripts.fetch_results as FR
    now = pd.Timestamp.now(tz="UTC")

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = " ".join(str(sql).split())
        if "FROM model_vs_sharp" in s:
            return pd.DataFrame([{"match": "seattle sounders vs portland timbers", "match_date": now - pd.Timedelta(hours=5),
                                  "league": "soccer_usa_mls"},
                                 {"match": "arsenal vs chelsea", "match_date": now - pd.Timedelta(hours=20),
                                  "league": "soccer_epl"}])
        return pd.DataFrame([{"h": "arsenal", "a": "chelsea", "d": (now - pd.Timedelta(hours=20)).date(),
                              "home_goals": 2, "away_goals": 1}])
    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(FR, "normalize_team", lambda t: t)
    assert FR.leagues_needing_results(3) == {"soccer_usa_mls"}

    def broken(*a, **k):
        raise RuntimeError("sin base")
    monkeypatch.setattr(pd, "read_sql", broken)
    assert FR.leagues_needing_results(3) is None


# ============================================================
# Los cierres primero (29-oct-26)
# ============================================================

def test_the_reserve_counts_each_league_kickoff_left_today():
    from src.utils.credit_budget import capture_reserve
    now = utc(2026, 10, 24, 12)
    kickoffs = [("soccer_epl", utc(2026, 10, 24, 14)), ("soccer_epl", utc(2026, 10, 24, 14)),  # mismo horario
                ("soccer_epl", utc(2026, 10, 24, 16, 30)), ("soccer_spain_la_liga", utc(2026, 10, 24, 14)),
                ("soccer_usa_mls", utc(2026, 10, 25, 0, 30)),   # su ventana de cierre empieza hoy
                ("soccer_usa_mls", utc(2026, 10, 25, 2)),       # mañana: otro presupuesto
                ("soccer_epl", utc(2026, 10, 24, 11))]          # ya empezó
    assert capture_reserve(kickoffs, now, league_cost=3) == 4 * 2 * 3


def test_what_is_not_a_closing_never_eats_the_reserve():
    from src.utils.credit_budget import extras_budget
    day = {"day": "2026-10-24", "start": 3510, "allowance": 646.0}
    assert extras_budget(day, 3510, reserve=264) == pytest.approx(646 - 200 - 264)
    assert extras_budget(day, 3300, reserve=264) == 0           # el día ya va en 210: nada más que cierres


def test_the_morning_leaves_the_closings_their_credits(monkeypatch):
    """El sábado 24-oct a las 13:30 el closing ya estaba "en pausa, 803 de 364
    créditos": los mercados por evento se comían el día antes de los partidos."""
    import scripts.update_upcoming_matches as uum
    from src.utils import credit_budget as CB
    real_budgets = uum._budgets
    now = datetime.now(timezone.utc)
    events = [{"id": f"ev{i}", "commence_time": (now + timedelta(minutes=15 + 12 * i)).isoformat().replace("+00:00", "Z")}
              for i in range(15)]                               # 5 en la próxima hora y media, el resto más tarde
    cache = {"soccer_sat": {"fetched_at": (now - timedelta(hours=13)).isoformat(), "data": events}}
    api = _Api(monkeypatch, cache, remaining=10_000)
    monkeypatch.setattr(uum, "_budgets", real_budgets)
    monkeypatch.setattr(CB, "load_and_update", lambda r, now=None: {"day": "x", "start": 10_000, "allowance": 300.0})
    reserve = CB.capture_reserve(uum._kickoffs(cache), datetime.now(timezone.utc), 3)
    api.uum.update_all()                                        # morning: sin budgeted
    assert api.league_calls == ["soccer_sat"]                   # sus ligas no se frenan
    assert len(api.event_calls) == int((300 - 200 - reserve) // 7) < 14   # sin reserva habrían sido 14


def test_a_saved_event_is_reattached_for_free_and_does_not_use_the_cap(monkeypatch):
    import scripts.update_upcoming_matches as uum
    now = datetime.now(timezone.utc)
    cache = {"soccer_a": _league("soccer_a", 5, 30),
             "_enrich:soccer_a-0": {"fetched_at": (now - timedelta(hours=20)).isoformat(),   # vigente (48 h)
                                    "bookmakers": [{"key": "bk", "markets": [{"key": "btts"}]}]}}
    api = _Api(monkeypatch, cache, budget=0)                    # sin presupuesto para mercados por evento
    monkeypatch.setattr(uum, "SPORT_KEYS", ["soccer_a"])
    merged = []
    monkeypatch.setattr(uum, "merge_specialty", lambda m, extra: merged.append(m["id"]))
    api.uum.update_all()
    assert api.event_calls == [] and merged == ["soccer_a-0"]   # antes: un tope de 0 lo dejaba sin mercados

    api = _Api(monkeypatch, cache, budget=7)                    # cabe uno: es el que no estaba guardado
    monkeypatch.setattr(uum, "SPORT_KEYS", ["soccer_a"])
    api.uum.update_all()
    assert api.event_calls == ["soccer_a-1"]


def test_the_closing_captures_before_the_normal_update():
    import inspect
    import scripts.orchestrator as O
    src = inspect.getsource(O.step_pre_kickoff_closing)
    assert src.index("refresh_for_closing(targets)") < src.index("update_all(force=False, budgeted=True)")
    assert src.index("update_all(force=False, budgeted=True)") < src.index("update_closing_odds()")


def test_closings_are_not_cut_by_fixed_floors_at_the_end_of_the_month():
    import scripts.update_upcoming_matches as uum
    from src.utils.credit_budget import RESERVE
    # 1,500 apagaba los cierres los últimos ~2 días de cada mes (27-oct: 17%, 28-oct: 0%)
    assert uum.CLOSING_MIN_CREDITS <= RESERVE
    assert uum.ENRICH_CACHE_TTL_HOURS >= uum.ENRICH_DAYS_AHEAD * 24   # un pago por partido antes del cierre


def test_every_run_ends_with_its_warnings_in_one_greppable_line(capsys):
    import scripts.orchestrator as O
    from src.utils.log import RUN_ISSUES, get_logger
    RUN_ISSUES.reset()
    O._report_warnings("closing")
    assert "Avisos de la corrida (closing): ninguno" in capsys.readouterr().out
    lg = get_logger("tests.avisos")
    for msg in ("⚠️  Kalshi: Timeout", "Presupuesto de créditos ilegible", "⚠️  Kalshi: Timeout"):
        lg.warning(msg)
    O._report_warnings("closing")
    out = capsys.readouterr().out
    assert "Avisos de la corrida (closing, 2 distintos): ⚠️  Kalshi: Timeout | Presupuesto de créditos ilegible" in out
    RUN_ISSUES.reset()
