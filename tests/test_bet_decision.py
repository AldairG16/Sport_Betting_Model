"""
tests/test_bet_decision.py
==========================
Tests para la cadena de decision pura (src/pipeline/bet_decision.py).

Verifica que:
  - El filtro corre sobre `edge_market` (edge real), nunca sobre `edge` (edge_ev)
  - Los mercados AH parametrizados resuelven su umbral via el grupo AH
  - `decide_bets` es determinista y no muta la entrada
  - El modulo no arrastra DB, settings ni red
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# HELPERS
# ============================================================

def _ah_group(market):
    """Replica minima de calibration_monitor._ah_group para no importar DB."""
    if not (market.startswith("ah_home_") or market.startswith("ah_away_")):
        return None
    side = "ah_home" if market.startswith("ah_home_") else "ah_away"
    try:
        line = float(market.rsplit("_", 1)[-1])
    except ValueError:
        return None
    if line < -0.25:
        return f"{side}_fav"
    if line <= 0.25:
        return f"{side}_pk"
    return f"{side}_dog"


def _flat_kelly(prob, odds, bankroll=100, market=None, league=None):
    """Kelly de juguete, determinista: 1% del bankroll por bet."""
    return round(bankroll * 0.01, 2)


def _make_bet(market="home_win", odds=2.0, prob=0.55, edge_market=0.05, side="home"):
    """Construye un bet con el contrato completo de llaves."""
    return {
        "match": "Team A vs Team B",
        "market": market,
        "side": side,
        "odds": odds,
        "prob": prob,
        "edge": prob * odds - 1,          # edge_ev (inflado por odds altas)
        "edge_market": edge_market,       # edge real
        "stake": 0.0,
    }


# ============================================================
# FILTRO DE EDGE MINIMO
# ============================================================

def test_filtra_sobre_edge_market_no_sobre_edge_ev():
    """Un bet con edge_ev alto pero edge_market bajo debe ser RECHAZADO."""
    from src.pipeline.bet_decision import apply_min_edge_filter

    # odds 5.0, prob 0.25 -> edge_ev = 0.25 (pasaria cualquier umbral),
    # pero edge_market = 0.25 - 1/5.0 = 0.05 -> por debajo de 0.10
    longshot = _make_bet(market="draw", odds=5.0, prob=0.25, edge_market=0.05)
    assert longshot["edge"] > 0.10, "el fixture debe tener edge_ev alto"

    out = apply_min_edge_filter([longshot], {"draw": 0.10}, 0.05, _ah_group)
    assert out == [], "filtrar sobre edge en vez de edge_market deja pasar longshots"


def test_acepta_bet_con_edge_market_suficiente():
    """Un bet cuyo edge real supera el umbral se conserva con todas sus llaves."""
    from src.pipeline.bet_decision import apply_min_edge_filter

    bet = _make_bet(market="home_win", odds=2.0, prob=0.60, edge_market=0.10)
    out = apply_min_edge_filter([bet], {"home_win": 0.06}, 0.05, _ah_group)

    assert len(out) == 1
    for key in ("match", "market", "side", "odds", "prob", "edge", "edge_market", "stake"):
        assert key in out[0], f"falta la llave {key} en el bet de salida"


def test_umbral_default_cuando_el_mercado_no_esta_en_la_tabla():
    """Mercado desconocido (no AH) cae al default."""
    from src.pipeline.bet_decision import apply_min_edge_filter

    bajo = _make_bet(market="btts", edge_market=0.03)
    alto = _make_bet(market="btts", edge_market=0.09)

    out = apply_min_edge_filter([bajo, alto], {"home_win": 0.06}, 0.05, _ah_group)
    assert len(out) == 1
    assert out[0]["edge_market"] == 0.09


# ============================================================
# FALLBACK DE GRUPO ASIAN HANDICAP
# ============================================================

def test_ah_parametrizado_usa_el_umbral_del_grupo_no_el_default():
    """El mercado ah_home_-0.5 resuelve a ah_home_fav (0.12), no al default (0.05)."""
    from src.pipeline.bet_decision import apply_min_edge_filter

    min_edge_by_market = {"home_win": 0.06, "ah_home_fav": 0.12}

    # edge real 0.08: pasa el default 0.05 pero NO el umbral del grupo 0.12
    bet = _make_bet(market="ah_home_-0.5", edge_market=0.08)
    out = apply_min_edge_filter([bet], min_edge_by_market, 0.05, _ah_group)
    assert out == [], "el AH cayo silenciosamente al default en vez de usar ah_home_fav"

    # edge real 0.15: supera el umbral del grupo
    bueno = _make_bet(market="ah_home_-0.5", edge_market=0.15)
    out = apply_min_edge_filter([bueno], min_edge_by_market, 0.05, _ah_group)
    assert len(out) == 1


def test_ah_sin_grupo_en_la_tabla_cae_al_default():
    """Si el grupo AH tampoco esta en la tabla, se usa el default."""
    from src.pipeline.bet_decision import apply_min_edge_filter

    bet = _make_bet(market="ah_away_1.5", edge_market=0.06)
    out = apply_min_edge_filter([bet], {"home_win": 0.06}, 0.05, _ah_group)
    assert len(out) == 1


# ============================================================
# SIZING KELLY
# ============================================================

def test_size_stakes_delega_en_kelly_fn():
    """El stake sale de kelly_fn, no de una formula interna."""
    from src.pipeline.bet_decision import size_stakes

    llamadas = []

    def spy(prob, odds, bankroll=100, market=None, league=None):
        llamadas.append((prob, odds, bankroll, market))
        return 2.5

    out = size_stakes([_make_bet()], bankroll=200.0, kelly_fn=spy)
    assert out[0]["stake"] == 2.5
    assert llamadas == [(0.55, 2.0, 200.0, "home_win")]


# ============================================================
# TOPES DE PORTFOLIO
# ============================================================

def test_portfolio_escala_cuando_supera_la_exposicion_maxima():
    """Stake total 30u con bankroll 100 y tope 15% -> escala a 15u."""
    from src.pipeline.bet_decision import apply_portfolio_caps

    bets = [
        dict(_make_bet(market="home_win"), stake=10.0),
        dict(_make_bet(market="over25"), stake=10.0),
        dict(_make_bet(market="btts"), stake=10.0),
    ]
    out = apply_portfolio_caps(bets, bankroll=100.0, max_total_pct=0.15)

    assert round(sum(b["stake"] for b in out), 2) == 15.0
    assert all(b["stake"] == 5.0 for b in out)


def test_portfolio_no_toca_nada_si_esta_bajo_el_tope():
    """Sin exceso de exposicion ni concentracion, los stakes quedan intactos."""
    from src.pipeline.bet_decision import apply_portfolio_caps

    bets = [
        dict(_make_bet(market="home_win"), stake=2.0),
        dict(_make_bet(market="over25"), stake=2.0),
    ]
    out = apply_portfolio_caps(bets, bankroll=100.0, max_total_pct=0.15)
    assert [b["stake"] for b in out] == [2.0, 2.0]


def test_portfolio_penaliza_el_mercado_dominante():
    """Concentracion > 60% con stake total > 5 -> penalizacion 0.75 al dominante."""
    from src.pipeline.bet_decision import apply_portfolio_caps

    bets = [
        dict(_make_bet(market="home_win"), stake=8.0),
        dict(_make_bet(market="over25"), stake=2.0),
    ]
    out = apply_portfolio_caps(bets, bankroll=1000.0, max_total_pct=0.15)

    assert out[0]["stake"] == 6.0   # 8.0 * 0.75
    assert out[1]["stake"] == 2.0   # no dominante, sin tocar


def test_portfolio_con_bankroll_cero_no_divide_por_cero():
    """Bankroll 0 devuelve los bets sin escalar en vez de reventar."""
    from src.pipeline.bet_decision import apply_portfolio_caps

    bets = [dict(_make_bet(), stake=3.0)]
    out = apply_portfolio_caps(bets, bankroll=0.0, max_total_pct=0.15)
    assert out[0]["stake"] == 3.0


def test_portfolio_lista_vacia():
    """Cartera vacia devuelve lista vacia."""
    from src.pipeline.bet_decision import apply_portfolio_caps
    assert apply_portfolio_caps([], bankroll=100.0, max_total_pct=0.15) == []


# ============================================================
# CADENA COMPLETA / DETERMINISMO
# ============================================================

def _decidir(bets):
    from src.pipeline.bet_decision import decide_bets
    return decide_bets(
        bets,
        bankroll=100.0,
        min_edge_by_market={"home_win": 0.06, "over25": 0.06, "ah_home_fav": 0.12},
        min_edge_default=0.05,
        ah_group=_ah_group,
        kelly_fn=_flat_kelly,
        max_total_pct=0.15,
    )


def test_decide_bets_compone_filtro_sizing_y_topes():
    """La cadena descarta el bet flojo y asigna stake a los que pasan."""
    entrada = [
        _make_bet(market="home_win", edge_market=0.10),
        _make_bet(market="over25", edge_market=0.01),   # bajo umbral -> fuera
        _make_bet(market="ah_home_-0.5", edge_market=0.20),
    ]
    out = _decidir(entrada)

    assert [b["market"] for b in out] == ["home_win", "ah_home_-0.5"]
    assert all(b["stake"] == 1.0 for b in out)   # 1% de 100u


def test_decide_bets_es_determinista():
    """Dos llamadas con la misma entrada producen exactamente la misma salida."""
    entrada = [
        _make_bet(market="home_win", edge_market=0.10),
        _make_bet(market="over25", edge_market=0.10),
    ]
    primera = _decidir(entrada)
    segunda = _decidir(entrada)
    assert primera == segunda


def test_decide_bets_no_muta_la_entrada():
    """La lista original conserva sus stakes: la cadena trabaja sobre copias."""
    entrada = [_make_bet(market="home_win", edge_market=0.10)]
    antes = [dict(b) for b in entrada]
    _decidir(entrada)
    assert entrada == antes


def test_decide_bets_repetido_no_reaplica_la_penalizacion():
    """Reejecutar sobre la MISMA lista no acumula la penalizacion de concentracion."""
    from src.pipeline.bet_decision import decide_bets

    def kelly_gordo(prob, odds, bankroll=100, market=None, league=None):
        return 8.0 if market == "home_win" else 2.0

    entrada = [
        _make_bet(market="home_win", edge_market=0.10),
        _make_bet(market="over25", edge_market=0.10),
    ]
    kwargs = dict(
        bankroll=1000.0,
        min_edge_by_market={"home_win": 0.06, "over25": 0.06},
        min_edge_default=0.05,
        ah_group=_ah_group,
        kelly_fn=kelly_gordo,
        max_total_pct=0.15,
    )
    primera = decide_bets(entrada, **kwargs)
    segunda = decide_bets(entrada, **kwargs)
    assert primera == segunda
    assert primera[0]["stake"] == 6.0   # 8.0 * 0.75, una sola vez


def test_decide_bets_conserva_el_contrato_de_llaves():
    """Cada bet de salida conserva las llaves del contrato."""
    out = _decidir([_make_bet(market="home_win", edge_market=0.10)])
    for key in ("match", "market", "side", "odds", "prob", "edge", "edge_market", "stake"):
        assert key in out[0], f"falta la llave {key}"


def test_decide_bets_sin_candidatos():
    """Si nada pasa el filtro, la cadena devuelve lista vacia."""
    assert _decidir([_make_bet(market="home_win", edge_market=0.001)]) == []


# ============================================================
# PUREZA DEL MODULO
# ============================================================

def test_modulo_no_importa_db_ni_settings():
    """bet_decision.py no puede depender de config, sqlalchemy ni red."""
    import ast

    ruta = Path(__file__).parent.parent / "src" / "pipeline" / "bet_decision.py"
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))

    prohibidos = ("config", "sqlalchemy", "psycopg2", "requests", "pandas", "anthropic")
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres = [a.name for a in nodo.names]
        elif isinstance(nodo, ast.ImportFrom):
            nombres = [nodo.module or ""]
        else:
            continue
        for nombre in nombres:
            raiz = nombre.split(".")[0]
            assert raiz not in prohibidos, f"import prohibido en bet_decision.py: {nombre}"
