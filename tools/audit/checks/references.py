"""Checkers del grafo de referencias: imports rotos, huerfanos y ciclos.

Tres categorias, tres checkers registrados por separado, una sola fuente de
verdad: el indice de modulos y el grafo de imports que `tools/audit/graph.py`
construye con `ast`. Nada de esto importa el codigo auditado — importar
`config.settings` sin `DB_URL` lanza RuntimeError en tiempo de import y
romperia la auditoria sin secretos (FR-006).

- `broken-reference` — un import que no resuelve a ningun modulo del arbol.
  Es S1 siempre: revienta con ImportError en la primera linea que se ejecute,
  y en produccion eso significa un cron que no corre y apuestas que no se
  colocan.
- `orphan-code` — modulos inalcanzables desde cualquier entry point. Las
  raices NO son una lista fija: salen de los comandos `run:` de
  `.github/workflows/*.yml` mas todo `tests/`, de modo que un workflow nuevo
  amplia el conjunto automaticamente.

  La misma categoria cubre un segundo caso, mas silencioso: un modulo de
  `src/` al que SOLO llega `tests/`. Contar la suite como raiz es correcto
  para no acusar de muerto a lo que la suite ejerce, pero deja un punto ciego:
  un modulo extraido y probado que produccion nunca llama pasa por vivo.
  Ese es exactamente el estado de `src/pipeline/bet_decision.py` mientras el
  pipeline conserve su copia en linea — la suite queda verde y no dice nada
  sobre lo que se apuesta. Por eso el alcance se calcula DOS veces: una desde
  los workflows (produccion) y otra desde workflows + tests.
- `circular-dependency` — S2 si el ciclo toca un modulo alcanzable desde un
  workflow (afecta produccion), S3 si queda confinado a scripts manuales o
  de un solo uso.

Cuando un modulo solo se alcanza por un import construido dinamicamente, el
hallazgo se marca `uncertain=True` en vez de afirmar que esta muerto: borrar
un modulo cargado con `importlib.import_module` es exactamente el tipo de
"limpieza" que tumba un cron.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

from tools.audit.graph import (
    build_import_graph,
    build_module_index,
    find_cycles,
)
from tools.audit.model import Category, Evidence, Finding, Severity
from tools.audit.registry import check
from tools.audit.workflows import read_workflows

__all__ = [
    "PRODUCTION_ROOTS",
    "check_broken_references",
    "check_cycles",
    "check_orphans",
    "dynamic_import_prefixes",
    "entry_point_modules",
    "reachable_modules",
    "test_only_modules",
]


# ---------------------------------------------------------------------------
# Utilidades compartidas
# ---------------------------------------------------------------------------

def _rel(root: Path, ruta: Path) -> str:
    """Ruta relativa a la raiz, siempre con `/`.

    El artefacto tiene que ser byte-identico corriendo en Windows local y en
    ubuntu-latest, asi que jamas se serializa un separador nativo.
    """
    return ruta.relative_to(root).as_posix()


def _parsear(ruta: Path) -> ast.Module | None:
    """Arbol sintactico del archivo, o None si no parsea."""
    try:
        return ast.parse(ruta.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None


def _linea_fuente(ruta: Path, numero: int | None) -> str:
    """Texto de una linea concreta, recortado, para citarlo como evidencia."""
    if not numero or numero < 1:
        return ""
    try:
        lineas = ruta.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if numero > len(lineas):
        return ""
    return lineas[numero - 1].strip()[:200]


def _paquetes(indice: dict[str, Path]) -> set[str]:
    """Modulos que vienen de un `__init__.py` (paquetes reales)."""
    return {n for n, ruta in indice.items() if ruta.name == "__init__.py"}


def _raices_internas(indice: dict[str, Path]) -> set[str]:
    """Primeros segmentos de todos los modulos: `src`, `config`, `tools`...

    Sirve para separar imports internos de dependencias externas. Solo se
    audita lo interno: `import requests` no es una referencia rota del
    repositorio aunque el paquete no este instalado en la maquina.
    """
    return {nombre.split(".")[0] for nombre in indice}


def _existe(nombre: str, indice: dict[str, Path], root: Path) -> bool:
    """El nombre punteado corresponde a un modulo o a un paquete namespace.

    `src/` no tiene ningun `__init__.py`, asi que un directorio existente es
    un destino de import perfectamente valido y no puede reportarse como roto.
    """
    if not nombre:
        return False
    if nombre in indice:
        return True
    return root.joinpath(*nombre.split(".")).is_dir()


def _base_relativa(modulo: str, es_paquete: bool, level: int) -> str | None:
    """Prefijo de un import relativo (`from .. import x`). None si sube de mas."""
    partes = modulo.split(".") if modulo else []
    if not es_paquete:
        partes = partes[:-1]
    subir = level - 1
    if subir > len(partes):
        return None
    if subir:
        partes = partes[:-subir]
    return ".".join(partes)


def _destinos_del_nodo(
    nodo: ast.Import | ast.ImportFrom,
    modulo: str,
    es_paquete: bool,
) -> list[str]:
    """Nombres punteados que un nodo de import pretende alcanzar.

    Para `ImportFrom` se devuelve el modulo origen, no los simbolos: si el
    modulo existe, que un nombre concreto falte dentro de el no es decidible
    estaticamente sin ejecutarlo, y reportarlo generaria un falso positivo en
    cada `from x import CONSTANTE`.
    """
    if isinstance(nodo, ast.Import):
        return [alias.name for alias in nodo.names]

    if nodo.level:
        base = _base_relativa(modulo, es_paquete, nodo.level)
        if base is None:
            return []
        prefijo = f"{base}.{nodo.module}" if nodo.module else base
    else:
        prefijo = nodo.module or ""
    return [prefijo] if prefijo else []


# ---------------------------------------------------------------------------
# broken-reference
# ---------------------------------------------------------------------------

@check("broken-reference", Category.BROKEN_REFERENCE)
def check_broken_references(root: Path) -> tuple[list[Finding], int]:
    """Imports internos que no resuelven a ningun modulo del arbol.

    El caso base es `load_10_seasons.py:1`, que importa `src.data.collector`
    cuando `src/data/` solo contiene `queries.py`.
    """
    root = Path(root)
    indice = build_module_index(root)
    paquetes = _paquetes(indice)
    internas = _raices_internas(indice)

    hallazgos: list[Finding] = []
    inspeccionados = 0

    for modulo, ruta in indice.items():
        relativa = _rel(root, ruta)
        arbol = _parsear(ruta)
        if arbol is None:
            # Un archivo que no parsea rompe a todo el que lo importe; se
            # cuenta como una ubicacion inspeccionada para que la categoria
            # nunca aparezca con cero ubicaciones.
            inspeccionados += 1
            hallazgos.append(
                Finding(
                    category=Category.BROKEN_REFERENCE,
                    severity=Severity.S1,
                    evidence=[Evidence(path=relativa, key="syntax")],
                    impact=(
                        f"`{relativa}` no parsea: cualquier import suyo falla "
                        f"con SyntaxError y detiene el proceso que lo cargue."
                    ),
                    remediation=(
                        "Corregir el error de sintaxis o retirar el archivo a "
                        "`archive/` con su disposicion registrada."
                    ),
                    effort="S",
                )
            )
            continue

        es_paquete = modulo in paquetes

        # ast.walk y no solo el cuerpo de nivel superior: en scripts/ y en
        # tests/ es habitual importar dentro de la funcion, y esos imports
        # revientan igual de fuerte cuando el destino no existe.
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.Import, ast.ImportFrom)):
                continue
            for destino in _destinos_del_nodo(nodo, modulo, es_paquete):
                inspeccionados += 1
                if destino.split(".")[0] not in internas:
                    # Dependencia externa o stdlib: fuera del alcance.
                    continue
                if _existe(destino, indice, root):
                    continue
                hallazgos.append(
                    Finding(
                        category=Category.BROKEN_REFERENCE,
                        severity=Severity.S1,
                        evidence=[
                            Evidence(
                                path=relativa,
                                line=nodo.lineno,
                                key=destino,
                                quote=_linea_fuente(ruta, nodo.lineno),
                            )
                        ],
                        impact=(
                            f"`{relativa}` importa `{destino}`, que no existe "
                            f"en el arbol: el modulo revienta con ImportError "
                            f"en cuanto se ejecuta."
                        ),
                        remediation=(
                            f"Crear `{destino}`, apuntar el import al modulo "
                            f"real, o retirar `{relativa}` a `archive/` con su "
                            f"disposicion registrada."
                        ),
                        effort="S",
                    )
                )

    return hallazgos, inspeccionados


# ---------------------------------------------------------------------------
# Alcanzabilidad
# ---------------------------------------------------------------------------

def _modulo_de_token(token: str, indice: dict[str, Path]) -> str | None:
    """Traduce un token de un comando `run:` a un modulo del indice."""
    limpio = token.strip().strip("\"'")
    if not limpio:
        return None
    if limpio.endswith(".py"):
        punteado = limpio[:-3].replace("\\", "/").strip("./").replace("/", ".")
        return punteado if punteado in indice else None
    if limpio in indice:
        return limpio
    return None


def entry_point_modules(root: Path, indice: dict[str, Path]) -> set[str]:
    """Modulos invocados desde los comandos `run:` de los workflows.

    Deliberadamente NO hay lista fija de entry points: si manana aparece
    `.github/workflows/audit.yml`, sus comandos entran solos y el analisis de
    huerfanos deja de reportar lo que ese workflow ejecuta.
    """
    raices: set[str] = set()
    for info in read_workflows(root):
        for comando in info.run_commands:
            for linea in comando.splitlines():
                tokens = linea.replace("&&", " ").replace(";", " ").split()
                for posicion, token in enumerate(tokens):
                    if token == "-m" and posicion + 1 < len(tokens):
                        modulo = _modulo_de_token(tokens[posicion + 1], indice)
                        if modulo:
                            raices.add(modulo)
                        continue
                    modulo = _modulo_de_token(token, indice)
                    if modulo:
                        raices.add(modulo)
    return raices


def _raices_de_tests(indice: dict[str, Path]) -> set[str]:
    """Todo lo que cuelga de `tests/` es raiz: la suite es un entry point."""
    return {
        nombre
        for nombre in indice
        if nombre == "tests" or nombre.startswith("tests.")
    }


def reachable_modules(grafo: dict[str, set[str]], raices: set[str]) -> set[str]:
    """Cierre transitivo de `raices` sobre el grafo de imports."""
    vistos: set[str] = set()
    cola = deque(sorted(raices))
    while cola:
        actual = cola.popleft()
        if actual in vistos:
            continue
        vistos.add(actual)
        for siguiente in sorted(grafo.get(actual, ())):
            if siguiente not in vistos:
                cola.append(siguiente)
    return vistos


# --- imports dinamicos -----------------------------------------------------

def _nombre_llamada(nodo: ast.Call) -> str:
    """Nombre invocable de una llamada: `import_module`, `__import__`, ..."""
    if isinstance(nodo.func, ast.Attribute):
        return nodo.func.attr
    if isinstance(nodo.func, ast.Name):
        return nodo.func.id
    return ""


def _prefijo_literal(nodo: ast.AST, modulo: str) -> str | None:
    """Prefijo literal de la expresion pasada a un import dinamico.

    Devuelve None si la expresion es una constante — ese caso no es
    incertidumbre sino una arista real del grafo. Devuelve el prefijo conocido
    cuando la expresion se construye: `f"src.markets.{n}"` -> `"src.markets."`.
    `__name__` se sustituye por el modulo actual, que es exactamente el patron
    con el que `tools/audit/checks/__init__.py` carga sus propios checkers.
    """
    if isinstance(nodo, ast.Constant):
        return None

    if isinstance(nodo, ast.JoinedStr):
        piezas: list[str] = []
        for parte in nodo.values:
            if isinstance(parte, ast.Constant) and isinstance(parte.value, str):
                piezas.append(parte.value)
                continue
            if (
                isinstance(parte, ast.FormattedValue)
                and isinstance(parte.value, ast.Name)
                and parte.value.id == "__name__"
            ):
                piezas.append(modulo)
                continue
            break
        return "".join(piezas)

    if isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Add):
        if isinstance(nodo.left, ast.Constant) and isinstance(
            nodo.left.value, str
        ):
            return nodo.left.value
        izquierda = _prefijo_literal(nodo.left, modulo)
        if izquierda:
            return izquierda

    return ""


def _prefijo_por_defecto(modulo: str) -> str:
    """Paquete del modulo importador, cuando no hay ningun prefijo literal.

    `importlib.import_module(nombre)` no dice nada del destino. En vez de
    marcar TODO el repositorio como incierto — lo que vaciaria de valor al
    reporte — se asume el alcance mas probable de un cargador dinamico: sus
    propios vecinos de paquete. Un modulo de nivel superior no aporta prefijo.
    """
    if "." not in modulo:
        return ""
    return modulo.rsplit(".", 1)[0] + "."


def dynamic_import_prefixes(
    root: Path, indice: dict[str, Path], modulos: set[str]
) -> tuple[set[str], dict[str, str]]:
    """Prefijos alcanzables dinamicamente y destinos constantes descubiertos.

    Segundo valor: destino constante -> modulo que lo importa, para poder
    tratarlo como arista real del grafo (`import_module("src.markets.btts")`
    no deja al modulo huerfano).
    """
    prefijos: set[str] = set()
    constantes: dict[str, str] = {}

    for modulo in sorted(modulos):
        ruta = indice.get(modulo)
        if ruta is None:
            continue
        arbol = _parsear(ruta)
        if arbol is None:
            continue
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            if _nombre_llamada(nodo) not in {"import_module", "__import__"}:
                continue
            if not nodo.args:
                continue
            argumento = nodo.args[0]
            if isinstance(argumento, ast.Constant) and isinstance(
                argumento.value, str
            ):
                constantes[argumento.value] = modulo
                continue
            prefijo = _prefijo_literal(argumento, modulo)
            if prefijo is None:
                continue
            if not prefijo:
                prefijo = _prefijo_por_defecto(modulo)
            if prefijo:
                prefijos.add(prefijo)

    return prefijos, constantes


# ---------------------------------------------------------------------------
# orphan-code
# ---------------------------------------------------------------------------

# Arbol donde vive la logica que produccion tiene que ejecutar. Un modulo de
# `scripts/` que ningun workflow lanza sigue siendo un entry point manual
# legitimo (CLAUDE.md documenta varios), y `tools/` es la propia auditoria,
# cuyo unico ejecutor legitimo es la suite. Por eso el punto ciego "solo lo
# alcanzan los tests" se reporta unicamente bajo `src/`.
PRODUCTION_ROOTS = ("src/",)


def _es_codigo_de_produccion(relativa: str) -> bool:
    """True si el archivo vive en el arbol que produccion debe ejecutar."""
    return relativa.startswith(PRODUCTION_ROOTS)


def _alcance(
    root: Path,
    indice: dict[str, Path],
    grafo: dict[str, set[str]],
    raices: set[str],
) -> set[str]:
    """Cierre transitivo de `raices`, incluyendo imports dinamicos constantes.

    `import_module("src.markets.btts")` es una arista real aunque `ast` no la
    vea como `Import`: se agrega al conjunto de raices y se recalcula, de modo
    que el modulo no se acuse de muerto por un tecnicismo del parser.
    """
    alcanzables = reachable_modules(grafo, raices)
    _, constantes = dynamic_import_prefixes(root, indice, alcanzables)
    extra = {destino for destino in constantes if destino in indice}
    if extra:
        alcanzables = reachable_modules(grafo, raices | extra)
    return alcanzables


def test_only_modules(
    root: Path,
    indice: dict[str, Path],
    grafo: dict[str, set[str]],
) -> set[str]:
    """Modulos de produccion a los que SOLO llega `tests/`.

    Ni huerfanos (la suite los ejerce) ni vivos (ningun workflow los ejecuta):
    el hueco exacto en el que cae un modulo extraido y probado que nadie
    llego a cablear.
    """
    raices_produccion = entry_point_modules(root, indice)
    raices_tests = _raices_de_tests(indice)

    alcance_produccion = _alcance(root, indice, grafo, raices_produccion)
    alcance_total = _alcance(root, indice, grafo, raices_produccion | raices_tests)

    return {
        modulo
        for modulo in alcance_total - alcance_produccion
        if modulo in indice and _es_codigo_de_produccion(_rel(root, indice[modulo]))
    }


@check("orphan-code", Category.ORPHAN_CODE)
def check_orphans(root: Path) -> tuple[list[Finding], int]:
    """Modulos que ningun entry point alcanza, mas los que solo alcanza la suite.

    Dos alcances, no uno: contar `tests/` como raiz evita acusar de muerto a lo
    que la suite ejerce, pero por si solo esconde el caso contrario — codigo de
    `src/` que unicamente existe para los tests. Ambos se reportan bajo
    `orphan-code`, con anclas distinguibles (`key='test-only'`) para que el
    diff corrida-a-corrida no los confunda.
    """
    root = Path(root)
    indice = build_module_index(root)
    grafo = build_import_graph(root)

    raices_produccion = entry_point_modules(root, indice)
    raices = raices_produccion | _raices_de_tests(indice)

    alcance_produccion = _alcance(root, indice, grafo, raices_produccion)
    alcanzables = _alcance(root, indice, grafo, raices)

    prefijos, _ = dynamic_import_prefixes(root, indice, alcanzables)
    # Para el caso test-only la pregunta es si un cargador dinamico DE
    # PRODUCCION podria alcanzarlo; los prefijos que solo aparecen en modulos
    # de test no absuelven a nadie.
    prefijos_produccion, _ = dynamic_import_prefixes(
        root, indice, alcance_produccion
    )

    hallazgos: list[Finding] = []
    for modulo in sorted(indice):
        relativa = _rel(root, indice[modulo])

        if modulo in alcanzables:
            if modulo in alcance_produccion:
                continue
            if not _es_codigo_de_produccion(relativa):
                continue

            incierto = any(
                modulo.startswith(prefijo) for prefijo in prefijos_produccion
            )
            if incierto:
                impacto = (
                    f"`{relativa}` solo aparece importado desde `tests/`, pero "
                    f"un cargador dinamico de produccion podria alcanzarlo: "
                    f"hallazgo INCIERTO, no se afirma que produccion lo ignore."
                )
                arreglo = (
                    "Confirmar a mano si algun `import_module` de la ruta de "
                    "produccion lo carga; si no, cablearlo o registrar su "
                    "disposicion en `archive/ARCHIVE.md`."
                )
                severidad = Severity.S4
            else:
                impacto = (
                    f"`{relativa}` solo se alcanza desde `tests/`: ningun "
                    f"workflow lo ejecuta. La suite lo prueba en verde mientras "
                    f"produccion sigue corriendo otro codigo, asi que esos tests "
                    f"no dicen nada sobre las apuestas que se colocan."
                )
                arreglo = (
                    f"Cablear `{relativa}` desde la ruta de produccion que "
                    f"deberia usarlo (y borrar la copia en linea que lo "
                    f"sustituye), o registrar su disposicion en "
                    f"`archive/ARCHIVE.md` si el enganche aun no existe."
                )
                # S2: no mueve dinero por si mismo, pero rompe la garantia
                # operativa de que un test verde cubre lo que produccion corre.
                severidad = Severity.S2

            hallazgos.append(
                Finding(
                    category=Category.ORPHAN_CODE,
                    severity=severidad,
                    evidence=[Evidence(path=relativa, key="test-only")],
                    impact=impacto,
                    remediation=arreglo,
                    effort="S",
                    uncertain=incierto,
                )
            )
            continue

        incierto = any(modulo.startswith(prefijo) for prefijo in prefijos)
        if incierto:
            impacto = (
                f"`{relativa}` no aparece en ningun import estatico, pero un "
                f"cargador dinamico podria alcanzarlo: hallazgo INCIERTO, no "
                f"se afirma que este muerto. Borrarlo puede tumbar un cron."
            )
            arreglo = (
                "Confirmar a mano si algun `import_module` lo carga; si no, "
                "registrar su disposicion en `archive/ARCHIVE.md`."
            )
            severidad = Severity.S4
        else:
            impacto = (
                f"`{relativa}` es inalcanzable desde los workflows y desde "
                f"`tests/`: codigo que se lee y se mantiene sin ejecutarse "
                f"nunca, y que confunde sobre que corre en produccion."
            )
            arreglo = (
                "Registrar su disposicion en `archive/ARCHIVE.md` (retirado, "
                "pendiente de cablear, o conservado a proposito). No borrar "
                "sin dejar constancia."
            )
            severidad = Severity.S3

        hallazgos.append(
            Finding(
                category=Category.ORPHAN_CODE,
                severity=severidad,
                evidence=[Evidence(path=relativa)],
                impact=impacto,
                remediation=arreglo,
                effort="S",
                uncertain=incierto,
            )
        )

    return hallazgos, len(indice)


# ---------------------------------------------------------------------------
# circular-dependency
# ---------------------------------------------------------------------------

@check("circular-dependency", Category.CIRCULAR_DEPENDENCY)
def check_cycles(root: Path) -> tuple[list[Finding], int]:
    """Ciclos de import, priorizados por si tocan o no la ruta de produccion."""
    root = Path(root)
    indice = build_module_index(root)
    grafo = build_import_graph(root)

    # Solo los workflows definen "produccion": tests/ alcanza practicamente
    # todo el arbol y usarlo aqui elevaria cualquier ciclo a S2.
    produccion = reachable_modules(grafo, entry_point_modules(root, indice))

    hallazgos: list[Finding] = []
    for ciclo in find_cycles(grafo):
        toca_produccion = any(modulo in produccion for modulo in ciclo)
        severidad = Severity.S2 if toca_produccion else Severity.S3
        evidencia = [
            Evidence(path=_rel(root, indice[modulo]), key="cycle")
            for modulo in ciclo
            if modulo in indice
        ]
        cadena = " -> ".join(list(ciclo) + [ciclo[0]])
        if toca_produccion:
            impacto = (
                f"Ciclo de imports en la ruta de produccion: {cadena}. El "
                f"orden de import decide que modulo ve al otro a medias, asi "
                f"que el ImportError parcial aparece y desaparece segun quien "
                f"arranque primero."
            )
        else:
            impacto = (
                f"Ciclo de imports confinado a scripts manuales o de un solo "
                f"uso: {cadena}. No corre en cron, pero endurece cualquier "
                f"refactor de esos modulos."
            )
        hallazgos.append(
            Finding(
                category=Category.CIRCULAR_DEPENDENCY,
                severity=severidad,
                evidence=evidencia,
                impact=impacto,
                remediation=(
                    "Romper el ciclo extrayendo lo compartido a un modulo sin "
                    "dependencias, o mover el import al interior de la funcion "
                    "que lo necesita."
                ),
                effort="M",
            )
        )

    return hallazgos, len(grafo)
