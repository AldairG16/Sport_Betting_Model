"""Checker de riesgo en el manejo de errores.

Clasifica TODA clausula `except` del arbol en una de tres conductas:

- `reraise` — vuelve a lanzar (o aborta el proceso). El fallo sigue siendo
  visible aguas arriba.
- `logged`  — imprime, loguea, alerta o captura el traceback y continua. El
  fallo se degrada pero deja rastro.
- `silent`  — traga la excepcion sin dejar rastro alguno.

Un handler `silent` sobre una ruta de LLM, de notificacion o de escritura a la
base es S1: es exactamente la forma en que este proyecto perdio un dia entero
de analisis sin que nadie se enterara (ver CLAUDE.md, "Never re-add a bare
`except Exception: pass` around the LLM call").

Ademas detecta EXCLUSION SILENCIOSA DE DATOS: un `WHERE ... IS NOT NULL` o un
`.dropna()` descartan filas sin contarlas, asi que un fallo aguas arriba que
deja columnas nulas se ve identico a "no habia datos".

DOS REGLAS QUE NO SE NEGOCIAN:

1. Hay handlers amplios que son load-bearing (feeds externos poco confiables).
   La remediacion propone VISIBILIDAD — contar, loguear, alertar — nunca un
   aborto duro que tumbe el pipeline.
2. Lo que se observa mejor en runtime no se reimplementa aqui: los hallazgos
   de la ruta de notificacion apuntan a `scripts/watchdog.py` via
   `runtime_owner` en vez de duplicar su chequeo (FR-017).

Analisis 100% estatico con `ast` + expresiones regulares. Nunca importa el
modulo bajo analisis: `config/settings.py` lanza RuntimeError en tiempo de
import sin `DB_URL` (FR-006).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tools.audit.graph import iter_python_files
from tools.audit.model import Category, Evidence, Finding, Severity
from tools.audit.redact import redact
from tools.audit.registry import check

__all__ = [
    "HandlerInfo",
    "RISK_PATHS",
    "RUNTIME_OWNER",
    "check_error_handling",
    "classify_handler",
    "detect_risk_paths",
    "scan_handlers",
    "scan_silent_exclusions",
]

# Duenio en runtime de la salud de las rutas de entrega. Los hallazgos de
# notificacion lo citan en vez de reimplementar su chequeo.
RUNTIME_OWNER = "scripts/watchdog.py"

# Rutas donde tragarse una excepcion cuesta dinero o silencia una alerta.
# El orden es el de prioridad al etiquetar un handler.
RISK_PATHS: tuple[str, ...] = ("llm", "db-write", "notify")

_MARCADORES_RUTA: dict[str, tuple[re.Pattern[str], ...]] = {
    "llm": (
        re.compile(r"\banthropic\b", re.I),
        re.compile(r"\bclaude\b", re.I),
        re.compile(r"messages\s*\.\s*create\b"),
        re.compile(r"\bweb_search\b"),
        re.compile(r"completions\s*\.\s*create\b"),
    ),
    "db-write": (
        re.compile(r"\bINSERT\s+INTO\b", re.I),
        re.compile(r"\bUPDATE\s+[\w.\"']+\s+SET\b", re.I),
        re.compile(r"\bDELETE\s+FROM\b", re.I),
        re.compile(r"\bON\s+CONFLICT\b", re.I),
        re.compile(r"\.to_sql\("),
        re.compile(r"\.commit\("),
        re.compile(r"session\s*\.\s*add\b"),
        re.compile(r"\bsave_bets?\b"),
    ),
    "notify": (
        re.compile(r"\btelegram\b", re.I),
        re.compile(r"send[_]?[Mm]essage\b"),
        re.compile(r"\bnotify\w*\s*\(", re.I),
        re.compile(r"\bchat_id\b", re.I),
        re.compile(r"\bsend[_]?alert\b", re.I),
    ),
}

# Fragmentos que, dentro del nombre de una llamada, delatan que el handler
# deja rastro. Deliberadamente amplios: preferimos NO reportar un handler que
# si loguea antes que inundar el reporte de falsos S1.
_SENIALES_LOG = (
    "print",
    "log",
    "warn",
    "error",
    "exception",
    "critical",
    "alert",
    "notify",
    "telegram",
    "traceback",
    "format_exc",
    "report",
    "capture",
    "debug",
)

# Nombres a los que se asigna el error para persistirlo despues (el patron de
# `analyst_heartbeat.error_msg`). Tambien cuenta como dejar rastro.
_SENIALES_ASIGNACION = ("error", "err", "traceback", "fail")

# Llamadas que abortan el proceso: equivalen a re-lanzar para efectos de
# visibilidad, porque el fallo no queda enterrado.
_ABORTOS = ("sys.exit", "os._exit", "exit", "quit")

# --- Exclusion silenciosa de datos ---------------------------------------

# Un filtro de nulos en la clausula WHERE descarta filas sin contarlas. Si un
# fetch aguas arriba dejo la columna nula, el sintoma es "no hay datos" en vez
# de "el fetch fallo".
#
# La columna tiene que ARRANCAR con un caracter de identificador: sin esa
# ancla, la prosa que describe el propio patron (con puntos suspensivos en
# lugar del nombre) se auto-denuncia y el checker se reporta a si mismo.
_WHERE_NOT_NULL = re.compile(
    r"\b(?:WHERE|AND)\s+(?P<col>[A-Za-z_\"'\[][\w.\"'\[\]]*)"
    r"\s+IS\s+NOT\s+NULL\b",
    re.I,
)

# Equivalentes en pandas. Exige un receptor real antes del punto (`df.dropna(`)
# para no casar con la mencion del metodo en un texto.
_DROPNA = re.compile(r"(?<=[\w\)\]])\.(?P<op>dropna|notna|notnull)\s*\(")

_SUFIJOS_ESCANEADOS = frozenset({".py", ".sql"})

# El auditor y su suite no consultan datos de produccion: una consulta de
# fixture ahi no puede enmascarar ningun fetch fallido, y reportarla seria
# ruido permanente en cada corrida semanal. Misma exencion que usa el checker
# de configuracion. NO se exenta ningun script de produccion: el defecto no
# depende de en cual viva la consulta.
_PREFIJOS_EXENTOS_DATOS = ("tools/", "tests/")


@dataclass(frozen=True)
class HandlerInfo:
    """Una clausula `except` clasificada, con su ruta de riesgo."""

    path: str
    line: int
    function: str
    kind: str  # 'reraise' | 'logged' | 'silent'
    risk: str | None  # 'llm' | 'db-write' | 'notify' | None
    exception: str
    ordinal: int = 0

    @property
    def key(self) -> str:
        """Clave estable del handler, independiente del numero de linea.

        Incluye el ordinal dentro de la funcion porque dos handlers identicos
        en la misma funcion son dos defectos distintos y no deben colapsar en
        un unico ID de hallazgo.
        """
        return f"{self.function}#{self.ordinal}"


# ---------------------------------------------------------------------------
# Clasificacion
# ---------------------------------------------------------------------------

def _hijos_efectivos(nodos: list[ast.AST]) -> Iterator[ast.AST]:
    """Recorre el cuerpo saltando definiciones anidadas.

    Un `def` declarado dentro del handler no se EJECUTA al manejar el error:
    contar un `raise` de su cuerpo daria por visible un fallo que sigue
    enterrado.
    """
    for nodo in nodos:
        if isinstance(
            nodo,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        yield nodo
        yield from _hijos_efectivos(list(ast.iter_child_nodes(nodo)))


def _nombre_llamado(nodo: ast.Call) -> str:
    """Nombre punteado de la funcion invocada (`logging.error` -> ese texto)."""
    partes: list[str] = []
    actual: ast.AST = nodo.func
    while isinstance(actual, ast.Attribute):
        partes.append(actual.attr)
        actual = actual.value
    if isinstance(actual, ast.Name):
        partes.append(actual.id)
    return ".".join(reversed(partes))


def _es_log(nombre: str) -> bool:
    return any(senial in nombre.lower() for senial in _SENIALES_LOG)


def _es_aborto(nombre: str) -> bool:
    return nombre in _ABORTOS


def _asigna_error(nodo: ast.AST) -> bool:
    """True si el handler guarda el error en una variable/atributo."""
    if isinstance(nodo, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        objetivos = (
            nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
        )
        for objetivo in objetivos:
            texto = ""
            if isinstance(objetivo, ast.Name):
                texto = objetivo.id
            elif isinstance(objetivo, ast.Attribute):
                texto = objetivo.attr
            elif isinstance(objetivo, ast.Subscript) and isinstance(
                objetivo.slice, ast.Constant
            ):
                texto = str(objetivo.slice.value)
            if any(s in texto.lower() for s in _SENIALES_ASIGNACION):
                return True
    return False


def classify_handler(node: ast.ExceptHandler) -> str:
    """Clasifica un handler como 'reraise', 'logged' o 'silent'.

    Prioridad: si vuelve a lanzar (o aborta) es `reraise`, aunque tambien
    loguee; si deja cualquier rastro es `logged`; si no, es `silent`.

    >>> import ast
    >>> h = ast.parse("try:\\n    x()\\nexcept Exception:\\n    pass").body[0]
    >>> classify_handler(h.handlers[0])
    'silent'
    """
    deja_rastro = False

    for nodo in _hijos_efectivos(list(node.body)):
        if isinstance(nodo, ast.Raise):
            return "reraise"
        if isinstance(nodo, ast.Call):
            nombre = _nombre_llamado(nodo)
            if _es_aborto(nombre):
                return "reraise"
            if _es_log(nombre):
                deja_rastro = True
        elif _asigna_error(nodo):
            deja_rastro = True

    return "logged" if deja_rastro else "silent"


def detect_risk_paths(texto: str) -> tuple[str, ...]:
    """Rutas de riesgo que toca un fragmento de codigo, en orden de prioridad."""
    encontradas = [
        ruta
        for ruta in RISK_PATHS
        if any(patron.search(texto) for patron in _MARCADORES_RUTA[ruta])
    ]
    return tuple(encontradas)


# ---------------------------------------------------------------------------
# Escaneo del arbol
# ---------------------------------------------------------------------------

def _rel(root: Path, ruta: Path) -> str:
    """Ruta relativa en formato posix: el artefacto debe ser portable."""
    return ruta.relative_to(root).as_posix()


def _texto_de(nodos: list[ast.stmt], lineas: list[str]) -> str:
    """Fuente aproximada de un bloque, reconstruida por rango de lineas."""
    if not nodos:
        return ""
    inicio = min(n.lineno for n in nodos)
    fin = max(getattr(n, "end_lineno", n.lineno) or n.lineno for n in nodos)
    return "\n".join(lineas[inicio - 1 : fin])


def _nombre_excepcion(node: ast.ExceptHandler) -> str:
    """`Exception`, `ValueError`, `(A, B)` o `bare` cuando no hay tipo.

    Sin `try/except` alrededor de `ast.unparse` a proposito: el arbol ya
    parseo, asi que no puede fallar, y un handler defensivo aqui seria
    exactamente el silencio que este checker persigue.
    """
    if node.type is None:
        return "bare"
    return ast.unparse(node.type)


def _funciones_por_linea(arbol: ast.Module) -> list[tuple[int, int, str]]:
    """Rangos (inicio, fin, nombre) de cada funcion, para ubicar handlers."""
    rangos: list[tuple[int, int, str]] = []

    def recorrer(nodo: ast.AST, prefijo: str) -> None:
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(
                hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                nombre = f"{prefijo}.{hijo.name}" if prefijo else hijo.name
                fin = getattr(hijo, "end_lineno", hijo.lineno) or hijo.lineno
                if not isinstance(hijo, ast.ClassDef):
                    rangos.append((hijo.lineno, fin, nombre))
                recorrer(hijo, nombre)
            else:
                recorrer(hijo, prefijo)

    recorrer(arbol, "")
    # El rango mas interno gana: se ordena de mayor inicio a menor.
    return sorted(rangos, key=lambda r: (-r[0], r[1]))


def scan_handlers(root: Path) -> list[HandlerInfo]:
    """Todas las clausulas `except` del arbol, clasificadas y ubicadas."""
    raiz = Path(root)
    encontrados: list[HandlerInfo] = []

    for ruta in iter_python_files(raiz):
        try:
            fuente = ruta.read_text(encoding="utf-8", errors="replace")
            arbol = ast.parse(fuente)
        except (OSError, SyntaxError, ValueError):
            continue

        lineas = fuente.splitlines()
        relativa = _rel(raiz, ruta)
        rangos = _funciones_por_linea(arbol)

        # Se recolecta primero y se ordena por linea antes de numerar: el
        # ordinal forma parte de la clave estable del hallazgo y no puede
        # depender del orden de recorrido de `ast.walk`.
        crudos: list[tuple[int, ast.ExceptHandler, str | None]] = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Try):
                continue
            rutas = detect_risk_paths(_texto_de(list(nodo.body), lineas))
            for handler in nodo.handlers:
                crudos.append(
                    (handler.lineno, handler, rutas[0] if rutas else None)
                )

        vistos: dict[str, int] = {}
        for _, handler, riesgo in sorted(crudos, key=lambda t: t[0]):
            funcion = next(
                (
                    nombre
                    for inicio, fin, nombre in rangos
                    if inicio <= handler.lineno <= fin
                ),
                "<module>",
            )
            ordinal = vistos.get(funcion, 0)
            vistos[funcion] = ordinal + 1

            encontrados.append(
                HandlerInfo(
                    path=relativa,
                    line=handler.lineno,
                    function=funcion,
                    kind=classify_handler(handler),
                    risk=riesgo,
                    exception=_nombre_excepcion(handler),
                    ordinal=ordinal,
                )
            )

    return sorted(encontrados, key=lambda h: (h.path, h.line))


def scan_silent_exclusions(root: Path) -> list[tuple[str, int, str, str]]:
    """Filtros que descartan filas nulas: (ruta, linea, columna, cita).

    Se escanea TODO el arbol — .py y .sql — porque el defecto no depende de
    en que script viva la consulta.
    """
    raiz = Path(root)
    encontrados: list[tuple[str, int, str, str]] = []

    for ruta in sorted(
        (
            p
            for p in raiz.rglob("*")
            if p.is_file() and p.suffix in _SUFIJOS_ESCANEADOS
        ),
        key=lambda p: p.relative_to(raiz).as_posix(),
    ):
        partes = ruta.relative_to(raiz).parts
        if any(parte.startswith(".") or parte == "__pycache__" for parte in partes[:-1]):
            continue
        try:
            texto = ruta.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        relativa = _rel(raiz, ruta)
        if relativa.startswith(_PREFIJOS_EXENTOS_DATOS):
            continue
        lineas = texto.splitlines()

        for numero, linea in enumerate(lineas, start=1):
            # Un comentario describe el filtro, no lo ejecuta.
            if linea.lstrip().startswith(("#", "--")):
                continue
            for coincidencia in _WHERE_NOT_NULL.finditer(linea):
                columna = coincidencia.group("col").strip("\"'[]").lower()
                encontrados.append(
                    (relativa, numero, columna, redact(linea.strip())[:200])
                )
            for coincidencia in _DROPNA.finditer(linea):
                encontrados.append(
                    (
                        relativa,
                        numero,
                        coincidencia.group("op").lower(),
                        redact(linea.strip())[:200],
                    )
                )

    return encontrados


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

_IMPACTO_RUTA = {
    "llm": (
        "Un fallo del LLM se traga sin dejar rastro: el analista deja de "
        "opinar y el operador ve exactamente lo mismo que cuando no habia "
        "nada que analizar."
    ),
    "notify": (
        "Un fallo de entrega se traga sin dejar rastro: la alerta nunca "
        "llega y el silencio se lee como 'todo esta bien'."
    ),
    "db-write": (
        "Una escritura fallida se traga sin dejar rastro: la fila nunca se "
        "guarda y el pipeline reporta exito."
    ),
}


@check("error-handling-risk", Category.ERROR_HANDLING_RISK)
def check_error_handling(root: Path) -> tuple[list[Finding], int]:
    """Reporta handlers silenciosos y exclusiones silenciosas de datos."""
    raiz = Path(root)
    handlers = scan_handlers(raiz)
    exclusiones = scan_silent_exclusions(raiz)

    hallazgos: list[Finding] = []
    hallazgos.extend(_hallazgos_silenciosos_criticos(handlers))
    hallazgos.extend(_hallazgos_silenciosos_restantes(handlers))
    hallazgos.extend(_hallazgos_exclusion(exclusiones))

    # Ubicaciones inspeccionadas: cada handler clasificado mas cada filtro
    # revisado. Nunca es 0 sobre un arbol con fuentes, asi que "0 hallazgos"
    # no puede confundirse con "el checker no corrio".
    inspeccionadas = len(handlers) + len(exclusiones)

    hallazgos.sort(key=lambda f: (f.severity.value, f.id))
    return hallazgos, inspeccionadas


def _hallazgos_silenciosos_criticos(
    handlers: list[HandlerInfo],
) -> list[Finding]:
    """Un hallazgo S1 por handler silencioso sobre una ruta de riesgo."""
    hallazgos: list[Finding] = []

    for handler in handlers:
        if handler.kind != "silent" or handler.risk is None:
            continue

        hallazgos.append(
            Finding(
                category=Category.ERROR_HANDLING_RISK,
                severity=Severity.S1,
                evidence=[
                    Evidence(
                        path=handler.path,
                        line=handler.line,
                        key=handler.key,
                        quote=(
                            f"except {handler.exception}: silencioso "
                            f"({handler.risk})"
                        ),
                    )
                ],
                impact=(
                    f"`except {handler.exception}` silencioso en "
                    f"{handler.path}:{handler.line} sobre la ruta "
                    f"{handler.risk}. "
                    + _IMPACTO_RUTA[handler.risk]
                ),
                remediation=(
                    "No abortar: el handler puede ser load-bearing frente a un "
                    "feed poco confiable. Agregar VISIBILIDAD — registrar la "
                    "excepcion con su traceback y contar el fallo — para que "
                    "el silencio deje de ser indistinguible del exito."
                ),
                effort="S",
                runtime_owner=(
                    RUNTIME_OWNER if handler.risk == "notify" else None
                ),
            )
        )

    return hallazgos


def _hallazgos_silenciosos_restantes(
    handlers: list[HandlerInfo],
) -> list[Finding]:
    """Un hallazgo S3 por archivo con handlers silenciosos fuera de riesgo.

    Se agrupa por archivo a proposito: un handler silencioso en una ruta de
    calculo es deuda tecnica, no una perdida de dinero, y listarlos uno por
    uno sepultaria a los S1 bajo decenas de entradas de baja prioridad.
    """
    por_archivo: dict[str, list[HandlerInfo]] = {}
    for handler in handlers:
        if handler.kind == "silent" and handler.risk is None:
            por_archivo.setdefault(handler.path, []).append(handler)

    hallazgos: list[Finding] = []
    for ruta, grupo in sorted(por_archivo.items()):
        grupo = sorted(grupo, key=lambda h: h.line)
        hallazgos.append(
            Finding(
                category=Category.ERROR_HANDLING_RISK,
                severity=Severity.S3,
                evidence=[
                    Evidence(
                        path=h.path,
                        line=h.line,
                        key=h.key,
                        quote=f"except {h.exception}: silencioso",
                    )
                    # Cota deterministica: un hallazgo con 60 anclas no es
                    # accionable.
                    for h in grupo[:10]
                ],
                impact=(
                    f"{len(grupo)} handler(s) silencioso(s) en `{ruta}`. No "
                    "estan sobre una ruta de LLM, notificacion ni escritura a "
                    "la base, pero cada uno convierte un fallo en un valor "
                    "por defecto que nadie audita."
                ),
                remediation=(
                    "Registrar la excepcion antes de continuar. Mantener el "
                    "flujo degradado si el handler protege una dependencia "
                    "poco confiable; lo que no puede quedar es el silencio."
                ),
                effort="S",
            )
        )

    return hallazgos


def _hallazgos_exclusion(
    exclusiones: list[tuple[str, int, str, str]],
) -> list[Finding]:
    """Filtros que descartan filas nulas sin contarlas.

    Se agrupa por (archivo, columna) porque la firma del hallazgo ignora el
    numero de linea: dos filtros sobre la misma columna en el mismo archivo
    son un unico defecto con dos anclas.
    """
    agrupadas: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for ruta, linea, columna, cita in exclusiones:
        agrupadas.setdefault((ruta, columna), []).append((linea, cita))

    hallazgos: list[Finding] = []
    for (ruta, columna), sitios in sorted(agrupadas.items()):
        sitios = sorted(sitios)
        pandas = columna in ("dropna", "notna", "notnull")
        descripcion = (
            f"`.{columna}()`" if pandas else f"`{columna} IS NOT NULL`"
        )
        hallazgos.append(
            Finding(
                category=Category.ERROR_HANDLING_RISK,
                severity=Severity.S2,
                evidence=[
                    Evidence(
                        path=ruta,
                        line=linea,
                        key=f"null-filter#{columna}",
                        quote=cita,
                    )
                    for linea, cita in sitios[:10]
                ],
                impact=(
                    f"{descripcion} en `{ruta}` descarta filas nulas sin "
                    "contarlas. Si un fetch aguas arriba dejo la columna "
                    "vacia, el sintoma es 'no habia datos' en vez de 'el "
                    "fetch fallo', y el fallo real queda enmascarado."
                ),
                remediation=(
                    "Contar las filas descartadas y registrar el conteo "
                    "cuando sea mayor que cero. El filtro puede quedarse: lo "
                    "que falta es la senal de que hubo exclusion."
                ),
                effort="S",
                # Cuantos nulos son legitimos y cuantos delatan un fallo solo
                # se sabe mirando los datos: el operador confirma.
                uncertain=True,
            )
        )

    return hallazgos
