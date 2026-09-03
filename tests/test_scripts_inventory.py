"""Tests del inventario de scripts (T013).

El contrato que se fija aqui es simple y duro: `docs/scripts_inventory.md`
cubre EXACTAMENTE los `.py` que viven en `scripts/`, ni uno mas ni uno menos,
y las columnas derivables coinciden con la fuente.

Ninguna asercion fija un conteo de scripts ni una lista de nombres: todas
comparan contra el listado VIVO de `scripts/`. Un test que hardcodeara "40
scripts" empezaria a mentir el dia que alguien agrega el numero 41 — que es
justo el dia en que este inventario tiene que gritar.
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


def _tabla(filas):
    """Construye un documento de inventario minimo con `filas` ya formateadas."""
    from tools.audit.checks.inventory import COLUMNAS

    lineas = [
        "# Inventario de scripts",
        "",
        "| " + " | ".join(COLUMNAS) + " |",
        "|" + "|".join(["---"] * len(COLUMNAS)) + "|",
    ]
    lineas.extend(filas)
    return "\n".join(lineas) + "\n"


# ---------------------------------------------------------------------------
# Cobertura: todo script tiene fila, toda fila tiene script
# ---------------------------------------------------------------------------

def test_todo_script_del_arbol_tiene_una_fila(repo_root):
    """Cada `.py` de `scripts/` aparece exactamente una vez en el inventario."""
    from tools.audit.checks.inventory import (
        DOC_RELPATH,
        duplicated_rows,
        iter_scripts,
        parse_inventory,
    )

    filas = parse_inventory(repo_root / DOC_RELPATH)
    en_disco = [ruta.name for ruta in iter_scripts(repo_root)]

    sin_fila = sorted(nombre for nombre in en_disco if nombre not in filas)
    assert not sin_fila, (
        f"Scripts sin fila en {DOC_RELPATH}: {sin_fila}. "
        "Un script sin dueño registrado es el que alguien corre a mano en "
        "produccion creyendo que es inofensivo."
    )
    assert not duplicated_rows(repo_root / DOC_RELPATH), (
        "Hay scripts con mas de una fila en el inventario: dos filas pueden "
        "contradecirse y el lector no sabe cual es la vigente."
    )


def test_el_inventario_no_documenta_scripts_inexistentes(repo_root):
    """Ninguna fila describe un script que ya no esta en disco."""
    from tools.audit.checks.inventory import (
        DOC_RELPATH,
        iter_scripts,
        parse_inventory,
    )

    filas = parse_inventory(repo_root / DOC_RELPATH)
    en_disco = {ruta.name for ruta in iter_scripts(repo_root)}

    fantasmas = sorted(set(filas) - en_disco)
    assert not fantasmas, (
        f"El inventario documenta scripts inexistentes: {fantasmas}. "
        "Un inventario con entradas muertas se deja de leer."
    )


# ---------------------------------------------------------------------------
# Referencias y vocabulario
# ---------------------------------------------------------------------------

def test_los_triggers_de_workflow_apuntan_a_workflows_reales(repo_root):
    """Toda fila cuyo Trigger nombra un workflow apunta a un archivo real."""
    from tools.audit.checks.inventory import (
        DOC_RELPATH,
        WORKFLOWS_RELPATH,
        parse_inventory,
    )

    filas = parse_inventory(repo_root / DOC_RELPATH)
    carpeta = repo_root / WORKFLOWS_RELPATH
    existentes = (
        {ruta.name for ruta in carpeta.iterdir() if ruta.is_file()}
        if carpeta.is_dir()
        else set()
    )

    rotos = []
    for nombre, fila in sorted(filas.items()):
        for pieza in fila.get("Trigger", "").split(","):
            citado = pieza.strip().strip("`")
            if citado.endswith((".yml", ".yaml")) and citado not in existentes:
                rotos.append(f"{nombre} -> {citado}")

    assert not rotos, (
        f"Triggers que citan workflows inexistentes en {WORKFLOWS_RELPATH}/: "
        f"{rotos}. O el script perdio su cron, o el documento cita un "
        "workflow renombrado."
    )


def test_las_columnas_curadas_usan_el_vocabulario_declarado(repo_root):
    """Idempotent y Owner loop se mantienen dentro del vocabulario cerrado."""
    from tools.audit.checks.inventory import (
        DOC_RELPATH,
        IDEMPOTENCIAS,
        OWNER_LOOPS,
        parse_inventory,
    )

    filas = parse_inventory(repo_root / DOC_RELPATH)
    assert filas, (
        f"{DOC_RELPATH} no contiene ninguna fila legible: revisa que el "
        "encabezado de la tabla no haya cambiado de columnas."
    )

    for nombre, fila in sorted(filas.items()):
        idempotente = fila.get("Idempotent", "").strip().lower()
        assert idempotente in IDEMPOTENCIAS, (
            f"`{nombre}` declara Idempotent='{idempotente}'; los valores "
            f"validos son {IDEMPOTENCIAS} ('unknown' es honesto y aceptable)."
        )
        loop = fila.get("Owner loop", "").strip().lower()
        assert loop in OWNER_LOOPS, (
            f"`{nombre}` declara Owner loop='{loop}'; los validos son "
            f"{OWNER_LOOPS}."
        )


def test_los_one_shot_aplicados_dicen_si_re_ejecutar_es_seguro(repo_root):
    """Toda fila `one-shot-applied` trae notas no vacias."""
    from tools.audit.checks.inventory import (
        DOC_RELPATH,
        TRIGGER_ONE_SHOT,
        parse_inventory,
    )

    filas = parse_inventory(repo_root / DOC_RELPATH)
    mudos = sorted(
        nombre
        for nombre, fila in filas.items()
        if fila.get("Trigger", "").strip() == TRIGGER_ONE_SHOT
        and not fila.get("Notes", "").strip()
    )
    assert not mudos, (
        f"One-shots ya aplicados sin notas: {mudos}. Borrar un one-shot es "
        "inofensivo; re-ejecutarlo puede no serlo, y la nota es el unico "
        "lugar donde eso queda dicho."
    )


# ---------------------------------------------------------------------------
# `Writes DB` se deriva de la fuente, no se adivina
# ---------------------------------------------------------------------------

def test_writes_db_coincide_con_la_derivacion_de_la_fuente(repo_root):
    """La columna Writes DB es, literalmente, la salida de detect_db_writes."""
    from tools.audit.checks.inventory import (
        DOC_RELPATH,
        detect_db_writes,
        iter_scripts,
        parse_inventory,
    )

    filas = parse_inventory(repo_root / DOC_RELPATH)
    discrepancias = []

    for ruta in iter_scripts(repo_root):
        fila = filas.get(ruta.name)
        if fila is None:
            continue  # ya lo cubre el test de cobertura
        declarado = fila.get("Writes DB", "").strip().lower()
        derivado = detect_db_writes(ruta.read_text(encoding="utf-8", errors="replace"))
        if declarado != derivado:
            discrepancias.append(f"{ruta.name}: doc='{declarado}' fuente='{derivado}'")

    assert not discrepancias, (
        "Writes DB no coincide con lo que dice el codigo: "
        f"{discrepancias}. Esta columna se deriva, no se redacta."
    )


def test_una_fuente_sin_dml_se_deriva_como_no():
    """Sin INSERT/UPDATE/DELETE/to_sql, la derivacion devuelve `no`."""
    from tools.audit.checks.inventory import detect_db_writes

    solo_lectura = (
        '"""Script de solo lectura."""\n'
        "from sqlalchemy import text\n"
        "def main(conn):\n"
        '    return conn.execute(text("SELECT * FROM matches")).all()\n'
    )
    assert detect_db_writes(solo_lectura) == "no"


def test_los_comentarios_no_disparan_falsos_positivos():
    """Un comentario que menciona DML no convierte el script en escritor."""
    from tools.audit.checks.inventory import detect_db_writes

    fuente = (
        "def main():\n"
        "    # Antes haciamos DELETE FROM bets_history aqui; ya no.\n"
        "    return 1\n"
    )
    assert detect_db_writes(fuente) == "no"


def test_la_derivacion_reconoce_las_formas_de_escritura_reales():
    """INSERT INTO, UPDATE ... SET, DELETE FROM y to_sql cuentan como `yes`."""
    from tools.audit.checks.inventory import detect_db_writes

    formas = {
        "insert": 'SQL = "INSERT INTO bets_history (id) VALUES (1)"\n',
        "update": 'SQL = "UPDATE bets_history SET result = \'win\'"\n',
        "delete": 'SQL = "DELETE FROM upcoming_matches WHERE id = 1"\n',
        "to_sql": "df.to_sql('matches', engine, if_exists='append')\n",
        "ddl": 'SQL = "CREATE TABLE bankroll (id INT)"\n',
    }
    for etiqueta, fuente in formas.items():
        assert detect_db_writes(fuente) == "yes", f"No detecto la forma: {etiqueta}"


# ---------------------------------------------------------------------------
# El checker
# ---------------------------------------------------------------------------

def test_el_checker_esta_registrado_bajo_inventory():
    """`inventory` vive en el registro con la categoria correcta."""
    from tools.audit.checks import load_all
    from tools.audit.model import Category
    from tools.audit.registry import get_check

    load_all()
    spec = get_check("inventory")
    assert spec is not None, (
        "El checker 'inventory' no quedo registrado: un checker escrito pero "
        "nunca registrado deja su categoria en cero hallazgos y cero "
        "ubicaciones, indistinguible de una categoria limpia."
    )
    assert spec.category == Category.CONVENTION_VIOLATION


def test_el_checker_no_reporta_nada_sobre_el_repositorio_real(repo_root):
    """Sobre este arbol el inventario esta completo y sin contradicciones."""
    from tools.audit.checks.inventory import check_script_inventory

    hallazgos, inspeccionadas = check_script_inventory(repo_root)
    assert not hallazgos, [h.impact for h in hallazgos]
    assert inspeccionadas > 0, (
        "El checker reporto cero ubicaciones inspeccionadas: eso significa "
        "que no miro nada, no que todo este bien."
    )


def test_el_checker_delata_un_script_sin_fila(tmp_path):
    """Un script nuevo sin fila en el inventario produce un hallazgo S2."""
    from tools.audit.checks.inventory import check_script_inventory
    from tools.audit.model import Severity

    raiz = _arbol(
        tmp_path,
        {
            "scripts/nuevo_sin_dueno.py": "print('hola')\n",
            "docs/scripts_inventory.md": _tabla([]),
        },
    )

    hallazgos, inspeccionadas = check_script_inventory(raiz)
    assert len(hallazgos) == 1
    assert hallazgos[0].severity == Severity.S2
    assert "nuevo_sin_dueno.py" in hallazgos[0].impact
    assert inspeccionadas == 1


def test_el_checker_delata_una_fila_fantasma(tmp_path):
    """Una fila que describe un script inexistente produce un hallazgo."""
    from tools.audit.checks.inventory import check_script_inventory

    raiz = _arbol(
        tmp_path,
        {
            "docs/scripts_inventory.md": _tabla(
                ["| `borrado.py` | manual | yes | no | manual | Nota. |"]
            ),
        },
    )

    hallazgos, _ = check_script_inventory(raiz)
    assert len(hallazgos) == 1
    assert "borrado.py" in hallazgos[0].impact


def test_el_checker_delata_un_trigger_hacia_un_workflow_inexistente(tmp_path):
    """Citar un workflow que no existe es un hallazgo, no un detalle."""
    from tools.audit.checks.inventory import check_script_inventory

    raiz = _arbol(
        tmp_path,
        {
            "scripts/algo.py": "print('hola')\n",
            "docs/scripts_inventory.md": _tabla(
                ["| `algo.py` | fantasma.yml | yes | no | daily-pipeline | Nota. |"]
            ),
        },
    )

    hallazgos, _ = check_script_inventory(raiz)
    impactos = " ".join(h.impact for h in hallazgos)
    assert "fantasma.yml" in impactos


def test_el_checker_delata_un_writes_db_que_contradice_la_fuente(tmp_path):
    """Declarar `no` en un script que hace INSERT INTO es un hallazgo S2."""
    from tools.audit.checks.inventory import check_script_inventory
    from tools.audit.model import Severity

    raiz = _arbol(
        tmp_path,
        {
            "scripts/escribe.py": 'SQL = "INSERT INTO bets_history (id) VALUES (1)"\n',
            "docs/scripts_inventory.md": _tabla(
                ["| `escribe.py` | manual | no | no | manual | Nota. |"]
            ),
        },
    )

    hallazgos, _ = check_script_inventory(raiz)
    assert len(hallazgos) == 1
    assert hallazgos[0].severity == Severity.S2
    assert "INSERT INTO" in hallazgos[0].impact


def test_el_checker_delata_un_one_shot_sin_notas(tmp_path):
    """Un one-shot aplicado con notas vacias no puede pasar en silencio."""
    from tools.audit.checks.inventory import (
        TRIGGER_ONE_SHOT,
        check_script_inventory,
    )

    raiz = _arbol(
        tmp_path,
        {
            "scripts/db_cleanup_full.py": "print('ya corrio')\n",
            "docs/scripts_inventory.md": _tabla(
                [f"| `db_cleanup_full.py` | {TRIGGER_ONE_SHOT} | no | no | one-shot |  |"]
            ),
        },
    )

    hallazgos, _ = check_script_inventory(raiz)
    impactos = " ".join(h.impact for h in hallazgos)
    assert "db_cleanup_full.py" in impactos
    assert "notas" in impactos


# ---------------------------------------------------------------------------
# Derivacion del trigger y regeneracion
# ---------------------------------------------------------------------------

def test_el_trigger_se_deriva_del_workflow_que_invoca_el_script(tmp_path):
    """Un script nombrado por un workflow no queda marcado como manual."""
    from tools.audit.checks.inventory import detect_trigger

    raiz = _arbol(
        tmp_path,
        {
            "scripts/orquestado.py": "print('hola')\n",
            ".github/workflows/morning.yml": (
                "name: morning\njobs:\n  run:\n    steps:\n"
                "      - run: python scripts/orquestado.py --mode morning\n"
            ),
        },
    )

    assert detect_trigger(raiz, "orquestado.py") == "morning.yml"
    assert detect_trigger(raiz, "nunca_invocado.py") == "manual"


def test_regenerar_preserva_las_columnas_curadas(tmp_path):
    """Regenerar no destruye Idempotent / Owner loop / Notes escritos a mano."""
    from tools.audit.checks.inventory import parse_inventory, render_inventory

    raiz = _arbol(
        tmp_path,
        {
            "scripts/algo.py": "print('hola')\n",
            "docs/scripts_inventory.md": _tabla(
                ["| `algo.py` | manual | no | no | manual | Nota curada a mano. |"]
            ),
        },
    )

    destino = raiz / "docs" / "scripts_inventory.md"
    destino.write_text(render_inventory(raiz), encoding="utf-8")

    fila = parse_inventory(destino)["algo.py"]
    assert fila["Notes"] == "Nota curada a mano."
    assert fila["Idempotent"] == "no"
