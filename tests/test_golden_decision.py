"""
tests/test_golden_decision.py
=============================
Harness de regresion del FIXTURE DORADO.

QUE PRUEBA ESTO Y POR QUE IMPORTA:
la auditoria toca configuracion, imports, manejo de errores y documentacion en
medio repositorio. La pregunta que el operador necesita responder antes de
mergear nada es una sola: "¿alguna de esas remediaciones cambio una apuesta?".

Este test la responde de forma mecanica. Reproduce un slate congelado a traves
de `src.pipeline.bet_decision.decide_bets` con el sizing de
`src.models.betting_engine.kelly_stake` -- LAS MISMAS funciones que llama
produccion, no copias -- y exige que las tuplas (match, market, side, stake)
sean identicas a la linea base commiteada.

NO HAY UNA SEGUNDA KELLY. Un `frozen_kelly_stake` paralelo haria que este
harness midiera el codigo del test: se podria cambiar la fraccion de Kelly, el
tope por bet o la penalizacion por odds altas en produccion y el fixture no se
moveria un centavo. El unico factor no determinista de `kelly_stake` -- el
cache de CLV que el ciclo weekly regenera -- se fija en vacio, sin sustituir la
funcion (ver `_clv_cache_neutral`).

⚠️  SI ESTE TEST FALLA, EL FIXTURE NO SE ACTUALIZA.
    Fallar significa que el codigo cambio una decision de apuesta. Eso se
    revisa, se justifica y, si es intencional, se documenta -- pero regenerar
    golden_output.json para que pase convierte la prueba en una tautologia
    (codigo nuevo comparado contra si mismo) y destruye su unico valor.

No requiere base de datos, red, ni claves: todo lo que necesita esta en
tests/fixtures/.
"""

import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Llaves que forman la identidad de una apuesta. Es exactamente lo que el
# operador compara: mismo partido, mismo mercado, mismo lado, mismo dinero.
_DECISION_KEYS = ("match", "market", "side", "stake")


# ============================================================
# HELPERS
# ============================================================

def _load(name):
    """Carga un JSON de tests/fixtures/."""
    import json

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _normalized_sha256(path):
    """SHA-256 del contenido con CRLF normalizado a LF.

    POR QUE NORMALIZAR EN VEZ DE HASHEAR LOS BYTES CRUDOS:
    el repo corre con `core.autocrlf=true` y sin .gitattributes, asi que un
    mismo commit se materializa con CRLF en Windows y con LF en ubuntu-latest.
    Hashear los bytes tal cual haria que el manifiesto fuera valido en una
    plataforma e invalido en la otra, y el test dorado empezaria a fallar en CI
    por una razon que no tiene nada que ver con las apuestas.

    Normalizar los finales de linea elimina esa diferencia y NO debilita la
    verificacion: cualquier edicion real del contenido (un stake, un odds, una
    llave) cambia el hash igual.
    """
    import hashlib

    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _production_kelly():
    """Devuelve LA funcion de sizing que usa `run_prediction_pipeline()`.

    No es una copia congelada. Hasta la remediacion R002 este harness inyectaba
    `frozen_kelly_stake` (una reimplementacion que vivia en
    scripts/capture_golden_fixture.py) y por lo tanto demostraba una propiedad
    del test, no del codigo que apuesta dinero: se podia cambiar la fraccion de
    Kelly, el tope por bet o la penalizacion por odds altas en produccion sin
    que el fixture dorado se moviera.

    `src.models.betting_engine` se importa sin base de datos: no toca
    `config.settings` en tiempo de import y su unico acceso a `config.database`
    esta diferido dentro del cuerpo de `refresh_clv_cache()`.
    """
    from src.models.betting_engine import kelly_stake

    return kelly_stake


@contextlib.contextmanager
def _clv_cache_neutral():
    """Fija el cache de CLV en vacio mientras corre el replay.

    `kelly_stake` NO es pura: cuando recibe `market` delega en
    `_adjusted_kelly_fraction`, que lee `data/clv_cache.json` -- un archivo que
    el ciclo weekly regenera y que escala los stakes por 1.20 o por 0.60 segun
    el CLV reciente. Sin fijarlo, un fixture "congelado" cambiaria de resultado
    cada lunes sin que nadie tocara una linea de codigo, y el harness no podria
    correr identico en Windows local y en ubuntu-latest (donde `data/` ni
    siquiera existe).

    Fijarlo en vacio deja exactamente el componente determinista del sizing
    (Kelly fraccionario, penalizacion por odds altas, tope por bet) y neutraliza
    el unico factor que depende de estado externo. La funcion que corre sigue
    siendo la de produccion: NO se sustituye, solo se le quita la entrada
    variable. El estado previo se restaura siempre para no contaminar otros
    tests de la misma sesion.
    """
    from src.models import betting_engine

    prev_cache = betting_engine._clv_cache_mem
    prev_loaded_at = betting_engine._clv_cache_loaded_at
    betting_engine._clv_cache_mem = {}
    betting_engine._clv_cache_loaded_at = time.time()
    try:
        yield
    finally:
        betting_engine._clv_cache_mem = prev_cache
        betting_engine._clv_cache_loaded_at = prev_loaded_at


def _replay():
    """Corre el fixture congelado por la cadena de decision de produccion.

    Devuelve las tuplas de decision proyectadas y ordenadas de forma estable.
    """
    from scripts.capture_golden_fixture import ah_group
    from src.pipeline.bet_decision import decide_bets

    payload = _load("golden_input.json")

    with _clv_cache_neutral():
        bets = decide_bets(
            payload["scored"],
            bankroll=payload["bankroll"],
            min_edge_by_market=payload["min_edge_by_market"],
            min_edge_default=payload["min_edge_default"],
            ah_group=ah_group,
            kelly_fn=_production_kelly(),
            max_total_pct=payload["max_total_pct"],
        )

    return sorted(
        ({k: b[k] for k in _DECISION_KEYS} for b in bets),
        key=lambda r: (r["match"], r["market"], r["side"]),
    )


# ============================================================
# INTEGRIDAD DEL FIXTURE
# ============================================================

def test_manifest_declara_el_fixture_congelado():
    """El manifiesto tiene que decir explicitamente que esto no se regenera."""
    manifest = _load("golden_manifest.json")

    assert manifest["frozen"] is True, (
        "golden_manifest.json debe declarar frozen=true: el fixture es una "
        "linea base permanente, no un snapshot refrescable."
    )
    assert manifest["input_sha256"]
    assert manifest["output_sha256"]
    assert manifest["created"]


def test_fixture_de_entrada_no_fue_editado():
    """El slate congelado debe coincidir con el SHA-256 del manifiesto."""
    manifest = _load("golden_manifest.json")
    actual = _normalized_sha256(FIXTURES / "golden_input.json")

    assert actual == manifest["input_sha256"], (
        "golden_input.json fue modificado. Este archivo esta congelado de "
        "forma permanente; si de verdad hay que cambiar el slate, es un "
        "fixture nuevo con su propia justificacion, no una edicion silenciosa. "
        f"esperado={manifest['input_sha256']} actual={actual}"
    )


def test_fixture_de_salida_no_fue_editado():
    """La linea base de decisiones debe coincidir con su SHA-256."""
    manifest = _load("golden_manifest.json")
    actual = _normalized_sha256(FIXTURES / "golden_output.json")

    assert actual == manifest["output_sha256"], (
        "golden_output.json fue modificado. Editar la salida esperada para "
        "que un test pase es exactamente el fallo que este harness existe "
        "para impedir. "
        f"esperado={manifest['output_sha256']} actual={actual}"
    )


# ============================================================
# COBERTURA DEL SLATE
# ============================================================

def test_el_slate_cubre_los_mercados_que_importan():
    """1X2, over/under, BTTS y una llave AH parametrizada, como minimo.

    Un fixture que solo tuviera home_win no probaria nada sobre el bug que mas
    ha costado: los mercados AH parametrizados cayendo al umbral default.
    """
    payload = _load("golden_input.json")
    markets = {b["market"] for b in payload["scored"]}

    assert {"home_win", "away_win", "draw"} & markets, "falta un mercado 1X2"
    assert {"over25", "under25"} & markets, "falta un mercado over/under"
    assert "btts_yes" in markets, "falta BTTS"

    ah_markets = {m for m in markets if m.startswith(("ah_home_", "ah_away_"))}
    assert ah_markets, "falta al menos una llave AH parametrizada"


def test_las_llaves_ah_resuelven_a_un_grupo_de_calibracion():
    """Cada llave AH del slate debe mapear a un grupo, no caer al default."""
    from scripts.capture_golden_fixture import ah_group

    payload = _load("golden_input.json")
    ah_markets = [
        b["market"]
        for b in payload["scored"]
        if b["market"].startswith(("ah_home_", "ah_away_"))
    ]

    assert ah_markets, "el slate debe incluir mercados AH"
    for market in ah_markets:
        assert ah_group(market) is not None, (
            f"{market} no resuelve a un grupo AH: caeria al min_edge_default "
            "y dejaria pasar bets por debajo de su umbral real."
        )


# ============================================================
# UNA SOLA KELLY: LA DE PRODUCCION
# ============================================================

def test_el_harness_inyecta_la_kelly_de_produccion():
    """El objeto que recibe `decide_bets` ES `betting_engine.kelly_stake`.

    Identidad de objeto, no equivalencia de resultados: si alguien vuelve a
    introducir una reimplementacion "congelada" que hoy da los mismos numeros,
    este assert falla igual -- que es el punto. Dos implementaciones que
    coinciden hoy divergen el dia que alguien toca una sola de ellas, y la que
    se toca siempre es la de produccion.
    """
    from src.models import betting_engine

    assert _production_kelly() is betting_engine.kelly_stake, (
        "El harness dorado debe inyectar la MISMA funcion de sizing que corre "
        "en produccion. Una copia paralela convierte este test en una prueba "
        "sobre el codigo del test."
    )


def test_no_sobrevive_ninguna_copia_de_kelly_en_el_capturador():
    """scripts/capture_golden_fixture.py ya no define una Kelly paralela.

    Ese modulo solo puede aportar `ah_group`, que no se puede importar de
    produccion sin arrastrar `config.database`. El sizing no tiene esa excusa.
    """
    import scripts.capture_golden_fixture as capture

    duplicadas = [
        nombre
        for nombre in dir(capture)
        if "kelly" in nombre.lower()
    ]
    assert not duplicadas, (
        "scripts/capture_golden_fixture.py volvio a declarar una Kelly propia "
        f"({duplicadas}). El sizing lo hace src.models.betting_engine."
    )


def test_produccion_pasa_esa_misma_kelly_a_decide_bets():
    """`run_prediction_pipeline()` pasa `kelly_fn=kelly_stake` importado de
    `src.models.betting_engine` -- el objeto exacto que inyecta el harness.

    La comprobacion es estatica (ast) a proposito: importar
    src/pipeline/prediction_pipeline.py exige DB_URL, sqlalchemy y el resto de
    la cadena de modelos, y este harness tiene que correr sin base ni claves
    (es parte del gate de CI y de la auditoria semanal sin secretos). Leer el
    wiring del arbol sintactico da la misma garantia sin ninguna de esas
    dependencias.
    """
    import ast

    ruta = (
        Path(__file__).resolve().parent.parent
        / "src" / "pipeline" / "prediction_pipeline.py"
    )
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))

    origen = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            for alias in nodo.names:
                origen[alias.asname or alias.name] = (nodo.module, alias.name)

    assert origen.get("kelly_stake") == ("src.models.betting_engine", "kelly_stake"), (
        "prediction_pipeline.py debe importar kelly_stake de "
        f"src.models.betting_engine; encontrado: {origen.get('kelly_stake')}"
    )

    inyectadas = [
        kw.value
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and getattr(nodo.func, "id", None) == "decide_bets"
        for kw in nodo.keywords
        if kw.arg == "kelly_fn"
    ]

    assert inyectadas, (
        "produccion debe llamar decide_bets(kelly_fn=...): si dejara de "
        "hacerlo, el fixture dorado ya no mediria el sizing real."
    )
    for valor in inyectadas:
        assert isinstance(valor, ast.Name) and valor.id == "kelly_stake", (
            "produccion pasa a decide_bets una funcion de sizing que no es "
            f"betting_engine.kelly_stake ({ast.dump(valor)[:80]}). El harness "
            "dorado dejaria de probar la cadena que apuesta dinero."
        )


# ============================================================
# LA REGRESION DORADA
# ============================================================

def test_el_replay_reproduce_la_linea_base_exacta():
    """LA prueba: mismas (match, market, side, stake) que la linea base."""
    expected = _load("golden_output.json")
    actual = _replay()

    assert actual == expected, (
        "La cadena de decision produjo apuestas distintas a la linea base "
        "congelada. Alguna remediacion CAMBIO una decision de apuesta. "
        "Revisa el diff antes de mergear; NO regeneres el fixture.\n"
        f"esperado={expected}\nactual  ={actual}"
    )


def test_cada_stake_es_identico_al_congelado():
    """Comparacion campo por campo para que el diff sea legible al fallar."""
    expected = _load("golden_output.json")
    actual = _replay()

    assert len(actual) == len(expected), (
        f"cambio el numero de apuestas: {len(expected)} -> {len(actual)}"
    )

    for exp, act in zip(expected, actual):
        anchor = f"{exp['match']} / {exp['market']} / {exp['side']}"
        assert act["match"] == exp["match"], f"cambio el partido en {anchor}"
        assert act["market"] == exp["market"], f"cambio el mercado en {anchor}"
        assert act["side"] == exp["side"], f"cambio el lado en {anchor}"
        assert act["stake"] == exp["stake"], (
            f"cambio el stake en {anchor}: "
            f"{exp['stake']} -> {act['stake']}"
        )


def test_el_replay_es_determinista():
    """Dos corridas seguidas dan exactamente lo mismo.

    `decide_bets` copia cada bet antes de tocarlo; sin eso la penalizacion de
    concentracion se aplicaria dos veces sobre la misma lista y la segunda
    corrida daria stakes distintos.
    """
    assert _replay() == _replay()


def test_el_replay_no_muta_el_slate_de_entrada():
    """El fixture en memoria no debe cambiar al correr la cadena."""
    import copy

    payload = _load("golden_input.json")
    before = copy.deepcopy(payload["scored"])

    _replay()

    assert payload["scored"] == before, (
        "decide_bets mutó el slate de entrada; el fixture dejaria de ser "
        "reproducible entre corridas."
    )


def test_el_harness_corre_sin_base_de_datos(monkeypatch):
    """Sin DB_URL el harness sigue funcionando.

    La auditoria semanal corre sin secretos configurados; si este harness
    exigiera una base, no podria formar parte de esa corrida ni del gate de CI.
    """
    monkeypatch.delenv("DB_URL", raising=False)

    expected = _load("golden_output.json")
    assert _replay() == expected
