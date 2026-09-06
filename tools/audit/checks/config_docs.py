"""Checkers de configuracion y de deriva documental.

Dos checkers registrados por separado sobre una misma lectura estatica del
arbol:

- `config-inconsistency` — constantes que leen el entorno y estan declaradas
  FUERA de `config/settings.py` (la unica fuente de verdad mandatada), mas
  las variables que la ruta de codigo de un workflow consume y que ese
  workflow no suministra en su bloque `env:`.
- `doc-drift` — el mismo valor operativo escrito con numeros distintos en
  `README.md`, `CLAUDE.md`, `.env.example`, `config/settings.py` o el bloque
  `env:` de un workflow, mas comentarios que afirman un numero que
  contradice la constante que documentan.

DOS INVARIANTES QUE NO SE NEGOCIAN:

1. `config/settings.py` se lee con `ast`, NUNCA se importa. Importarlo lanza
   RuntimeError en tiempo de import cuando `DB_URL` no esta definida, y la
   auditoria tiene que poder correr sin un solo secreto configurado (FR-006).
2. Un hallazgo emite NOMBRES de variable y UBICACIONES, jamas el valor
   resuelto de una credencial. Los nombres sensibles quedan fuera de la
   comparacion y toda cita pasa por `redact()` antes de viajar al artefacto.

Un hallazgo de contradiccion cita TODAS las ubicaciones en desacuerdo: un
reporte de un solo lado obliga al operador a buscar el otro a mano y deja de
ser accionable.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from tools.audit.graph import (
    build_import_graph,
    build_module_index,
    iter_python_files,
)
from tools.audit.model import Category, Evidence, Finding, Severity
from tools.audit.redact import redact
from tools.audit.registry import check
from tools.audit.workflows import read_workflows

__all__ = [
    "Declaracion",
    "check_config",
    "check_doc_drift",
    "constantes_descentralizadas",
    "es_sensible",
    "extract_documented_values",
    "extract_settings_constants",
    "lectura_de_entorno",
    "settings_path",
]


# ---------------------------------------------------------------------------
# Vocabulario compartido
# ---------------------------------------------------------------------------

# Los tres accesores mandatados por CLAUDE.md mas la variante booleana.
# Cualquiera de ellos convierte una asignacion en "constante que depende del
# entorno", que es lo que tiene que vivir en `config/settings.py`.
LECTORES_ENV = frozenset({"env_int", "env_float", "env_str", "env_bool"})

# Casts que envuelven una lectura de entorno sin cambiar su naturaleza:
# `int(os.environ.get("X", "50"))` sigue siendo una lectura de `X`. Sin
# esto, `lectura_de_entorno` ve el `int(...)` exterior, no reconoce el
# nombre y devuelve None sin mirar los argumentos, dejando la constante
# invisible para el checker.
_CASTS_TRANSPARENTES = frozenset({"int", "float", "str", "bool"})

# Ruta canonica de la fuente unica de verdad, siempre en formato posix.
RUTA_SETTINGS = "config/settings.py"

# Rutas cuyo codigo corre en produccion: una constante descentralizada aqui
# puede cambiar cuanto se apuesta, no solo confundir al lector.
_PREFIJOS_PRODUCCION = ("scripts/", "src/")

# El auditor y la suite no son codigo de produccion: sus constantes locales no
# pertenecen a `config/settings.py` y reportarlas seria ruido puro.
_PREFIJOS_EXENTOS = ("tools/", "tests/")

# Sufijos que denotan una credencial. Estos nombres NUNCA entran a la
# comparacion de valores: comparar `ODDS_API_KEY` entre dos archivos exigiria
# leer ambos valores, y el artefacto se commitea al repositorio.
_SUFIJOS_SENSIBLES = frozenset(
    {
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "PWD",
        "CREDENTIAL",
        "CREDENTIALS",
        "AUTH",
        "URL",
        "URI",
        "DSN",
    }
)

# Valores de plantilla en la documentacion. No son desacuerdos: son huecos
# que el operador tiene que rellenar.
_PLACEHOLDERS = re.compile(
    r"^(?:tu_|your_|<|\.\.\.|xxx|change_?me|placeholder)", re.IGNORECASE
)

_NOMBRE_CONSTANTE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# `NAME=value` de un `.env` o de un bloque ```env dentro de un markdown.
_ASIGNACION_ENV = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]{2,})\s*=\s*(?P<value>[^#\n]*)"
)

# Fila de tabla markdown: `| NAME | value | ... |`.
_FILA_TABLA = re.compile(r"^\s*\|(?P<celdas>.+)\|\s*$")

# Un nombre mencionado en prosa junto a su valor por defecto:
# "ANTHROPIC_DAILY_BUDGET_USD (default $0.30)".
#
# SIN `re.IGNORECASE` a proposito: con esa bandera el grupo del nombre tambien
# acepta minusculas y la primera palabra de la frase ("presupuesto") gana el
# match, consume la linea y el nombre real nunca se registra. La distincion
# mayuscula/minuscula ES la senal de que se habla de una variable.
_PROSA = re.compile(
    r"(?P<name>[A-Z][A-Z0-9_]{2,})[^\n]{0,40}?"
    r"(?:[Dd]efault|DEFAULT|[Dd]efecto)\D{0,12}?"
    r"(?P<value>\d+(?:\.\d+)?)"
)

# Un numero suelto, sin letras ni puntos pegados, para leer comentarios.
_NUMERO = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


@dataclass(frozen=True)
class Declaracion:
    """Una constante de nivel de modulo, con su ancla y su valor por defecto."""

    nombre: str
    env_var: str | None
    valor: str | None
    ruta: str
    linea: int
    cita: str

    @property
    def clave(self) -> str:
        """Nombre bajo el que se cruza con la documentacion."""
        return self.env_var or self.nombre


# ---------------------------------------------------------------------------
# Utilidades de lectura
# ---------------------------------------------------------------------------

def settings_path(root: Path) -> Path:
    """Ubicacion canonica de la unica fuente de verdad de configuracion."""
    return Path(root) / "config" / "settings.py"


def _rel(root: Path, ruta: Path) -> str:
    """Ruta relativa a la raiz, siempre con `/`.

    El artefacto tiene que ser byte-identico corriendo en Windows local y en
    ubuntu-latest, asi que jamas se serializa un separador nativo.
    """
    return ruta.relative_to(root).as_posix()


def _lineas(ruta: Path) -> list[str]:
    """Lineas del archivo, o lista vacia si no se puede leer."""
    try:
        return ruta.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _cita(lineas: list[str], numero: int | None) -> str:
    """Texto redactado de una linea concreta, listo para el artefacto."""
    if not numero or numero < 1 or numero > len(lineas):
        return ""
    return redact(lineas[numero - 1].strip())[:200]


def _parsear(ruta: Path) -> ast.Module | None:
    """Arbol sintactico del archivo, o None si no existe o no parsea."""
    try:
        return ast.parse(ruta.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None


def es_sensible(nombre: str) -> bool:
    """True si el nombre denota una credencial y su valor no debe compararse."""
    partes = nombre.upper().split("_")
    return bool(partes) and partes[-1] in _SUFIJOS_SENSIBLES


def _canonico(texto: str) -> str | None:
    """Forma canonica comparable de un valor, o None si no es comparable.

    Solo numeros y booleanos: comparar prosa entre un README y un docstring
    genera falsos positivos a mansalva, y los desacuerdos que cuestan dinero
    (`50` contra `500`, `0.20` contra `0.30`) siempre son numericos.
    """
    limpio = texto.strip().strip("\"'").strip()
    if not limpio:
        return None
    minusculas = limpio.lower()
    if minusculas in ("true", "false"):
        return minusculas
    monetario = limpio.lstrip("$").rstrip("%")
    try:
        valor = float(monetario)
    except ValueError:
        return None
    if valor == int(valor) and abs(valor) < 1e15:
        return str(int(valor))
    return repr(valor)


# ---------------------------------------------------------------------------
# Lectura estatica de constantes (ast, nunca import)
# ---------------------------------------------------------------------------

def _cuerpo_de_modulo(arbol: ast.Module) -> list[ast.stmt]:
    """Sentencias de nivel de modulo, incluyendo las de un `if`/`try` externo.

    `ast.walk` seria demasiado ancho: agarraria asignaciones locales dentro de
    funciones, que no son constantes de configuracion. El cuerpo pelado seria
    demasiado angosto: `config/settings.py` y varios scripts declaran dentro
    de un `if` o un `try` de nivel superior.
    """
    sentencias: list[ast.stmt] = []
    pendientes = list(arbol.body)
    while pendientes:
        nodo = pendientes.pop(0)
        sentencias.append(nodo)
        if isinstance(nodo, ast.If):
            pendientes = list(nodo.body) + list(nodo.orelse) + pendientes
        elif isinstance(nodo, ast.Try):
            pendientes = (
                list(nodo.body)
                + [s for manejador in nodo.handlers for s in manejador.body]
                + list(nodo.orelse)
                + list(nodo.finalbody)
                + pendientes
            )
    return sentencias


def _nombre_llamado(nodo: ast.AST) -> str:
    """Nombre punteado de lo que se invoca: `env_int`, `os.environ.get`."""
    partes: list[str] = []
    actual: ast.AST | None = nodo
    while isinstance(actual, ast.Attribute):
        partes.append(actual.attr)
        actual = actual.value
    if isinstance(actual, ast.Name):
        partes.append(actual.id)
    return ".".join(reversed(partes))


def _texto_literal(nodo: ast.AST | None) -> str | None:
    """Representacion textual de un literal simple, o None."""
    if isinstance(nodo, ast.Constant):
        if isinstance(nodo.value, bool):
            return "true" if nodo.value else "false"
        if isinstance(nodo.value, (int, float, str)):
            return str(nodo.value)
        return None
    if isinstance(nodo, ast.UnaryOp) and isinstance(nodo.op, ast.USub):
        interno = _texto_literal(nodo.operand)
        if interno is not None:
            return f"-{interno}"
    return None


def lectura_de_entorno(nodo: ast.AST) -> tuple[str | None, str | None] | None:
    """`(env_var, default)` si el nodo lee el entorno; None si no lo hace.

    Cubre los accesores mandatados (`env_int`/`env_float`/`env_str`), la
    variante booleana, y la lectura cruda `os.environ.get` / `os.getenv` /
    `os.environ["X"]`. Un cast transparente que envuelva cualquiera de esos
    (`int(os.environ.get("X", "50"))`) se atraviesa y se reporta la lectura
    interna: el cast no cambia que la constante depende del entorno.
    """
    if isinstance(nodo, ast.Subscript):
        objetivo = _nombre_llamado(nodo.value)
        if objetivo in ("os.environ", "environ"):
            return _texto_literal(nodo.slice), None
        return None

    if not isinstance(nodo, ast.Call):
        return None

    llamado = _nombre_llamado(nodo.func)
    corto = llamado.rsplit(".", 1)[-1]

    # Un cast transparente se atraviesa: lo que importa es lo que envuelve.
    # Se exige exactamente un argumento posicional y ningun keyword para que
    # `int(x, base=16)` —que no es una lectura de entorno— siga rechazandose.
    if corto in _CASTS_TRANSPARENTES and len(nodo.args) == 1 and not nodo.keywords:
        return lectura_de_entorno(nodo.args[0])

    if corto not in LECTORES_ENV and llamado not in (
        "os.environ.get",
        "os.getenv",
        "environ.get",
        "getenv",
    ):
        return None

    env_var = _texto_literal(nodo.args[0]) if nodo.args else None
    defecto = _texto_literal(nodo.args[1]) if len(nodo.args) > 1 else None
    if defecto is None:
        for palabra_clave in nodo.keywords:
            if palabra_clave.arg in ("default", "defecto"):
                defecto = _texto_literal(palabra_clave.value)
    return env_var, defecto


def _declaraciones_de_archivo(root: Path, ruta: Path) -> list[Declaracion]:
    """Constantes de nivel de modulo del archivo, dependan o no del entorno."""
    arbol = _parsear(ruta)
    if arbol is None:
        return []
    lineas = _lineas(ruta)
    relativa = _rel(Path(root), ruta)

    salida: list[Declaracion] = []
    for sentencia in _cuerpo_de_modulo(arbol):
        if isinstance(sentencia, ast.Assign):
            objetivos: list[ast.expr] = list(sentencia.targets)
        elif isinstance(sentencia, ast.AnnAssign) and sentencia.value is not None:
            objetivos = [sentencia.target]
        else:
            continue

        for objetivo in objetivos:
            if not isinstance(objetivo, ast.Name):
                continue
            if not _NOMBRE_CONSTANTE.match(objetivo.id):
                continue
            valor = sentencia.value
            lectura = lectura_de_entorno(valor) if valor is not None else None
            if lectura is not None:
                env_var, defecto = lectura
            else:
                env_var, defecto = None, _texto_literal(valor)
            salida.append(
                Declaracion(
                    nombre=objetivo.id,
                    env_var=env_var,
                    valor=defecto,
                    ruta=relativa,
                    linea=sentencia.lineno,
                    cita=_cita(lineas, sentencia.lineno),
                )
            )
    return salida


def _declaraciones(root: Path) -> list[Declaracion]:
    """Todas las constantes de modulo del arbol, ordenadas por ancla."""
    raiz = Path(root)
    salida: list[Declaracion] = []
    for ruta in iter_python_files(raiz):
        salida.extend(_declaraciones_de_archivo(raiz, ruta))
    return sorted(salida, key=lambda d: (d.ruta, d.linea, d.nombre))


def extract_settings_constants(root: Path) -> dict[str, str]:
    """Constantes de `config/settings.py` con su valor por defecto textual.

    Se lee con `ast`: importar el modulo lanza RuntimeError sin `DB_URL` y
    romperia la auditoria sin secretos. Devuelve `{}` cuando el archivo no
    existe, para que el checker funcione contra cualquier subarbol.

    La clave es el nombre de la constante; cuando lee una variable de entorno
    con otro nombre, ese alias tambien se registra para poder cruzarlo con
    `.env.example` y con los bloques `env:` de los workflows.
    """
    raiz = Path(root)
    ruta = settings_path(raiz)
    if not ruta.is_file():
        return {}

    salida: dict[str, str] = {}
    for declaracion in _declaraciones_de_archivo(raiz, ruta):
        if declaracion.valor is None:
            continue
        salida[declaracion.nombre] = declaracion.valor
        alias = declaracion.env_var
        if alias and alias != declaracion.nombre:
            salida.setdefault(alias, declaracion.valor)
    return salida


# ---------------------------------------------------------------------------
# Lectura de documentacion
# ---------------------------------------------------------------------------

def _valor_documentado_util(nombre: str, crudo: str) -> str | None:
    """Filtra plantillas, secretos y prosa; devuelve el valor limpio o None."""
    if es_sensible(nombre):
        return None
    limpio = crudo.strip().strip("`").strip().strip("\"'").strip()
    if not limpio or _PLACEHOLDERS.match(limpio):
        return None
    if len(limpio) > 40:
        return None
    return limpio


def extract_documented_values(path: Path) -> dict[str, str]:
    """Pares NOMBRE -> valor declarados en un markdown o en un `.env.example`.

    Reconoce las tres formas en que este repositorio documenta un valor:

    - `NAME=value`          (bloque ```env del README, `.env.example`)
    - `| NAME | value |`    (tabla markdown)
    - `NAME ... default 0.30`  (prosa de CLAUDE.md)

    Los nombres sensibles y los valores de plantilla (`tu_api_key_aqui`)
    quedan fuera: nunca se compara — ni se cita — el valor de una credencial.
    Gana la primera aparicion, que es la que el lector encuentra primero.
    """
    ruta = Path(path)
    if not ruta.is_file():
        return {}

    salida: dict[str, str] = {}

    def registrar(nombre: str, crudo: str) -> None:
        valor = _valor_documentado_util(nombre, crudo)
        if valor is not None:
            salida.setdefault(nombre, valor)

    for linea in _lineas(ruta):
        desnuda = linea.strip()
        if desnuda.startswith("#") and "=" in desnuda:
            # Linea de `.env` comentada: es documentacion apagada, no un valor
            # vigente. Se ignora a proposito.
            continue

        asignacion = _ASIGNACION_ENV.match(linea)
        if asignacion:
            registrar(asignacion.group("name"), asignacion.group("value"))
            continue

        tabla = _FILA_TABLA.match(linea)
        if tabla:
            celdas = [celda.strip() for celda in tabla.group("celdas").split("|")]
            if len(celdas) >= 2:
                nombre = celdas[0].strip("`*_ ")
                if _NOMBRE_CONSTANTE.match(nombre):
                    registrar(nombre, celdas[1])
            continue

        for prosa in _PROSA.finditer(linea):
            registrar(prosa.group("name"), prosa.group("value"))

    return salida


def _bloques_env(nodo: object) -> list[tuple[str, object]]:
    """Todos los pares de todo bloque `env:` del documento, a cualquier nivel."""
    pares: list[tuple[str, object]] = []
    if isinstance(nodo, dict):
        bloque = nodo.get("env")
        if isinstance(bloque, dict):
            pares.extend((str(clave), valor) for clave, valor in bloque.items())
        for clave, valor in nodo.items():
            if clave != "env":
                pares.extend(_bloques_env(valor))
    elif isinstance(nodo, list):
        for elemento in nodo:
            pares.extend(_bloques_env(elemento))
    return pares


def _valores_env_de_workflows(root: Path) -> list[tuple[str, str, str]]:
    """`(ruta_workflow, NOMBRE, valor)` de los `env:` con valor literal.

    Las entradas `${{ secrets.X }}` se descartan: no tienen valor estatico y
    resolverlas seria justo lo que la auditoria promete no hacer.
    """
    raiz = Path(root)
    carpeta = raiz / ".github" / "workflows"
    if not carpeta.is_dir():
        return []

    salida: list[tuple[str, str, str]] = []
    archivos = sorted(
        ruta
        for patron in ("*.yml", "*.yaml")
        for ruta in carpeta.glob(patron)
        if ruta.is_file()
    )
    for ruta in archivos:
        try:
            documento = yaml.safe_load(ruta.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        relativa = _rel(raiz, ruta)
        for nombre, valor in _bloques_env(documento):
            if not isinstance(valor, (str, int, float, bool)):
                continue
            texto = "true" if valor is True else "false" if valor is False else str(valor)
            if "${{" in texto:
                continue
            if _valor_documentado_util(nombre, texto) is None:
                continue
            salida.append((relativa, nombre, texto.strip()))
    return salida


# ---------------------------------------------------------------------------
# Checker: config-inconsistency
# ---------------------------------------------------------------------------

def _es_exento(relativa: str) -> bool:
    """True si el archivo no participa de la centralizacion de configuracion."""
    return relativa.startswith(_PREFIJOS_EXENTOS)


def constantes_descentralizadas(root: Path) -> list[Declaracion]:
    """Constantes que leen el entorno y no viven en `config/settings.py`.

    El defecto es la UBICACION, no el accesor: `API_CREDITS_STOP_THRESHOLD =
    env_int(...)` en `scripts/update_upcoming_matches.py` usa el helper
    correcto y aun asi parte la fuente de verdad en dos.
    """
    return [
        declaracion
        for declaracion in _declaraciones(root)
        if declaracion.env_var is not None
        and declaracion.ruta != RUTA_SETTINGS
        and not _es_exento(declaracion.ruta)
    ]


def _modulo_de_token(token: str, indice: dict[str, Path]) -> str | None:
    """Modulo punteado al que apunta un token de un comando `run:`."""
    limpio = token.strip().strip("\"'")
    if not limpio or limpio.startswith("-"):
        return None
    if limpio.endswith(".py"):
        candidato = Path(limpio).with_suffix("").as_posix().replace("/", ".")
    else:
        candidato = limpio
    candidato = candidato.lstrip(".")
    return candidato if candidato in indice else None


def _modulos_del_workflow(info, indice: dict[str, Path]) -> set[str]:
    """Modulos que los comandos `run:` de ese workflow invocan directamente."""
    raices: set[str] = set()
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


def _alcance(grafo: dict[str, set[str]], raices: set[str]) -> set[str]:
    """Cierre transitivo de `raices` sobre el grafo de imports."""
    vistos: set[str] = set()
    pendientes = sorted(raices)
    while pendientes:
        actual = pendientes.pop()
        if actual in vistos:
            continue
        vistos.add(actual)
        pendientes.extend(
            siguiente
            for siguiente in grafo.get(actual, ())
            if siguiente not in vistos
        )
    return vistos


def _consumos_de_entorno(root: Path, ruta: Path) -> list[tuple[str, int, str]]:
    """`(NOMBRE, linea, cita)` por cada lectura de entorno del archivo.

    Aqui si se usa `ast.walk`: una variable leida dentro de una funcion se
    consume igual de fuerte cuando el workflow ejecuta esa funcion.
    """
    arbol = _parsear(ruta)
    if arbol is None:
        return []
    lineas = _lineas(ruta)

    salida: set[tuple[str, int, str]] = set()
    for nodo in ast.walk(arbol):
        lectura = lectura_de_entorno(nodo)
        if lectura is None:
            continue
        env_var, _ = lectura
        if not env_var or not _NOMBRE_CONSTANTE.match(env_var):
            continue
        numero = getattr(nodo, "lineno", 0) or 0
        salida.add((env_var, numero, _cita(lineas, numero)))
    return sorted(salida)


@check("config-inconsistency", Category.CONFIG_INCONSISTENCY)
def check_config(root: Path) -> tuple[list[Finding], int]:
    """Configuracion partida en dos.

    Mitad 1: constantes de entorno declaradas fuera de `config/settings.py`.
    Mitad 2: variables que la ruta de codigo de un workflow consume sin que
    ese workflow las suministre, teniendo otro workflow que si lo hace.

    Nada de esto se detecta importando: todo sale de `ast` sobre el codigo y
    de PyYAML sobre los workflows.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    # --- Mitad 1: constantes de entorno fuera de la fuente unica de verdad ---
    todas = _declaraciones(raiz)
    inspeccionadas += len(todas)

    for declaracion in todas:
        if declaracion.env_var is None:
            continue
        if declaracion.ruta == RUTA_SETTINGS or _es_exento(declaracion.ruta):
            continue
        produccion = declaracion.ruta.startswith(_PREFIJOS_PRODUCCION)
        hallazgos.append(
            Finding(
                category=Category.CONFIG_INCONSISTENCY,
                severity=Severity.S1 if produccion else Severity.S2,
                evidence=[
                    Evidence(
                        path=declaracion.ruta,
                        line=declaracion.linea,
                        key=declaracion.nombre,
                        quote=declaracion.cita,
                    )
                ],
                impact=(
                    f"`{declaracion.nombre}` lee el entorno desde "
                    f"`{declaracion.ruta}:{declaracion.linea}` en vez de "
                    f"`{RUTA_SETTINGS}`: la configuracion tiene dos fuentes "
                    f"de verdad y cambiar una no cambia la otra."
                ),
                remediation=(
                    f"Mover la declaracion de `{declaracion.nombre}` a "
                    f"`{RUTA_SETTINGS}` e importarla desde ahi."
                ),
                effort="S",
            )
        )

    # --- Mitad 2: matriz `env:` incompleta por workflow ---
    workflows = read_workflows(raiz)
    if workflows:
        indice = build_module_index(raiz)
        grafo = build_import_graph(raiz)

        # Cache por modulo: el mismo modulo aparece en el alcance de varios
        # workflows y volver a parsearlo por cada uno es puro desperdicio.
        cache: dict[str, list[tuple[str, int, str]]] = {}

        suministradas: dict[str, list[str]] = {}
        for info in workflows:
            for clave in info.env_keys:
                suministradas.setdefault(clave, []).append(info.path)

        for info in workflows:
            raices = _modulos_del_workflow(info, indice)
            for modulo in sorted(_alcance(grafo, raices)):
                ruta = indice.get(modulo)
                if ruta is None:
                    continue
                if modulo not in cache:
                    cache[modulo] = _consumos_de_entorno(raiz, ruta)
                for env_var, linea, cita in cache[modulo]:
                    inspeccionadas += 1
                    if env_var in info.env_keys:
                        continue
                    # Solo se acusa cuando otro workflow SI la suministra: una
                    # variable que nadie inyecta corre con su default y eso es
                    # una decision, no una omision.
                    hermanos = sorted(
                        otro
                        for otro in suministradas.get(env_var, [])
                        if otro != info.path
                    )
                    if not hermanos:
                        continue
                    evidencias = [
                        Evidence(path=info.path, key=env_var),
                        Evidence(
                            path=_rel(raiz, ruta),
                            line=linea,
                            key=env_var,
                            quote=cita,
                        ),
                    ]
                    evidencias.extend(
                        Evidence(path=hermano, key=env_var) for hermano in hermanos
                    )
                    hallazgos.append(
                        Finding(
                            category=Category.CONFIG_INCONSISTENCY,
                            severity=Severity.S1,
                            evidence=evidencias,
                            impact=(
                                f"`{info.path}` ejecuta codigo que lee "
                                f"`{env_var}` pero no la declara en su bloque "
                                f"`env:`; {', '.join(hermanos)} si la declara. "
                                f"En ese workflow la variable corre con su "
                                f"valor por defecto sin que nadie lo note."
                            ),
                            remediation=(
                                f"Agregar `{env_var}` al bloque `env:` de "
                                f"`{info.path}`, igual que en {hermanos[0]}."
                            ),
                            effort="S",
                        )
                    )

    hallazgos.sort(key=lambda hallazgo: (hallazgo.severity.value, hallazgo.id))
    return hallazgos, inspeccionadas


# ---------------------------------------------------------------------------
# Checker: doc-drift
# ---------------------------------------------------------------------------

def _primera_linea_con(lineas: list[str], nombre: str) -> int | None:
    """Primera linea que menciona el nombre, para anclar la cita."""
    for numero, texto in enumerate(lineas, start=1):
        if nombre in texto:
            return numero
    return None


def _fuentes_documentales(root: Path) -> list[tuple[str, Evidence, str]]:
    """`(NOMBRE, evidencia, valor canonico)` de cada fuente que declara un valor.

    Cinco fuentes, un solo formato de salida, para poder cruzarlas todas
    contra todas sin casos especiales.
    """
    raiz = Path(root)
    salida: list[tuple[str, Evidence, str]] = []

    # 1) Codigo: el default de cada constante de modulo (incluye settings.py).
    for declaracion in _declaraciones(raiz):
        if declaracion.valor is None:
            continue
        clave = declaracion.clave
        if es_sensible(clave):
            continue
        canonico = _canonico(declaracion.valor)
        if canonico is None:
            continue
        salida.append(
            (
                clave,
                Evidence(
                    path=declaracion.ruta,
                    line=declaracion.linea,
                    key=clave,
                    quote=declaracion.cita,
                ),
                canonico,
            )
        )

    # 2) Documentacion y plantilla de entorno.
    for relativa in (".env.example", "README.md", "CLAUDE.md"):
        ruta = raiz / relativa
        if not ruta.is_file():
            continue
        lineas = _lineas(ruta)
        for nombre, crudo in extract_documented_values(ruta).items():
            canonico = _canonico(crudo)
            if canonico is None:
                continue
            numero = _primera_linea_con(lineas, nombre)
            salida.append(
                (
                    nombre,
                    Evidence(
                        path=relativa,
                        line=numero,
                        key=nombre,
                        quote=_cita(lineas, numero),
                    ),
                    canonico,
                )
            )

    # 3) Bloques `env:` de los workflows con valor literal.
    for ruta_workflow, nombre, crudo in _valores_env_de_workflows(raiz):
        canonico = _canonico(crudo)
        if canonico is None:
            continue
        salida.append((nombre, Evidence(path=ruta_workflow, key=nombre), canonico))

    return salida


def _comentarios_de(
    lineas: list[str], declaracion: Declaracion
) -> list[tuple[str, int]]:
    """Comentarios atribuibles a la declaracion: el inline y el de arriba.

    El de arriba solo cuenta si menciona el nombre de la constante o la
    palabra `default`/`defecto`: sin ese ancla, cualquier comentario de
    seccion quedaria atribuido a la primera constante que lo siga.
    """
    salida: list[tuple[str, int]] = []
    indice = declaracion.linea - 1
    if 0 <= indice < len(lineas):
        propia = lineas[indice]
        if "#" in propia:
            salida.append((propia.split("#", 1)[1], declaracion.linea))
    if indice - 1 >= 0:
        anterior = lineas[indice - 1].strip()
        if anterior.startswith("#"):
            cuerpo = anterior.lstrip("#").strip()
            minusculas = cuerpo.lower()
            menciona = declaracion.nombre.lower() in minusculas or any(
                palabra in minusculas for palabra in ("default", "defecto")
            )
            if menciona:
                salida.append((cuerpo, declaracion.linea - 1))
    return salida


def _mismo_valor_otra_unidad(afirmado: str, esperado: str, comentario: str) -> bool:
    """True si comentario y constante dicen lo mismo en unidades distintas.

    `MAX_STAKE_PCT = 0.05  # tope duro: 5% del bankroll` no es deriva: es la
    misma cantidad escrita como fraccion y como porcentaje. Reportarla seria
    exactamente el ruido que hace que nadie vuelva a abrir el reporte.
    """
    contexto = comentario.lower()
    if "%" not in contexto and "por ciento" not in contexto and "pct" not in contexto:
        return False
    try:
        uno, otro = float(afirmado), float(esperado)
    except ValueError:
        return False
    return uno == otro * 100 or otro == uno * 100


def _drift_de_comentarios(root: Path) -> tuple[list[Finding], int]:
    """Comentarios que afirman un numero distinto al de la constante.

    Se exige que el comentario sea inequivocamente sobre esa constante y que
    contenga UN solo numero. Sin esas dos restricciones cualquier comentario
    en espaniol con una cifra suelta ("ultimos 30 dias") se convertiria en un
    falso positivo, y un checker ruidoso deja de leerse a la segunda semana.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    for ruta in iter_python_files(raiz):
        relativa = _rel(raiz, ruta)
        if _es_exento(relativa):
            continue
        lineas = _lineas(ruta)
        for declaracion in _declaraciones_de_archivo(raiz, ruta):
            if declaracion.valor is None:
                continue
            esperado = _canonico(declaracion.valor)
            if esperado is None or esperado in ("true", "false"):
                continue

            for comentario, numero_linea in _comentarios_de(lineas, declaracion):
                inspeccionadas += 1
                numeros = _NUMERO.findall(comentario)
                if len(numeros) != 1:
                    continue
                afirmado = _canonico(numeros[0])
                if afirmado is None or afirmado == esperado:
                    continue
                if _mismo_valor_otra_unidad(afirmado, esperado, comentario):
                    continue
                hallazgos.append(
                    Finding(
                        category=Category.DOC_DRIFT,
                        severity=Severity.S3,
                        evidence=[
                            Evidence(
                                path=relativa,
                                line=declaracion.linea,
                                key=declaracion.nombre,
                                quote=declaracion.cita,
                            ),
                            Evidence(
                                path=relativa,
                                line=numero_linea,
                                key=declaracion.nombre,
                                quote=redact(comentario.strip())[:200],
                            ),
                        ],
                        impact=(
                            f"El comentario de `{declaracion.nombre}` en "
                            f"`{relativa}:{numero_linea}` afirma `{afirmado}` "
                            f"pero la constante vale `{esperado}`: quien lea "
                            f"el comentario razona sobre un valor que el "
                            f"codigo no usa."
                        ),
                        remediation=(
                            f"Actualizar el comentario a `{esperado}`, o "
                            f"corregir la constante si el comentario describe "
                            f"la intencion real."
                        ),
                        effort="S",
                    )
                )

    return hallazgos, inspeccionadas


@check("doc-drift", Category.DOC_DRIFT)
def check_doc_drift(root: Path) -> tuple[list[Finding], int]:
    """El mismo valor operativo escrito distinto en dos lugares.

    Cruza `config/settings.py`, el resto del codigo, `.env.example`,
    `README.md`, `CLAUDE.md` y los bloques `env:` de los workflows. Cada
    hallazgo cita TODAS las ubicaciones en desacuerdo: reportar una sola
    obligaria al operador a buscar la otra a mano.
    """
    raiz = Path(root)
    entradas = _fuentes_documentales(raiz)

    por_nombre: dict[str, list[tuple[Evidence, str]]] = {}
    for nombre, evidencia, valor in entradas:
        por_nombre.setdefault(nombre, []).append((evidencia, valor))

    hallazgos: list[Finding] = []
    inspeccionadas = len(entradas)

    for nombre in sorted(por_nombre):
        ubicaciones = por_nombre[nombre]
        valores = {valor for _, valor in ubicaciones}
        if len(valores) < 2:
            continue

        ordenadas = sorted(ubicaciones, key=lambda par: (par[0].anchor, par[1]))
        ejecutables = sum(
            1
            for evidencia, _ in ordenadas
            if evidencia.path.endswith(".py")
            or evidencia.path.startswith(".github/workflows/")
        )
        detalle = "; ".join(
            f"{evidencia.anchor} dice `{valor}`" for evidencia, valor in ordenadas
        )
        hallazgos.append(
            Finding(
                category=Category.DOC_DRIFT,
                severity=Severity.S1 if ejecutables >= 2 else Severity.S2,
                evidence=[evidencia for evidencia, _ in ordenadas],
                impact=(
                    f"`{nombre}` tiene {len(valores)} valores distintos segun "
                    f"donde se lea: {detalle}. El operador que siga la fuente "
                    f"equivocada configura produccion mal."
                ),
                remediation=(
                    f"Elegir el valor vigente de `{nombre}` y alinear todas "
                    f"las ubicaciones con `{RUTA_SETTINGS}` como fuente unica "
                    f"de verdad."
                ),
                effort="S",
            )
        )

    comentarios, inspeccionadas_comentarios = _drift_de_comentarios(raiz)
    hallazgos.extend(comentarios)
    inspeccionadas += inspeccionadas_comentarios

    hallazgos.sort(key=lambda hallazgo: (hallazgo.severity.value, hallazgo.id))
    return hallazgos, inspeccionadas
