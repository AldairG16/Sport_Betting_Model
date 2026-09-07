"""Determinismo del auditor y semantica del diff corrida-a-corrida (T014).

Dos garantias que sostienen todo el valor del artefacto:

1. **Determinismo.** Dos corridas sobre el mismo arbol producen el MISMO
   `audits/latest.json`, byte a byte. Sin esto el archivo cambia en cada
   corrida, cada commit trae un diff ilegible y el operador deja de leerlo.
   Por eso `run_id` se deriva del contenido y no del reloj, y `--timestamp`
   permite fijar la unica marca que si depende de cuando se corrio.

2. **Nada desaparece en silencio.** Un hallazgo que se arregla queda marcado
   `resolved`, no borrado. "No esta" y "nunca estuvo" se leen igual, y esa
   ambiguedad impide distinguir el defecto que se cerro del checker que dejo
   de correr. El caso sutil es el ID que cambia sin que el defecto cambie
   (renombrar el archivo): se reporta como `reassigned` con un puntero al ID
   nuevo, en vez de un cierre falso mas un alta falsa.

El arbol es sintetico y vive en `tmp_path`: estos tests describen el
comportamiento del auditor, no el estado de remediacion del repo real.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import contextlib
import io
import json


TIMESTAMP_FIJO = "2026-01-05T00:00:00+00:00"
OTRO_TIMESTAMP = "2027-11-30T23:59:59+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _escribir(ruta: Path, contenido: str) -> Path:
    """Crea el archivo y sus directorios padre."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def _arbol(base: Path) -> Path:
    """Arbol sintetico chico con defectos estables entre corridas.

    Se mantiene minimo a proposito: cuantos menos checkers disparen, mas
    claro queda que una diferencia entre dos artefactos es una diferencia
    real y no ruido de un checker ajeno al test.
    """
    raiz = base / "repo"

    _escribir(
        raiz / "config" / "settings.py",
        "import os\n"
        "\n"
        "def env_int(nombre, default):\n"
        "    crudo = os.environ.get(nombre, '')\n"
        "    return int(crudo) if crudo.strip() else default\n"
        "\n"
        "INITIAL_BANKROLL = env_int('INITIAL_BANKROLL', 100)\n",
    )

    # Referencia rota: `config.database` no existe en este arbol.
    _escribir(
        raiz / "scripts" / "orchestrator.py",
        "from config.database import get_engine\n"
        "\n"
        "def main():\n"
        "    return get_engine()\n",
    )

    # Huerfano: ningun workflow ni test lo alcanza.
    _escribir(
        raiz / "src" / "betting" / "legacy_sizer.py",
        "def sizer_viejo(bankroll):\n"
        "    return bankroll * 0.01\n",
    )

    _escribir(
        raiz / ".github" / "workflows" / "morning.yml",
        "name: morning\n"
        "on:\n"
        "  schedule:\n"
        "    - cron: '0 12 * * *'\n"
        "jobs:\n"
        "  run:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: python scripts/orchestrator.py\n"
        "        env:\n"
        "          DB_URL: ${{ secrets.DB_URL }}\n",
    )

    _escribir(raiz / "requirements.txt", "PyYAML==6.0.2\n")
    return raiz


def _correr(raiz: Path, destino: Path, timestamp: str = TIMESTAMP_FIJO) -> str:
    """Ejecuta el CLI en proceso y devuelve lo que imprimio en stdout."""
    from tools.audit.__main__ import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        codigo = main(
            [
                "--root", str(raiz),
                "--out", str(destino),
                "--timestamp", timestamp,
            ]
        )
    assert codigo == 0, f"el CLI salio con {codigo}"
    return buffer.getvalue()


def _corrida(raiz: Path, timestamp: str = TIMESTAMP_FIJO):
    """Construye un `AuditRun` completo sin escribir artefactos."""
    from tools.audit.__main__ import build_run
    from tools.audit.checks import load_all

    load_all()
    return build_run(raiz, timestamp=timestamp)


def _hallazgo(categoria, severidad, ruta: str, clave: str, cita: str = ""):
    """Hallazgo sintetico minimo, util para ejercitar el diff sin checkers."""
    from tools.audit.model import Evidence, Finding

    return Finding(
        category=categoria,
        severity=severidad,
        evidence=[Evidence(path=ruta, key=clave, quote=cita)],
        impact=f"impacto de {clave}",
        remediation=f"arreglar {clave}",
    )


def _run_sintetica(findings):
    """`AuditRun` armado a mano alrededor de una lista de hallazgos."""
    from tools.audit.model import AuditRun

    return AuditRun(
        run_id="run-sintetica",
        timestamp=TIMESTAMP_FIJO,
        commit_sha="deadbeef",
        findings=list(findings),
        inspected={},
    )


# ---------------------------------------------------------------------------
# 1. Determinismo del artefacto
# ---------------------------------------------------------------------------

def test_dos_corridas_seguidas_producen_el_mismo_json_byte_a_byte(tmp_path):
    """Sobre un arbol sin cambios el artefacto no se mueve ni un byte.

    El destino vive DENTRO de la raiz auditada (`raiz/audits`), igual que el
    default de produccion. Con la salida colgada afuera el test pasaba sin
    ejercitar el caso que de verdad importa: la segunda corrida recorre un
    arbol que YA contiene el reporte de la primera. Si ese reporte se lee
    como codigo fuente, la segunda corrida encuentra hallazgos que solo
    existen porque hubo una auditoria antes, y el artefacto cambia sin que
    el repo haya cambiado.
    """
    raiz = _arbol(tmp_path)
    destino = raiz / "audits"

    _correr(raiz, destino)
    primera = (destino / "latest.json").read_bytes()

    # La segunda corrida ademas lee el artefacto de la primera como linea
    # base del diff: el camino con `previous != None` tambien tiene que ser
    # estable, no solo la primera corrida en frio.
    _correr(raiz, destino)
    segunda = (destino / "latest.json").read_bytes()

    assert primera == segunda, "audits/latest.json cambio sin cambiar el arbol"


def test_dos_corridas_seguidas_producen_el_mismo_markdown(tmp_path):
    """El reporte legible tampoco puede bailar entre corridas.

    Tambien con el destino dentro de la raiz. `latest.md` es el mas
    peligroso de los dos artefactos para la realimentacion: cita rutas,
    nombres de variables y fragmentos de codigo en prosa, asi que es el que
    mas se parece a codigo fuente cuando la corrida siguiente lo recorre.
    """
    raiz = _arbol(tmp_path)
    destino = raiz / "audits"

    _correr(raiz, destino)
    primero = (destino / "latest.md").read_bytes()
    _correr(raiz, destino)
    segundo = (destino / "latest.md").read_bytes()

    assert primero == segundo


def test_el_run_id_sale_del_contenido_y_no_del_reloj(tmp_path):
    """Dos corridas con timestamps distintos comparten `run_id`.

    Es la propiedad que hace comparable el artefacto: si el ID dependiera de
    la hora, cada corrida se veria distinta aunque el arbol fuera identico.
    """
    raiz = _arbol(tmp_path)

    temprana = _corrida(raiz, TIMESTAMP_FIJO)
    tardia = _corrida(raiz, OTRO_TIMESTAMP)

    assert temprana.run_id == tardia.run_id
    assert temprana.timestamp != tardia.timestamp


def test_el_orden_de_los_hallazgos_no_depende_del_orden_de_llegada(tmp_path):
    """`rank` es total: barajar la entrada no cambia la salida.

    Sin el tercer componente (`id`) de la clave de orden, dos hallazgos con
    la misma severidad y categoria quedarian en el orden en que los
    devolvio el checker, y eso no es reproducible.
    """
    from tools.audit.rank import rank

    corrida = _corrida(_arbol(tmp_path))
    assert corrida.findings, "el arbol sintetico debe producir hallazgos"

    original = [f.id for f in rank(corrida.findings)]
    invertido = [f.id for f in rank(list(reversed(corrida.findings)))]

    assert original == invertido


def test_los_ids_de_los_hallazgos_se_repiten_entre_corridas(tmp_path):
    """Un ID que cambia solo por volver a correr rompe todo el diff."""
    raiz = _arbol(tmp_path)

    primera = {f.id for f in _corrida(raiz).findings}
    segunda = {f.id for f in _corrida(raiz).findings}

    assert primera == segunda


# ---------------------------------------------------------------------------
# 2. El diff no deja desaparecer nada
# ---------------------------------------------------------------------------

def test_el_hallazgo_arreglado_queda_marcado_resuelto_no_borrado(tmp_path):
    """Un defecto cerrado sigue en el artefacto con `status='resolved'`."""
    raiz = _arbol(tmp_path)
    destino = tmp_path / "audits"

    _correr(raiz, destino)
    antes = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    huerfanos = [
        h for h in antes["findings"] if h["category"] == "orphan-code"
    ]
    assert huerfanos, "el arbol sintetico debe reportar el modulo huerfano"
    objetivo = next(
        h for h in huerfanos
        if any("legacy_sizer" in ev["path"] for ev in h["evidence"])
    )

    # Se "remedia": el modulo huerfano se elimina del arbol.
    (raiz / "src" / "betting" / "legacy_sizer.py").unlink()
    _correr(raiz, destino)

    despues = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    por_id = {h["id"]: h for h in despues["findings"]}

    assert objetivo["id"] in por_id, (
        "el hallazgo arreglado desaparecio en vez de marcarse resuelto"
    )
    assert por_id[objetivo["id"]]["status"] == "resolved"


def test_diff_runs_marca_resuelto_lo_que_ya_no_aparece():
    """Contrato directo de `diff_runs`, sin pasar por el sistema de archivos."""
    from tools.audit.diff import diff_runs
    from tools.audit.model import Category, Severity

    vigente = _hallazgo(
        Category.DOC_DRIFT, Severity.S2, "README.md", "SIGUE_ROTO"
    )
    arreglado = _hallazgo(
        Category.DOC_DRIFT, Severity.S2, "README.md", "YA_ARREGLADO"
    )

    previa = _run_sintetica([vigente, arreglado])
    actual = _run_sintetica([vigente])

    publicada = diff_runs(previa, actual)
    estados = {f.id: f.status for f in publicada.findings}

    assert estados[vigente.id] == "open"
    assert estados[arreglado.id] == "resolved"


def test_diff_runs_no_muta_la_corrida_previa():
    """Marcar resuelto produce una copia; el llamador conserva su objeto."""
    from tools.audit.diff import diff_runs
    from tools.audit.model import Category, Severity

    arreglado = _hallazgo(
        Category.DOC_DRIFT, Severity.S2, "README.md", "YA_ARREGLADO"
    )
    previa = _run_sintetica([arreglado])

    diff_runs(previa, _run_sintetica([]))

    assert previa.findings[0].status == "open"


def test_diff_runs_sin_corrida_previa_devuelve_la_actual_intacta():
    """La PRIMERA corrida no tiene linea base y no inventa resueltos."""
    from tools.audit.diff import diff_runs
    from tools.audit.model import Category, Severity

    abierto = _hallazgo(
        Category.DOC_DRIFT, Severity.S2, "README.md", "NUEVO"
    )
    publicada = diff_runs(None, _run_sintetica([abierto]))

    assert [f.id for f in publicada.findings] == [abierto.id]
    assert publicada.findings[0].status == "open"


def test_un_resuelto_no_se_arrastra_corrida_tras_corrida():
    """El acuse de recibo se emite UNA vez, no para siempre.

    Sin este corte el artefacto crece sin limite con cierres antiguos hasta
    que el operador deja de encontrar los defectos vigentes.
    """
    from tools.audit.diff import diff_runs
    from tools.audit.model import Category, Severity

    arreglado = _hallazgo(
        Category.DOC_DRIFT, Severity.S2, "README.md", "YA_ARREGLADO"
    )

    primera = diff_runs(_run_sintetica([arreglado]), _run_sintetica([]))
    assert [f.status for f in primera.findings] == ["resolved"]

    segunda = diff_runs(primera, _run_sintetica([]))
    assert segunda.findings == []


def test_el_renombre_de_archivo_se_reporta_como_id_reasignado(tmp_path):
    """Mismo defecto, ID nuevo: se reporta reasignacion, no cierre + alta."""
    from tools.audit.diff import diff_runs, load_run, reassignments

    raiz = _arbol(tmp_path)
    destino = tmp_path / "audits"
    _correr(raiz, destino)
    previa = load_run(destino / "latest.json")
    assert previa is not None

    # El defecto no se arregla: solo cambia el nombre del archivo, que si
    # entra en la firma del ID (el directorio y la linea no).
    viejo = raiz / "src" / "betting" / "legacy_sizer.py"
    viejo.rename(viejo.with_name("sizer_antiguo.py"))

    actual = _corrida(raiz)
    reasignaciones = reassignments(previa, actual)

    assert reasignaciones, "el renombre no se reporto como reasignacion"
    assert all(
        r.id_previo != r.id_actual for r in reasignaciones
    ), "una reasignacion con el mismo ID de ambos lados no es una reasignacion"

    publicada = diff_runs(previa, actual)
    reasignados = [f for f in publicada.findings if f.status == "reassigned"]
    assert reasignados, "el hallazgo previo no quedo marcado como reasignado"
    # La nota tiene que llevar al operador al ID nuevo; si no, el rastro se
    # corta igual que si el hallazgo hubiera desaparecido.
    nuevos = {r.id_actual for r in reasignaciones}
    assert any(
        any(nuevo in f.remediation for nuevo in nuevos) for f in reasignados
    )


def test_sin_corrida_previa_no_hay_reasignaciones():
    """`reassignments(None, actual)` es vacio, no un error."""
    from tools.audit.diff import reassignments
    from tools.audit.model import Category, Severity

    actual = _run_sintetica(
        [_hallazgo(Category.DOC_DRIFT, Severity.S2, "README.md", "X")]
    )
    assert reassignments(None, actual) == []


# ---------------------------------------------------------------------------
# 3. El artefacto previo se lee tal como se escribio
# ---------------------------------------------------------------------------

def test_load_run_recupera_los_ids_declarados_por_el_artefacto(tmp_path):
    """El JSON es la fuente de verdad de sus propios IDs.

    Las citas se guardan YA redactadas; recalcular el ID sobre la cita
    redactada daria un valor distinto al que el archivo declara y el diff
    compararia manzanas con peras.
    """
    from tools.audit.diff import identidad, load_run

    raiz = _arbol(tmp_path)
    destino = tmp_path / "audits"
    _correr(raiz, destino)

    datos = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    corrida = load_run(destino / "latest.json")

    assert corrida is not None
    assert [identidad(f) for f in corrida.findings] == [
        h["id"] for h in datos["findings"]
    ]


def test_load_run_devuelve_none_si_no_hay_artefacto_o_esta_corrupto(tmp_path):
    """Un archivo ilegible cuesta el marcado de resueltos, no la auditoria."""
    from tools.audit.diff import load_run

    assert load_run(tmp_path / "no_existe.json") is None

    corrupto = tmp_path / "latest.json"
    corrupto.write_text("{ esto no es json", encoding="utf-8")
    assert load_run(corrupto) is None
