"""Tests del checker de deriva de esquema (T012).

El DDL de este proyecto esta partido entre creaciones explicitas, migraciones
perezosas y creaciones implicitas por `to_sql`. Estos tests fijan el contrato
de las tres deteccciones y, sobre todo, la regla que protege las ~79K filas
historicas: una alteracion no aditiva jamas sale como aplicable en automatico.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _arbol(raiz, archivos):
    """Materializa un arbol sintetico {ruta relativa: contenido}."""
    for relativa, contenido in archivos.items():
        ruta = raiz / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(contenido, encoding="utf-8")
    return raiz


_CREA_BANKROLL = '''"""Modulo que declara la tabla de bankroll."""

DDL = """
CREATE TABLE IF NOT EXISTS bankroll (
    id SERIAL PRIMARY KEY,
    balance NUMERIC
);
"""
'''

_ALTERA_BANKROLL = '''"""Migracion perezosa desde otro modulo distinto."""

MIGRACION = "ALTER TABLE bankroll ADD COLUMN IF NOT EXISTS moneda TEXT;"
'''

_TO_SQL_MATCHES = '''"""Carga historica: crea `matches` sin ningun DDL escrito."""

import pandas as pd


def cargar(df, engine):
    df.to_sql("matches", engine, if_exists="append", index=False)
'''

_LEE_MATCHES = '''"""Consumidor de la tabla historica."""

CONSULTA = "SELECT home_team, away_team FROM matches WHERE season = 2025"
'''


def _hallazgos_de(raiz):
    """Corre el checker y devuelve solo la lista de hallazgos."""
    from tools.audit.checks.schema import check_schema

    hallazgos, _ = check_schema(raiz)
    return hallazgos


def _textos(hallazgos):
    """Impacto + remediacion concatenados, para aserciones de contenido."""
    return "\n".join(f"{h.impact}\n{h.remediation}" for h in hallazgos)


# ---------------------------------------------------------------------------
# Inventario de sitios DDL
# ---------------------------------------------------------------------------

def test_table_inventory_incluye_create_alter_y_to_sql(tmp_path):
    """Las tres formas de DDL entran al mismo inventario."""
    from tools.audit.checks.schema import table_inventory

    raiz = _arbol(
        tmp_path,
        {
            "src/models/store.py": _CREA_BANKROLL,
            "scripts/migrate.py": _ALTERA_BANKROLL,
            "scripts/load_historical.py": _TO_SQL_MATCHES,
        },
    )

    inventario = table_inventory(raiz)

    assert set(inventario) == {"bankroll", "matches"}
    tipos_bankroll = {s.kind for s in inventario["bankroll"]}
    assert tipos_bankroll == {"create", "alter"}
    assert [s.kind for s in inventario["matches"]] == ["to_sql"]


def test_table_inventory_normaliza_el_esquema_calificado(tmp_path):
    """`public.bets_history` y `bets_history` son la misma tabla."""
    from tools.audit.checks.schema import table_inventory

    raiz = _arbol(
        tmp_path,
        {
            "a.sql": "CREATE TABLE public.bets_history (id INT);",
            "b.sql": "ALTER TABLE bets_history ADD COLUMN clv NUMERIC;",
        },
    )

    inventario = table_inventory(raiz)

    assert "bets_history" in inventario
    assert "public.bets_history" not in inventario
    assert len(inventario["bets_history"]) == 2


def test_table_inventory_ordena_los_sitios_de_forma_estable(tmp_path):
    """Dos corridas sobre el mismo arbol devuelven el mismo orden exacto."""
    from tools.audit.checks.schema import table_inventory

    raiz = _arbol(
        tmp_path,
        {
            "z/tarde.sql": "ALTER TABLE bankroll ADD COLUMN a INT;",
            "a/temprano.sql": "CREATE TABLE bankroll (id INT);",
            "m/medio.sql": "ALTER TABLE bankroll ADD COLUMN b INT;",
        },
    )

    primera = table_inventory(raiz)
    segunda = table_inventory(raiz)

    rutas = [s.path for s in primera["bankroll"]]
    assert rutas == sorted(rutas)
    assert rutas == [s.path for s in segunda["bankroll"]]


# ---------------------------------------------------------------------------
# Columnas definidas en mas de un modulo
# ---------------------------------------------------------------------------

def test_columnas_definidas_en_varios_modulos_generan_hallazgo(tmp_path):
    """Una tabla con columnas declaradas en dos modulos se reporta."""
    raiz = _arbol(
        tmp_path,
        {
            "src/models/store.py": _CREA_BANKROLL,
            "scripts/migrate.py": _ALTERA_BANKROLL,
        },
    )

    hallazgos = _hallazgos_de(raiz)
    dispersion = [h for h in hallazgos if "se definen desde" in h.impact]

    assert len(dispersion) == 1
    anclas = dispersion[0].anchors
    assert any(a.startswith("src/models/store.py:") for a in anclas)
    assert any(a.startswith("scripts/migrate.py:") for a in anclas)


def test_el_hallazgo_de_dispersion_lista_cada_sitio(tmp_path):
    """La evidencia trae una entrada por sitio, no solo por modulo."""
    raiz = _arbol(
        tmp_path,
        {
            "a.sql": "CREATE TABLE bankroll (id INT);",
            "b.sql": (
                "ALTER TABLE bankroll ADD COLUMN uno INT;\n"
                "ALTER TABLE bankroll ADD COLUMN dos INT;\n"
            ),
        },
    )

    dispersion = [
        h for h in _hallazgos_de(raiz) if "se definen desde" in h.impact
    ]

    assert len(dispersion) == 1
    assert len(dispersion[0].evidence) == 3


def test_tabla_definida_en_un_solo_modulo_no_genera_dispersion(tmp_path):
    """Un unico modulo dueno del esquema es exactamente lo que se busca."""
    raiz = _arbol(
        tmp_path,
        {
            "src/models/store.py": (
                "DDL = \"CREATE TABLE bankroll (id INT);\"\n"
                "MIG = \"ALTER TABLE bankroll ADD COLUMN moneda TEXT;\"\n"
            ),
        },
    )

    assert [h for h in _hallazgos_de(raiz) if "se definen desde" in h.impact] == []


def test_add_constraint_en_otro_modulo_no_cuenta_como_columna(tmp_path):
    """Una constraint no define columnas: no parte la propiedad del esquema."""
    raiz = _arbol(
        tmp_path,
        {
            "a.sql": "CREATE TABLE bankroll (id INT);",
            "b.sql": "ALTER TABLE bankroll ADD CONSTRAINT pk_bankroll PRIMARY KEY (id);",
        },
    )

    assert [h for h in _hallazgos_de(raiz) if "se definen desde" in h.impact] == []


# ---------------------------------------------------------------------------
# Tablas sin creacion explicita
# ---------------------------------------------------------------------------

def test_tabla_creada_solo_por_to_sql_se_reporta(tmp_path):
    """`matches` nace de un to_sql y se lee por SQL: falta su declaracion."""
    raiz = _arbol(
        tmp_path,
        {
            "scripts/load_historical.py": _TO_SQL_MATCHES,
            "src/pipeline/reader.py": _LEE_MATCHES,
        },
    )

    hallazgos = _hallazgos_de(raiz)
    sin_create = [h for h in hallazgos if "creacion explicita" in h.impact]

    assert [h for h in sin_create if "`matches`" in h.impact]
    objetivo = [h for h in sin_create if "`matches`" in h.impact][0]
    assert "to_sql" in objetivo.impact
    assert any("load_historical.py" in a for a in objetivo.anchors)
    assert any("reader.py" in a for a in objetivo.anchors)


def test_tabla_con_create_explicito_no_se_reporta_como_faltante(tmp_path):
    """Si existe la declaracion, la tabla no aparece en esa deteccion."""
    raiz = _arbol(
        tmp_path,
        {
            "a.sql": "CREATE TABLE bankroll (id INT);",
            "b.py": "Q = \"SELECT balance FROM bankroll\"\n",
        },
    )

    sin_create = [
        h for h in _hallazgos_de(raiz) if "creacion explicita" in h.impact
    ]

    assert not any("`bankroll`" in h.impact for h in sin_create)


def test_tabla_solo_referenciada_se_marca_como_incierta(tmp_path):
    """Sin ningun sitio DDL el nombre pudo ser una vista: se marca uncertain."""
    raiz = _arbol(
        tmp_path,
        {"src/pipeline/reader.py": "Q = \"SELECT * FROM vista_rara\"\n"},
    )

    objetivo = [
        h for h in _hallazgos_de(raiz) if "`vista_rara`" in h.impact
    ]

    assert len(objetivo) == 1
    assert objetivo[0].uncertain is True


def test_import_de_python_no_se_confunde_con_un_from_de_sql(tmp_path):
    """`from datetime import datetime` no inventa una tabla llamada datetime."""
    from tools.audit.checks.schema import referenced_tables

    raiz = _arbol(
        tmp_path,
        {
            "src/utils/helper.py": (
                "from datetime import datetime\n"
                "from pathlib import Path\n"
                "import os\n"
            ),
        },
    )

    referencias = referenced_tables(raiz)

    assert "datetime" not in referencias
    assert "pathlib" not in referencias
    assert referencias == {}


def test_referenced_tables_detecta_lecturas_y_escrituras(tmp_path):
    """FROM, JOIN, INSERT INTO y UPDATE cuentan todos como uso de la tabla."""
    from tools.audit.checks.schema import referenced_tables

    raiz = _arbol(
        tmp_path,
        {
            "q.py": (
                "A = \"SELECT * FROM matches\"\n"
                "B = \"SELECT * FROM bets_history b JOIN upcoming_matches u ON 1=1\"\n"
                "C = \"INSERT INTO bets_history (id) VALUES (1)\"\n"
                "D = \"UPDATE bankroll SET balance = 1\"\n"
            ),
        },
    )

    referencias = referenced_tables(raiz)

    assert {"matches", "bets_history", "upcoming_matches", "bankroll"} <= set(
        referencias
    )


# ---------------------------------------------------------------------------
# Cambios no aditivos
# ---------------------------------------------------------------------------

def test_add_column_es_aditivo_y_aplicable(tmp_path):
    """ADD COLUMN IF NOT EXISTS es seguro contra datos historicos."""
    from tools.audit.checks.schema import scan_schema_changes

    raiz = _arbol(
        tmp_path,
        {"m.sql": "ALTER TABLE bets_history ADD COLUMN IF NOT EXISTS clv NUMERIC;"},
    )

    cambios = scan_schema_changes(raiz)

    assert [c.operation for c in cambios] == ["add_column"]
    assert cambios[0].additive is True
    assert cambios[0].auto_applicable is True
    assert cambios[0].approval == "auto"


def test_rename_type_change_y_not_null_nunca_son_auto_aplicables(tmp_path):
    """Las tres alteraciones destructivas exigen aprobacion explicita."""
    from tools.audit.checks.schema import (
        NON_ADDITIVE_OPERATIONS,
        scan_schema_changes,
    )

    raiz = _arbol(
        tmp_path,
        {
            "m.sql": (
                "ALTER TABLE bets_history RENAME COLUMN stake TO stake_units;\n"
                "ALTER TABLE bets_history ALTER COLUMN odds TYPE NUMERIC;\n"
                "ALTER TABLE bets_history ALTER COLUMN stake SET NOT NULL;\n"
                "ALTER TABLE bets_history DROP COLUMN legacy;\n"
            ),
        },
    )

    cambios = scan_schema_changes(raiz)
    operaciones = {c.operation for c in cambios}

    assert {"rename", "type_change", "not_null", "drop_column"} <= operaciones
    for cambio in cambios:
        if cambio.operation in NON_ADDITIVE_OPERATIONS:
            assert cambio.additive is False
            assert cambio.auto_applicable is False
            assert cambio.approval == "explicit-approval"


def test_classify_alteration_es_deterministico_y_ordenado():
    """La clasificacion devuelve operaciones ordenadas alfabeticamente."""
    from tools.audit.checks.schema import classify_alteration

    ops = classify_alteration(
        "ALTER TABLE t RENAME COLUMN a TO b; ALTER TABLE t DROP COLUMN c;"
    )

    assert ops == tuple(sorted(ops))
    assert "rename" in ops and "drop_column" in ops


def test_cambio_no_aditivo_genera_hallazgo_que_pide_aprobacion(tmp_path):
    """El hallazgo dice explicitamente que no se aplica en automatico."""
    raiz = _arbol(
        tmp_path,
        {"m.sql": "ALTER TABLE bets_history RENAME COLUMN stake TO stake_units;"},
    )

    no_aditivos = [
        h for h in _hallazgos_de(raiz) if "no aditiva" in h.impact
    ]

    assert len(no_aditivos) == 1
    assert "aprobacion explicita" in no_aditivos[0].remediation
    assert "nunca se propone como cambio automatico" in no_aditivos[0].remediation


def test_rename_y_drop_son_s1_y_el_resto_s2(tmp_path):
    """Perder o renombrar una columna rompe lectores: es S1."""
    from tools.audit.model import Severity

    raiz = _arbol(
        tmp_path,
        {
            "a.sql": "ALTER TABLE bets_history DROP COLUMN legacy;",
            "b.sql": "ALTER TABLE bets_history ALTER COLUMN odds TYPE NUMERIC;",
        },
    )

    por_operacion = {
        h.evidence[0].key: h.severity
        for h in _hallazgos_de(raiz)
        if "no aditiva" in h.impact
    }

    assert por_operacion["bets_history#drop_column"] is Severity.S1
    assert por_operacion["bets_history#type_change"] is Severity.S2


def test_ningun_hallazgo_del_checker_se_propone_como_auto_aplicable(tmp_path):
    """Ni un solo hallazgo de esquema sugiere aplicar el cambio solo."""
    raiz = _arbol(
        tmp_path,
        {
            "a.sql": "ALTER TABLE bets_history ALTER COLUMN stake SET NOT NULL;",
            "b.py": _TO_SQL_MATCHES,
        },
    )

    for hallazgo in _hallazgos_de(raiz):
        assert hallazgo.status == "open"
        assert "auto-aplicar" not in hallazgo.remediation.lower()


# ---------------------------------------------------------------------------
# Contrato del checker
# ---------------------------------------------------------------------------

def test_check_schema_esta_registrado_en_la_categoria_correcta():
    """El checker se enchufa al registro bajo la categoria schema-drift."""
    import tools.audit.checks.schema  # noqa: F401  (registra el checker)
    from tools.audit.model import Category
    from tools.audit.registry import get_check

    spec = get_check("schema-drift")

    assert spec is not None
    assert spec.category is Category.SCHEMA_DRIFT


def test_check_schema_respeta_el_contrato_del_registro(tmp_path):
    """Devuelve (list[Finding], int) y el registro lo acepta sin quejarse."""
    from tools.audit.registry import get_check

    import tools.audit.checks.schema  # noqa: F401

    raiz = _arbol(tmp_path, {"a.sql": "CREATE TABLE bankroll (id INT);"})
    hallazgos, inspeccionadas = get_check("schema-drift").run(raiz)

    assert isinstance(hallazgos, list)
    assert isinstance(inspeccionadas, int)
    assert inspeccionadas > 0


def test_check_schema_cuenta_ubicaciones_aunque_no_haya_hallazgos(tmp_path):
    """0 hallazgos con N ubicaciones != el checker no corrio (FR-016)."""
    from tools.audit.checks.schema import check_schema

    raiz = _arbol(
        tmp_path, {"src/utils/puro.py": "def suma(a, b):\n    return a + b\n"}
    )

    hallazgos, inspeccionadas = check_schema(raiz)

    assert hallazgos == []
    assert inspeccionadas > 0


def test_check_schema_es_deterministico(tmp_path):
    """Dos corridas sobre el mismo arbol dan los mismos IDs en el mismo orden."""
    from tools.audit.checks.schema import check_schema

    raiz = _arbol(
        tmp_path,
        {
            "src/models/store.py": _CREA_BANKROLL,
            "scripts/migrate.py": _ALTERA_BANKROLL,
            "scripts/load_historical.py": _TO_SQL_MATCHES,
            "src/pipeline/reader.py": _LEE_MATCHES,
            "m.sql": "ALTER TABLE bets_history DROP COLUMN legacy;",
        },
    )

    primera, n1 = check_schema(raiz)
    segunda, n2 = check_schema(raiz)

    assert n1 == n2
    assert [h.id for h in primera] == [h.id for h in segunda]
    assert [h.anchors for h in primera] == [h.anchors for h in segunda]


def test_todo_hallazgo_trae_ancla_impacto_y_remediacion(tmp_path):
    """Un hallazgo sin ancla o sin arreglo propuesto no es accionable."""
    raiz = _arbol(
        tmp_path,
        {
            "src/models/store.py": _CREA_BANKROLL,
            "scripts/migrate.py": _ALTERA_BANKROLL,
            "scripts/load_historical.py": _TO_SQL_MATCHES,
            "m.sql": "ALTER TABLE bets_history RENAME COLUMN a TO b;",
        },
    )

    hallazgos = _hallazgos_de(raiz)

    assert hallazgos
    for hallazgo in hallazgos:
        assert hallazgo.anchors
        assert hallazgo.impact.strip()
        assert hallazgo.remediation.strip()
        assert hallazgo.category.value == "schema-drift"


def test_los_ids_de_hallazgo_son_unicos(tmp_path):
    """Dos defectos distintos no pueden colapsar en el mismo ID."""
    raiz = _arbol(
        tmp_path,
        {
            "src/models/store.py": _CREA_BANKROLL,
            "scripts/migrate.py": _ALTERA_BANKROLL,
            "scripts/load_historical.py": _TO_SQL_MATCHES,
            "src/pipeline/reader.py": _LEE_MATCHES,
            "m.sql": "ALTER TABLE bets_history DROP COLUMN legacy;",
        },
    )

    ids = [h.id for h in _hallazgos_de(raiz)]

    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Sin base de datos
# ---------------------------------------------------------------------------

def test_el_checker_no_abre_ninguna_conexion():
    """El fuente no menciona ningun driver ni motor de conexion."""
    import re

    fuente = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "audit"
        / "checks"
        / "schema.py"
    ).read_text(encoding="utf-8")

    prohibido = re.compile(
        "create" + "_engine|psycopg" + "2|sql" + "alchemy", re.IGNORECASE
    )

    assert prohibido.search(fuente) is None


def test_el_checker_corre_sobre_el_repo_real_sin_secretos(repo_root):
    """Sobre el arbol real termina y reporta ubicaciones inspeccionadas."""
    from tools.audit.checks.schema import check_schema

    hallazgos, inspeccionadas = check_schema(repo_root)

    assert isinstance(hallazgos, list)
    assert inspeccionadas > 0
