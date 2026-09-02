"""Tests del registro de checkers y de los escritores de reporte (T005)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(categoria="config-inconsistency", severidad="S1", ruta="a.py",
             linea=10, cita="", impacto="impacto", arreglo="arreglo"):
    """Construye un hallazgo minimo pero valido."""
    from tools.audit.model import Evidence, Finding

    return Finding(
        category=categoria,
        severity=severidad,
        evidence=[Evidence(path=ruta, line=linea, quote=cita)],
        impact=impacto,
        remediation=arreglo,
    )


def _run(hallazgos=None, inspeccionadas=None):
    """Construye una corrida con metadatos fijos (nada de datetime.now())."""
    from tools.audit.model import AuditRun

    return AuditRun(
        run_id="run-0001",
        timestamp="2026-01-05T00:00:00+00:00",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        findings=list(hallazgos or []),
        inspected=dict(inspeccionadas or {}),
    )


def _checker_vacio(root):
    """Checker de prueba: no encuentra nada pero inspecciona 7 ubicaciones."""
    return [], 7


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

def test_all_checks_ordena_por_nombre_sin_importar_el_orden_de_registro():
    """all_checks() es alfabetico aunque los checkers se registren al reves."""
    from tools.audit.model import Category
    from tools.audit.registry import all_checks, check, isolated_registry

    with isolated_registry():
        for nombre in ("zeta", "media", "alfa"):
            check(nombre, Category.DOC_DRIFT)(_checker_vacio)

        assert [spec.name for spec in all_checks()] == ["alfa", "media", "zeta"]


def test_all_checks_es_la_unica_fuente_y_no_ve_checkers_sin_registrar():
    """Un checker escrito pero nunca decorado no aparece en all_checks()."""
    from tools.audit.model import Category
    from tools.audit.registry import (
        all_checks,
        check,
        get_check,
        isolated_registry,
    )

    def checker_huerfano(root):  # nunca se decora
        return [], 999

    with isolated_registry():
        check("registrado", Category.ORPHAN_CODE)(_checker_vacio)

        nombres = [spec.name for spec in all_checks()]
        assert nombres == ["registrado"]
        assert get_check("checker_huerfano") is None
        assert checker_huerfano(Path(".")) == ([], 999)  # existe, pero invisible


def test_registrar_dos_veces_el_mismo_nombre_revienta():
    """El nombre es la clave estable del registro: colisionar es un error duro."""
    import pytest

    from tools.audit.model import Category
    from tools.audit.registry import check, isolated_registry

    with isolated_registry():
        check("dup", Category.SCHEMA_DRIFT)(_checker_vacio)
        with pytest.raises(ValueError, match="dup"):
            check("dup", Category.SCHEMA_DRIFT)(_checker_vacio)


def test_checkspec_run_devuelve_hallazgos_y_ubicaciones_inspeccionadas():
    """run() entrega la tupla (hallazgos, inspeccionadas) del checker."""
    from tools.audit.model import Category
    from tools.audit.registry import check, get_check, isolated_registry

    def checker(root):
        return [_finding()], 42

    with isolated_registry():
        check("con-hallazgo", Category.CONFIG_INCONSISTENCY)(checker)
        hallazgos, inspeccionadas = get_check("con-hallazgo").run(Path("."))

    assert len(hallazgos) == 1
    assert inspeccionadas == 42


def test_checkspec_run_rechaza_un_checker_que_no_reporta_el_conteo():
    """Sin conteo, 'no encontre nada' y 'no mire nada' serian indistinguibles."""
    import pytest

    from tools.audit.model import Category
    from tools.audit.registry import check, get_check, isolated_registry

    def checker_malo(root):
        return [_finding()]  # lista pelada, sin conteo

    with isolated_registry():
        check("malo", Category.ERROR_HANDLING_RISK)(checker_malo)
        with pytest.raises(TypeError, match="malo"):
            get_check("malo").run(Path("."))


def test_checkspec_declara_las_categorias_secundarias_que_puede_emitir():
    """Un checker multi-categoria cubre todas las categorias que declara."""
    from tools.audit.model import Category
    from tools.audit.registry import check, get_check, isolated_registry

    with isolated_registry():
        check(
            "referencias",
            Category.BROKEN_REFERENCE,
            also=(Category.ORPHAN_CODE, Category.CIRCULAR_DEPENDENCY),
        )(_checker_vacio)

        spec = get_check("referencias")

    assert spec.categories == (
        Category.BROKEN_REFERENCE,
        Category.ORPHAN_CODE,
        Category.CIRCULAR_DEPENDENCY,
    )


def test_load_all_importa_los_modulos_del_paquete_de_checks():
    """load_all() descubre modulos sin lista central que se pueda olvidar."""
    from tools.audit.checks import load_all

    cargados = load_all()

    assert isinstance(cargados, list)
    assert cargados == sorted(cargados)
    assert not any(nombre.startswith("_") for nombre in cargados)


# ---------------------------------------------------------------------------
# Escritor markdown
# ---------------------------------------------------------------------------

def test_write_markdown_emite_una_seccion_por_cada_categoria(tmp_path):
    """Las diez categorias tienen encabezado, haya o no hallazgos."""
    from tools.audit.model import Category
    from tools.audit.report import write_markdown

    destino = tmp_path / "latest.md"
    write_markdown(_run(hallazgos=[_finding()]), destino)
    texto = destino.read_text(encoding="utf-8")

    for categoria in Category:
        assert f"### {categoria.value}" in texto


def test_categoria_sin_hallazgos_muestra_cero_y_su_conteo_inspeccionado(tmp_path):
    """'0 hallazgos, N ubicaciones' evita leer una categoria vacia como no auditada."""
    from tools.audit.report import write_markdown

    corrida = _run(
        hallazgos=[_finding(categoria="config-inconsistency")],
        inspeccionadas={"broken-reference": 812, "config-inconsistency": 44},
    )
    destino = tmp_path / "latest.md"
    write_markdown(corrida, destino)
    texto = destino.read_text(encoding="utf-8")

    # Categoria auditada y limpia: cero hallazgos pero 812 ubicaciones vistas.
    assert "0 hallazgos, 812 ubicaciones inspeccionadas." in texto
    # Categoria con hallazgo: conserva su propio conteo.
    assert "1 hallazgos, 44 ubicaciones inspeccionadas." in texto
    # Categoria sin checker: el cero doble delata al checker faltante.
    assert "0 hallazgos, 0 ubicaciones inspeccionadas." in texto


def test_write_markdown_no_filtra_el_valor_de_un_secreto(tmp_path):
    """El reporte cita la ubicacion de la credencial, jamas su valor."""
    from tools.audit.report import write_markdown

    corrida = _run(hallazgos=[
        _finding(
            categoria="secret-leakage",
            cita='ODDS_API_KEY = "a1b2c3d4e5f6g7h8i9j0"',
            impacto='la clave ODDS_API_KEY = "a1b2c3d4e5f6g7h8i9j0" quedo en el repo',
        )
    ])
    destino = tmp_path / "latest.md"
    write_markdown(corrida, destino)
    texto = destino.read_text(encoding="utf-8")

    assert "a1b2c3d4e5f6g7h8i9j0" not in texto
    # El nombre de la variable es lo que hace accionable el hallazgo.
    assert "ODDS_API_KEY" in texto


# ---------------------------------------------------------------------------
# Escritor JSON
# ---------------------------------------------------------------------------

def test_write_json_es_byte_identico_en_dos_serializaciones(tmp_path):
    """Serializar la misma corrida dos veces produce archivos identicos."""
    from tools.audit.report import write_json

    corrida = _run(
        hallazgos=[_finding(ruta="a.py"), _finding(ruta="b.py", severidad="S3")],
        inspeccionadas={"config-inconsistency": 12},
    )
    primero = tmp_path / "uno.json"
    segundo = tmp_path / "dos.json"
    write_json(corrida, primero)
    write_json(corrida, segundo)

    assert primero.read_bytes() == segundo.read_bytes()


def test_write_json_no_depende_del_orden_de_la_lista_de_hallazgos(tmp_path):
    """El orden en que los checkers aportan hallazgos no cambia el artefacto."""
    from tools.audit.report import write_json

    a = _finding(ruta="a.py", severidad="S1")
    b = _finding(ruta="b.py", severidad="S3")
    directo = tmp_path / "directo.json"
    invertido = tmp_path / "invertido.json"
    write_json(_run(hallazgos=[a, b]), directo)
    write_json(_run(hallazgos=[b, a]), invertido)

    assert directo.read_bytes() == invertido.read_bytes()


def test_write_json_ordena_llaves_y_pone_lo_mas_grave_primero(tmp_path):
    """sort_keys=True para diffs legibles; S1 encabeza la lista."""
    import json

    from tools.audit.report import write_json

    destino = tmp_path / "latest.json"
    write_json(
        _run(hallazgos=[_finding(ruta="b.py", severidad="S4"),
                        _finding(ruta="a.py", severidad="S1")]),
        destino,
    )
    datos = json.loads(destino.read_text(encoding="utf-8"))

    assert list(datos) == sorted(datos)
    assert [f["severity"] for f in datos["findings"]] == ["S1", "S4"]


def test_write_json_rellena_las_diez_categorias_en_inspected(tmp_path):
    """Un cero explicito distingue 'mire y esta limpio' de 'no corrio el checker'."""
    import json

    from tools.audit.model import Category
    from tools.audit.report import write_json

    destino = tmp_path / "latest.json"
    write_json(_run(inspeccionadas={"doc-drift": 5}), destino)
    datos = json.loads(destino.read_text(encoding="utf-8"))

    assert set(datos["inspected"]) == {c.value for c in Category}
    assert datos["inspected"]["doc-drift"] == 5
    assert datos["inspected"]["schema-drift"] == 0
    assert set(datos["counts"]["by_category"]) == {c.value for c in Category}


def test_write_json_no_filtra_el_valor_de_un_secreto(tmp_path):
    """Ningun valor de credencial llega al artefacto JSON."""
    from tools.audit.report import write_json

    corrida = _run(hallazgos=[
        _finding(
            categoria="secret-leakage",
            cita='TELEGRAM_BOT_TOKEN = "123456789:AAbbCCddEEffGGhhIIjjKKllMMnnOOppQQ"',
            arreglo="mover el valor a un secret de GitHub Actions",
        )
    ])
    destino = tmp_path / "latest.json"
    write_json(corrida, destino)
    texto = destino.read_text(encoding="utf-8")

    assert "AAbbCCddEEffGGhhIIjjKKllMMnnOOppQQ" not in texto
    assert "TELEGRAM_BOT_TOKEN" in texto


def test_los_escritores_crean_el_directorio_destino(tmp_path):
    """Ruta explicita, sin rutas absolutas fijas, y el padre se crea solo."""
    from tools.audit.report import write_json, write_markdown

    corrida = _run(hallazgos=[_finding()])
    write_json(corrida, tmp_path / "nuevo" / "latest.json")
    write_markdown(corrida, tmp_path / "otro" / "latest.md")

    assert (tmp_path / "nuevo" / "latest.json").is_file()
    assert (tmp_path / "otro" / "latest.md").is_file()


def test_los_artefactos_usan_saltos_de_linea_lf(tmp_path):
    """Sin LF explicito, Windows generaria CRLF y el diff contra CI seria ilegible."""
    from tools.audit.report import write_json, write_markdown

    corrida = _run(hallazgos=[_finding()], inspeccionadas={"doc-drift": 3})
    destino_json = tmp_path / "latest.json"
    destino_md = tmp_path / "latest.md"
    write_json(corrida, destino_json)
    write_markdown(corrida, destino_md)

    assert b"\r\n" not in destino_json.read_bytes()
    assert b"\r\n" not in destino_md.read_bytes()
