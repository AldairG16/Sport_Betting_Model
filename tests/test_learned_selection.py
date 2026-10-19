"""
Filtros y horario que el sistema aprende solo (19-oct-26, a pedido del dueño).

- filter_learner: los filtros de selección (entre semana, rango de cuota,
  ligas difíciles) se apagan solo si los resultados los contradicen con
  fuerza; con pocos datos quedan prendidos.
- timing_learner: el horario (horas antes del kickoff) en que las ventajas
  contra Pinnacle pierden al cierre queda sin dinero real.
"""

import pandas as pd
import pytest

from src.models import filter_learner as FL
from src.models import timing_learner as TL
from src.models.rule_evidence import MIN_CLUSTERS, MIN_N, VERDICT_THIN


# ============================================================
# filter_learner
# ============================================================

def _gd(mean, se, n=150, clusters=60, verdict="x"):
    return {"gap_diff": {"mean": mean, "se": se, "n": n, "clusters": clusters, "verdict": verdict}}


def _evidence(**filters):
    return {"filters": filters, "updated_at": "2030-01-01"}


def test_filters_stay_on_without_enough_data():
    st = FL.learn_filters(_evidence(entre_semana=_gd(-0.10, 0.01, n=MIN_N - 1),
                                    liga_dura=_gd(-0.10, 0.01, clusters=MIN_CLUSTERS - 1)))
    assert all(node["on"] for node in st["filters"].values()) and st["off"] == []
    assert FL.learn_filters(None)["off"] == []                      # sin evidencia: como se diseñaron


def test_filter_turns_off_only_when_results_contradict_it():
    st = FL.learn_filters(_evidence(entre_semana=_gd(-0.05, 0.01),       # su grupo acierta MEJOR
                                    liga_dura=_gd(-0.05, 0.03),          # IC cruza 0: sin evidencia
                                    fuera_de_sweet_spot=_gd(0.05, 0.01)))  # el filtro acierta
    assert st["off"] == ["entre_semana"]
    assert st["filters"]["liga_dura"]["on"] and st["filters"]["fuera_de_sweet_spot"]["on"]


def test_an_off_filter_comes_back_when_the_data_stops_favoring_removal():
    prev = {"filters": {"entre_semana": {"on": False}}, "off": ["entre_semana"]}
    still = FL.learn_filters(_evidence(entre_semana=_gd(-0.01, 0.02)), prev)
    assert still["off"] == ["entre_semana"]                         # sigue negativa: sigue apagado
    back = FL.learn_filters(_evidence(entre_semana=_gd(0.002, 0.02)), prev)
    assert back["off"] == [] and FL.changed(prev, back) and not FL.changed(prev, still)
    # una cohorte nueva (pocos datos) vuelve a las reglas tal como se diseñaron
    assert FL.learn_filters(_evidence(entre_semana=_gd(-0.05, 0.01, n=10)), prev)["off"] == []


def test_filter_switches_default_to_on(monkeypatch):
    monkeypatch.setattr(FL, "load_state", lambda key: None)
    assert FL.load_filter_switches() == {n: True for n in FL.FILTERS}
    monkeypatch.setattr(FL, "load_state", lambda key: {"filters": {"liga_dura": {"on": False}}})
    assert FL.load_filter_switches() == {"entre_semana": True, "fuera_de_sweet_spot": True,
                                         "liga_dura": False}


def test_filter_report_says_what_changed():
    st = FL.learn_filters(_evidence(entre_semana=_gd(-0.05, 0.01)))
    txt = FL.format_report(st, {})
    assert "partidos entre semana: APAGADO (desde hoy)" in txt and "-5.0pt" in txt
    assert "ligas difíciles (Serie A, MLS, Ligue 1): PRENDIDO" in txt


# ============================================================
# timing_learner — funciones puras
# ============================================================

@pytest.mark.parametrize("hours, window", [
    (None, None), ("x", None), (0, None), (-3, None), (0.5, "0-12h"), (12, "0-12h"),
    (12.01, "12-24h"), (24, "12-24h"), (48, "24-48h"), (48.5, "48h+"), (900, "48h+")])
def test_window_of(hours, window):
    assert TL.window_of(hours) == window


def test_hours_until_and_is_blocked():
    now = pd.Timestamp("2030-01-05T12:00:00+00:00")
    assert TL.hours_until("2030-01-05T18:00:00+00:00", now) == pytest.approx(6.0)
    assert TL.hours_until(pd.Timestamp("2030-01-07T12:00:00"), now) == pytest.approx(48.0)  # sin zona = UTC
    assert TL.hours_until("no es fecha", now) is None
    assert TL.is_blocked(60, {"48h+"}) and not TL.is_blocked(6, {"48h+"})
    assert not TL.is_blocked(None, {"48h+"}) and not TL.is_blocked(60, set())


KO = pd.Timestamp("2030-01-05T15:00:00+00:00")


def _snap(key, hours_before, home_odds, pin=(2.00, 3.60, 4.00), **extra):
    return {"match_key": key, "kickoff": KO, "captured_at": KO - pd.Timedelta(hours=hours_before),
            "home_odds": home_odds, "draw_odds": None, "away_odds": None,
            "over25_odds": None, "under25_odds": None,
            "pin_home_odds": pin[0], "pin_draw_odds": pin[1], "pin_away_odds": pin[2],
            "pin_over25_odds": None, "pin_under25_odds": None, **extra}


def test_signals_use_the_filter_rule_and_the_valid_close():
    from src.features.pinnacle import pinnacle_prob
    close_pin = (2.10, 3.50, 3.90)
    snaps = pd.DataFrame([
        _snap("m1", 60, 2.30),                        # pasa el filtro (≈ +4 pt) a 60 h
        _snap("m1", 55, 2.30),                        # mismo horario: cuenta la primera
        _snap("m1", 30, 2.06),                        # ≈ +0.7 pt: no pasa el filtro
        _snap("m1", 1.0, 2.10, pin=close_pin),        # cierre (≤ 150 min)
        _snap("m2", 60, 2.30),                        # sin foto de cierre: no cuenta
        _snap("m2", 4.0, 2.30),
    ])
    sig = TL.build_signals(snaps)
    assert list(zip(sig["match_key"], sig["market"], sig["window"])) == [("m1", "home_win", "48h+")]
    p_close = pinnacle_prob("home_win", {"pin_home_odds": close_pin[0], "pin_draw_odds": close_pin[1],
                                         "pin_away_odds": close_pin[2]})
    assert sig["value"].iloc[0] == pytest.approx(p_close * 2.30 - 1)
    assert TL.build_signals(pd.DataFrame()).empty


def _season(n_matches, hours_before, value_sign, seed=0):
    """Un partido = una señal en el horario dado que al cierre gana o pierde."""
    import numpy as np
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_matches):
        key = f"{hours_before}-{i}"
        rows.append(_snap(key, hours_before, 2.30))
        # cierre: Pinnacle se mueve contra (local ≈ 41 %: −6 %) o a favor (≈ 51 %: +16 %)
        jitter = rng.uniform(-0.03, 0.03)
        close = (2.40 + jitter, 3.40, 3.20) if value_sign < 0 else (1.95 + jitter, 3.70, 4.30)
        rows.append(_snap(key, 1.0, 2.30, pin=close))
    return rows


def test_a_window_that_loses_against_the_close_is_blocked():
    snaps = pd.DataFrame(_season(110, 60, -1) + _season(110, 6, +1, seed=1))
    st = TL.learn_timing(snaps)
    assert st["blocked"] == ["48h+"]
    assert st["windows"]["48h+"]["mean"] < 0 < st["windows"]["0-12h"]["mean"]
    assert st["windows"]["12-24h"]["verdict"] == VERDICT_THIN and TL.has_verdict(st)
    assert TL.changed({}, st) and not TL.changed({"blocked": ["48h+"]}, st)
    # con pocos partidos, nada se bloquea
    assert TL.learn_timing(pd.DataFrame(_season(20, 60, -1)))["blocked"] == []


def test_a_blocked_window_needs_a_non_negative_mean_to_come_back():
    strong = {"n": 150, "clusters": 60}
    assert TL._decide({**strong, "mean": -0.03, "se": 0.01}, False)          # IC entero < 0
    assert not TL._decide({**strong, "mean": -0.01, "se": 0.01}, False)      # IC cruza 0
    assert TL._decide({**strong, "mean": -0.01, "se": 0.01}, True)           # bloqueado: sigue
    assert not TL._decide({**strong, "mean": 0.001, "se": 0.01}, True)       # media ≥ 0: vuelve
    assert not TL._decide({"n": 10, "clusters": 5, "mean": -0.5, "se": 0.01}, True)


def test_blocked_windows_ignore_unknown_labels(monkeypatch):
    monkeypatch.setattr(TL, "load_state", lambda key: {"blocked": ["48h+", "raro"]})
    assert TL.load_blocked_windows() == {"48h+"}
    monkeypatch.setattr(TL, "load_state", lambda key: None)
    assert TL.load_blocked_windows() == set()


def test_timing_report_is_readable():
    st = TL.learn_timing(pd.DataFrame(_season(110, 60, -1)))
    txt = TL.format_report(st, {})
    assert "más de 48 h" in txt and "BLOQUEADO (desde hoy)" in txt
    assert "Sin dinero real en: más de 48 h" in txt
    assert "Sin dinero real en: ningún horario" in TL.format_report(TL.learn_timing(pd.DataFrame()))


# ============================================================
# El pipeline usa lo aprendido
# ============================================================

def _keys(cap):
    return sorted((b["match"], b["market"]) for b in cap["bets"])


def test_midweek_filter_can_be_turned_off(monkeypatch):
    from tests.pipeline_harness import default_matches, run_pipeline
    monday = default_matches()
    monday["match_date"] = "2030-01-07T15:00:00+00:00"
    key = ("iota vs kappa", "home_win")                  # 6.9 pt: no llega a 5 pt × 1.4
    assert key not in _keys(run_pipeline(monkeypatch, monday))
    assert key in _keys(run_pipeline(monkeypatch, monday, learned={"filters": {"entre_semana": False}}))


def test_odds_band_filter_can_be_turned_off(monkeypatch):
    from tests.pipeline_harness import run_pipeline
    narrow = {"SWEET_SPOTS": {"home_win": (1.90, 3.80)}}            # 1.75 y 1.80 quedan fuera
    assert ("alpha vs beta", "home_win") not in _keys(run_pipeline(monkeypatch, overrides=narrow))
    assert ("alpha vs beta", "home_win") in _keys(run_pipeline(
        monkeypatch, overrides=narrow, learned={"filters": {"fuera_de_sweet_spot": False}}))


def test_tough_league_filter_can_be_turned_off(monkeypatch):
    from tests.pipeline_harness import run_pipeline
    tough = {"TOUGH_LEAGUES": {"soccer_germany_bundesliga"}}       # exige 7 pt; iota tiene 6.9
    key = ("iota vs kappa", "home_win")
    assert key not in _keys(run_pipeline(monkeypatch, overrides=tough))
    assert key in _keys(run_pipeline(monkeypatch, overrides=tough,
                                     learned={"filters": {"liga_dura": False}}))


def test_a_blocked_hour_waits_for_a_closer_run(monkeypatch):
    import src.models.proposals as P
    from tests.test_collection_mode import _with_pinnacle
    from tests.pipeline_harness import run_pipeline
    saved = []
    monkeypatch.setattr(P, "persist_proposals", lambda recs: saved.extend(recs) or len(recs))
    m = _with_pinnacle(1.60, 4.00, 6.00)                 # alpha vs beta: valor contra Pinnacle
    key = ("alpha vs beta", "home_win")
    # el arnés juega en 2030: más de 48 h antes del kickoff
    assert _keys(run_pipeline(monkeypatch, m, sharp_gate=True,
                              learned={"timing_blocked": {"0-12h"}})) == [key]
    saved.clear()
    assert run_pipeline(monkeypatch, m, sharp_gate=True,
                        learned={"timing_blocked": {"48h+"}})["bets"] == []
    decision = {(p["match"], p["market"]): p["decision"] for p in saved}
    assert decision[key] == P.HORARIO_SIN_VENTAJA
