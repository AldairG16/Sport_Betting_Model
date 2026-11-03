"""
Filtro aprendido por región para el 🎯 (2-nov-26, el dueño: "haz el filtro
aprendido por región para América"). Los 🎯 de América iban 25 de 59 contra
31.1 que esperaba el mercado (−14.15u) y perdían con cualquier ventaja. El
weekly decide con resultados: prendido, el pick de América se ve igual,
nunca 🎯; se apaga en cuanto América deja de perder contra el resto.
"""

import pytest

from src.models import filter_learner as FL
from src.models import program_picks as PK
from src.models.rule_evidence import MIN_CLUSTERS_GROUP, MIN_N_GROUP
from tests.fake_db import FakeEngine, FakeResult
from tests.test_low_history_picks import ROW, UP


def _rows(league, won, n, p=0.5, tag="m"):
    return [{"match": f"{tag}{i}", "league": league, "result": "win" if i < won else "loss", "market_prob": p}
            for i in range(n)]


AMERICA = _rows("soccer_usa_mls", 25, 59, tag="a")              # el 2-nov: 25 de 59
REST = _rows("soccer_epl", 80, 133, tag="e")


# ─── la evidencia y la decisión ──────────────────────────────────────────────

def test_the_evidence_compares_the_region_with_the_rest():
    gd = FL.region_evidence(AMERICA + REST, "america")
    assert (gd["n"], gd["clusters"], gd["n_rest"]) == (59, 59, 133)
    assert gd["region_gap"] == pytest.approx(25 / 59 - 0.5) and gd["rest_gap"] == pytest.approx(80 / 133 - 0.5)
    assert gd["mean"] == pytest.approx(gd["rest_gap"] - gd["region_gap"])           # + = la región es peor
    assert gd["se"] > 0
    rows = AMERICA + [{"match": "x", "league": "soccer_epl", "result": "push", "market_prob": 0.5},
                      {"match": "y", "league": "soccer_epl", "result": "win", "market_prob": float("nan")}]
    assert FL.region_evidence(rows, "america")["n_rest"] == 0 and FL.region_evidence(rows, "america")["mean"] is None


def test_it_turns_on_only_with_evidence_and_off_when_the_region_stops_losing():
    on = FL.learn_filters(None, {}, AMERICA + REST)
    assert on["paused"] == ["america"] and on["filters"]["america"]["on"]
    assert FL.changed({}, on)                                                        # se avisa al prenderse
    # una diferencia dentro del margen de error no lo prende…
    close = _rows("soccer_usa_mls", 32, 59, tag="a") + REST
    assert FL.learn_filters(None, {}, close)["paused"] == []
    # …pero, ya prendido, lo mantiene mientras la región pierda en promedio
    assert FL.learn_filters(None, on, close)["paused"] == ["america"]
    even = _rows("soccer_usa_mls", 36, 59, tag="a") + REST                           # 61% contra 60%: ya no pierde
    off = FL.learn_filters(None, on, even)
    assert off["paused"] == [] and FL.changed(on, off)


def test_with_few_picks_or_unreadable_picks_it_stays_as_it_was():
    few = _rows("soccer_usa_mls", 2, MIN_N_GROUP - 1, tag="a") + REST
    assert FL.learn_filters(None, {}, few)["paused"] == []                           # nace apagado
    on = FL.learn_filters(None, {}, AMERICA + REST)
    assert FL.learn_filters(None, on, few)["paused"] == ["america"]                  # se queda prendido
    assert FL.learn_filters(None, on, None)["paused"] == ["america"]                 # sin poder leer: igual
    thin_matches = [{**r, "match": f"a{i % (MIN_CLUSTERS_GROUP - 1)}"} for i, r in enumerate(AMERICA)]
    assert FL.learn_filters(None, {}, thin_matches + REST)["paused"] == []           # pocos partidos


def test_the_classic_filters_do_not_change(monkeypatch):
    st = FL.learn_filters({"filters": {"entre_semana": {"gap_diff": {"mean": -0.05, "se": 0.01, "n": 150,
                                                                     "clusters": 60}}}}, {}, AMERICA + REST)
    assert st["off"] == ["entre_semana"] and st["paused"] == ["america"]
    monkeypatch.setattr(FL, "load_state", lambda key: st)
    assert FL.load_filter_switches() == {"entre_semana": False, "fuera_de_sweet_spot": True,
                                         "liga_dura": True}                         # el pipeline lee los mismos tres


def test_the_report_says_what_the_region_filter_does():
    st = FL.learn_filters(None, {}, AMERICA + REST)
    txt = FL.format_report(st, {})
    assert "🎯 en América" in txt and "PRENDIDO, 🎯 en pausa (desde hoy)" in txt
    assert "región -7.6pt (n=59) contra el resto +10.2pt (n=133)" in txt
    back = FL.format_report(FL.learn_filters(None, st, _rows("soccer_usa_mls", 40, 59, tag="a") + REST), st)
    assert "APAGADO (el 🎯 vuelve hoy)" in back


def test_the_weekly_reads_only_picks_that_passed_the_rule():
    assert "reason IS NULL OR reason LIKE 'Región % en pausa%'" in FL.REGION_ROWS_SQL
    assert PK.REGION_PAUSED.format(region="América").startswith("Región América en pausa")


def test_paused_regions_reads_the_state_with_the_callers_connection():
    import json
    st = FL.learn_filters(None, {}, AMERICA + REST)
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[(json.dumps(st),)]))
    assert FL.paused_regions(eng) == ("america",)
    assert FL.paused_regions(FakeEngine()) == ()                                     # sin estado: ninguna

    class Down:
        def connect(self):
            raise RuntimeError("neon caído")
    assert FL.paused_regions(Down()) == ()                                           # ilegible: el 🎯 de siempre


# ─── el pick ─────────────────────────────────────────────────────────────────

def test_a_paused_region_keeps_its_pick_without_the_target():
    normal, why = PK.decide(dict(ROW), UP)
    assert why is None                                                              # en Bélgica pasa la regla
    mls = {**ROW, "league": "soccer_usa_mls"}
    pick, why_mls = PK.decide(mls, UP, paused=("america",))
    assert pick["market"] == normal["market"]                                       # el mismo pick…
    assert why_mls == PK.REGION_PAUSED.format(region="América")                     # …sin 🎯, con el motivo
    assert PK.decide(mls, UP) == (normal, None)                                     # apagado: 🎯 como siempre
    assert PK.decide(dict(ROW), UP, paused=("america",)) == (normal, None)          # otra región: nada cambia
    low, why_low = PK.decide({**mls, "low_history": "x con 4"}, UP, paused=("america",))
    assert why_low.startswith(PK.LOW_HISTORY)                                       # el otro motivo manda


def test_the_live_snapshot_applies_the_learned_pause(monkeypatch):
    from tests.test_program_picks import _reads
    import json
    reads = _reads()
    monkeypatch.setattr(PK.pd, "read_sql", lambda sql, con=None, params=None, **kw: next(
        df for k, df in reads.items() if k in str(sql)))
    st = json.dumps(FL.learn_filters(None, {}, AMERICA + REST))
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[(st,)]) if "filter_switches" in str(p) else FakeResult())
    assert PK.snapshot_picks(engine=eng) == 1
    (sql, params), = eng.statements("INSERT INTO program_picks")
    assert params["league0"] == "soccer_mexico_ligamx" and params["market0"] == "over25"
    assert params["reason0"] == PK.REGION_PAUSED.format(region="América")


def test_the_telegram_board_shows_the_paused_pick_without_the_target():
    from src.models.today_board import pick_text
    r = {"label": "Más de 2.5 goles", "reason": PK.REGION_PAUSED.format(region="América")}
    assert pick_text(r["label"], bool(r["label"]) and not r["reason"]) == "Más de 2.5 goles"
