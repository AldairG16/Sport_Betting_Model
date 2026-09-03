"""Checker de filtracion de credenciales.

Un literal con forma de credencial en fuente versionada es S1 sin matices: el
repositorio es publico para efectos practicos (CI, artifacts, clones) y una
llave commiteada ya esta comprometida aunque se borre despues.

REGLA QUE NO SE NEGOCIA: el VALOR nunca viaja al artefacto. El hallazgo lleva
el NOMBRE de la variable y su UBICACION — que es lo unico accionable — y toda
cita se construye a mano como `NOMBRE = <redacted>`, ademas de pasar por
`redact()` antes de salir. Un checker de secretos que filtra el secreto es
peor que no tener checker.

NO se reporta lo que es una REFERENCIA al secreto y no el secreto:

- `${{ secrets.X }}` en un workflow,
- `os.environ[...]`, `os.getenv(...)`, `env_str("...")`,
- placeholders de plantilla (`tu_api_key_aqui`, `your_token_here`, `<...>`,
  `changeme`, `xxx`) tipicos de `.env.example`.

Analisis 100% estatico: `ast` sobre los .py y expresiones regulares sobre el
resto. Nunca importa el modulo bajo analisis (FR-006).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from tools.audit.graph import EXCLUDED_DIRS
from tools.audit.model import Category, Evidence, Finding, Severity
from tools.audit.redact import REDACTED, redact
from tools.audit.registry import check

__all__ = [
    "SUFIJOS_SENSIBLES",
    "SecretHit",
    "check_secrets",
    "es_nombre_sensible",
    "es_placeholder",
    "scan_secrets",
]

# Sufijo del identificador que denota una credencial. Se ancla al final para
# que `ODDS_API_KEY` entre y `KEYWORDS` no.
SUFIJOS_SENSIBLES = frozenset(
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
        "APIKEY",
    }
)

# Palabras que solo denotan una credencial cuando estan CALIFICADAS. `key` y
# `auth` a secas son nombres genericos y ubicuos — `sorted(key=...)`,
# `Evidence(key=...)`, un modulo `auth` — de modo que tratarlos como
# credencial genera un S1 falso cada vez que aparecen con un literal largo al
# lado. Un S1 falso recurrente vacia de significado al tope del ranking, que
# es justo lo que la auditoria tiene que responder ("que puede costarme
# dinero esta semana"), asi que la palabra sola no alcanza: hace falta
# `API_KEY`, `ODDS_API_KEY`, `basic_auth`.
#
# Se excluye SOLO el caso de una unica parte: los tokens con forma reconocible
# (`sk-ant-`, `ghp_`, `AKIA`, la contrasena de una URL de conexion) los sigue
# atrapando `_FORMAS_TOKEN`, que no depende del nombre de la variable.
_GENERICAS_SIN_CALIFICAR = frozenset({"KEY", "AUTH"})

# Extensiones donde puede vivir una credencial commiteada.
_SUFIJOS_ESCANEADOS = frozenset(
    {
        ".py",
        ".yml",
        ".yaml",
        ".json",
        ".md",
        ".txt",
        ".cfg",
        ".ini",
        ".toml",
        ".sh",
        ".ps1",
        ".sql",
        ".env",
        "",
    }
)

# Archivos de ejemplo: por definicion contienen NOMBRES de credencial con
# valores de plantilla. Ahi solo se buscan tokens con forma real.
_ARCHIVOS_PLANTILLA = frozenset({".env.example", ".env.sample", ".env.template"})

# Un valor que no es el secreto sino una referencia a el.
_REFERENCIAS = re.compile(
    r"^\s*(?:"
    r"\$\{\{|\$\{|\$[A-Z_]|"
    r"os\.|env_|settings\.|config\.|self\.|cls\.|"
    r"secrets\.|getenv|environ"
    r")",
    re.IGNORECASE,
)

# Plantillas tipicas de `.env.example` y de la documentacion.
_PLACEHOLDER = re.compile(
    r"^(?:tu[_-]|your[_-]|my[_-]|<|\.\.\.|x{3,}|change[_-]?me|placeholder|"
    r"dummy|fake|sample|example|test[_-]|abc123|123456|aqui|here|todo|none|"
    r"null|true|false|redacted)",
    re.IGNORECASE,
)
_PLACEHOLDER_SUFIJO = re.compile(
    r"(?:_aqui|_here|_goes_here|_placeholder|_example|_dummy|_xxx)$",
    re.IGNORECASE,
)

# Formas de token reconocibles por si mismas, sin nombre de variable alrededor.
_FORMAS_TOKEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-api-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai-api-key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("github-token", re.compile(r"(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{30,}")),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{30,}")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("telegram-bot-token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b")),
    (
        "db-connection-password",
        re.compile(r"://[^\s:/@]+:(?P<value>[^\s@/]{4,})@"),
    ),
)

# Una linea que DEFINE un patron de deteccion no contiene una credencial. Sin
# esta exencion el propio auditor (redact.py, este archivo) se autodenuncia.
_LINEA_DE_PATRON = re.compile(r"re\.compile\(|\[A-Za-z|\{\d+,|\\b|\(\?:")

# Longitud minima de un valor para considerarlo credencial. Debajo de esto un
# literal es una bandera o un separador, no una llave.
_LARGO_MINIMO = 12

# Superficie de fixtures: la suite y el propio auditor necesitan literales CON
# forma de credencial para poder probar que se redactan. Ahi el hallazgo NO se
# suprime — eso abriria un escondite perfecto para una llave real — pero baja a
# S2 y se marca como incierto, para que S1 siga significando "esto puede
# costarme dinero esta semana".
_PREFIJOS_FIXTURE = ("tests/", "tools/")


@dataclass(frozen=True)
class SecretHit:
    """Una credencial detectada. NUNCA guarda el valor, solo su forma.

    `name` es el nombre de la variable (o la forma del token) y `path`/`line`
    la ubicacion. El valor se descarta en el momento de la deteccion para que
    no exista ninguna ruta por la que pueda llegar al artefacto.
    """

    path: str
    line: int
    name: str
    kind: str  # 'hardcoded-literal' | forma de token
    length: int = 0

    @property
    def quote(self) -> str:
        """Cita segura: nombre y forma, jamas el valor."""
        return f"{self.name} = {REDACTED} ({self.kind}, {self.length} chars)"


def es_nombre_sensible(nombre: str) -> bool:
    """True si el identificador denota una credencial."""
    limpio = nombre.strip().strip("\"'")
    if not limpio:
        return False
    partes = [parte for parte in re.split(r"[_\-.]", limpio.upper()) if parte]
    if partes and partes[-1] in SUFIJOS_SENSIBLES:
        # `key` / `auth` a secas no bastan: hace falta que esten calificadas.
        if not (len(partes) == 1 and partes[0] in _GENERICAS_SIN_CALIFICAR):
            return True
    # `apiKey`, `botToken`: camelCase sin separadores.
    return bool(
        re.search(
            r"(?:API_?KEY|ACCESS_?TOKEN|BOT_?TOKEN|PASSWORD|SECRET)$",
            limpio.upper(),
        )
    )


def es_placeholder(valor: str) -> bool:
    """True si el valor es una plantilla o una referencia, no un secreto."""
    limpio = valor.strip().strip("\"'").strip()
    if len(limpio) < _LARGO_MINIMO:
        return True
    if _REFERENCIAS.match(limpio):
        return True
    if _PLACEHOLDER.match(limpio):
        return True
    if _PLACEHOLDER_SUFIJO.search(limpio):
        return True
    # Un valor sin ningun caracter alfanumerico variado (`------------`) es un
    # separador de documentacion, no una llave.
    return len(set(limpio)) < 5


def _rel(root: Path, ruta: Path) -> str:
    """Ruta relativa en formato posix: el artefacto debe ser portable."""
    return ruta.relative_to(root).as_posix()


def _archivos(raiz: Path) -> list[Path]:
    """Archivos escaneables del arbol, ordenados y sin directorios de ruido."""
    encontrados: list[Path] = []
    for ruta in raiz.rglob("*"):
        if not ruta.is_file():
            continue
        if ruta.suffix not in _SUFIJOS_ESCANEADOS and not ruta.name.startswith(
            ".env"
        ):
            continue
        partes = ruta.relative_to(raiz).parts
        if any(parte in EXCLUDED_DIRS for parte in partes[:-1]):
            continue
        encontrados.append(ruta)
    return sorted(encontrados, key=lambda p: p.relative_to(raiz).as_posix())


def _hits_python(relativa: str, fuente: str) -> list[SecretHit]:
    """Asignaciones `NOMBRE_SENSIBLE = "literal"` detectadas con `ast`."""
    try:
        arbol = ast.parse(fuente)
    except (SyntaxError, ValueError):
        return []

    hits: list[SecretHit] = []
    for nodo in ast.walk(arbol):
        objetivos: list[ast.expr] = []
        valor: ast.expr | None = None

        if isinstance(nodo, ast.Assign):
            objetivos, valor = list(nodo.targets), nodo.value
        elif isinstance(nodo, ast.AnnAssign) and nodo.value is not None:
            objetivos, valor = [nodo.target], nodo.value
        elif isinstance(nodo, ast.keyword) and nodo.arg:
            # `Anthropic(api_key="...")`
            objetivos, valor = [ast.Name(id=nodo.arg)], nodo.value
        else:
            continue

        if not isinstance(valor, ast.Constant) or not isinstance(
            valor.value, str
        ):
            continue

        for objetivo in objetivos:
            nombre = ""
            if isinstance(objetivo, ast.Name):
                nombre = objetivo.id
            elif isinstance(objetivo, ast.Attribute):
                nombre = objetivo.attr
            elif isinstance(objetivo, ast.Subscript) and isinstance(
                objetivo.slice, ast.Constant
            ):
                nombre = str(objetivo.slice.value)

            if not es_nombre_sensible(nombre) or es_placeholder(valor.value):
                continue

            hits.append(
                SecretHit(
                    path=relativa,
                    line=getattr(valor, "lineno", 1),
                    name=nombre,
                    kind="hardcoded-literal",
                    length=len(valor.value),
                )
            )

    return hits


def _hits_texto(relativa: str, texto: str) -> list[SecretHit]:
    """Asignaciones `NOMBRE=valor` fuera de Python (.env, .yml, .md)."""
    patron = re.compile(
        r"^\s*(?:export\s+|-\s*)?(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)"
        r"\s*[:=]\s*(?P<value>[^\s#]+)"
    )
    hits: list[SecretHit] = []
    for numero, linea in enumerate(texto.splitlines(), start=1):
        if linea.lstrip().startswith("#"):
            continue
        coincidencia = patron.match(linea)
        if not coincidencia:
            continue
        nombre = coincidencia.group("name")
        valor = coincidencia.group("value")
        if not es_nombre_sensible(nombre) or es_placeholder(valor):
            continue
        hits.append(
            SecretHit(
                path=relativa,
                line=numero,
                name=nombre,
                kind="hardcoded-literal",
                length=len(valor.strip("\"'")),
            )
        )
    return hits


def _hits_forma(relativa: str, texto: str) -> list[SecretHit]:
    """Tokens reconocibles por su forma, con o sin nombre de variable."""
    hits: list[SecretHit] = []
    for numero, linea in enumerate(texto.splitlines(), start=1):
        if _LINEA_DE_PATRON.search(linea):
            # Es la definicion de un detector, no una credencial.
            continue
        for forma, patron in _FORMAS_TOKEN:
            for coincidencia in patron.finditer(linea):
                crudo = (
                    coincidencia.groupdict().get("value")
                    or coincidencia.group(0)
                )
                if es_placeholder(crudo):
                    continue
                hits.append(
                    SecretHit(
                        path=relativa,
                        line=numero,
                        name=forma.replace("-", "_").upper(),
                        kind=forma,
                        length=len(crudo),
                    )
                )
    return hits


def scan_secrets(root: Path) -> list[SecretHit]:
    """Todas las credenciales detectadas en el arbol, sin sus valores."""
    raiz = Path(root)
    hits: list[SecretHit] = []

    for ruta in _archivos(raiz):
        relativa = _rel(raiz, ruta)
        try:
            texto = ruta.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        plantilla = ruta.name in _ARCHIVOS_PLANTILLA

        # En un archivo de ejemplo solo cuenta una forma de token real: sus
        # nombres de credencial con valores de plantilla son el punto.
        if not plantilla:
            if ruta.suffix == ".py":
                hits.extend(_hits_python(relativa, texto))
            else:
                hits.extend(_hits_texto(relativa, texto))

        hits.extend(_hits_forma(relativa, texto))

    # Deduplica: una misma ubicacion puede casar por nombre y por forma.
    unicos = {(h.path, h.line, h.name): h for h in hits}
    return sorted(unicos.values(), key=lambda h: (h.path, h.line, h.name))


@check("secret-leakage", Category.SECRET_LEAKAGE)
def check_secrets(root: Path) -> tuple[list[Finding], int]:
    """Reporta credenciales literales en fuente versionada. Siempre S1."""
    raiz = Path(root)
    archivos = _archivos(raiz)
    hits = scan_secrets(raiz)

    hallazgos: list[Finding] = []
    for hit in hits:
        fixture = hit.path.startswith(_PREFIJOS_FIXTURE)
        hallazgos.append(
            Finding(
                category=Category.SECRET_LEAKAGE,
                severity=Severity.S2 if fixture else Severity.S1,
                evidence=[
                    Evidence(
                        path=hit.path,
                        line=hit.line,
                        key=hit.name,
                        # `SecretHit.quote` se construye a mano y nunca
                        # contiene el valor; `redact` es el segundo cinturon.
                        quote=redact(hit.quote),
                    )
                ],
                impact=(
                    f"`{hit.name}` esta escrita como literal en "
                    f"{hit.path}:{hit.line} ({hit.kind}). Una credencial "
                    "commiteada ya esta comprometida: vive en el historial de "
                    "git, en los clones y en los artifacts de CI aunque se "
                    "borre del archivo. El valor NO se reproduce aqui."
                    + (
                        " Vive en la superficie de fixtures, asi que lo mas "
                        "probable es que sea un literal sintetico; confirmarlo "
                        "antes de descartarlo."
                        if fixture
                        else ""
                    )
                ),
                remediation=(
                    f"Rotar la credencial en el proveedor, moverla a un secret "
                    f"de GitHub Actions y leerla con `env_str(\"{hit.name}\")` "
                    "desde `config/settings.py`. Purgar el valor del historial "
                    "solo despues de rotarlo."
                ),
                effort="M",
                # Un literal sintetico y una llave real son indistinguibles por
                # contenido: el operador confirma cual es cual.
                uncertain=fixture,
            )
        )

    # Ubicaciones inspeccionadas: archivos leidos. Nunca es 0 sobre un arbol
    # real, asi que "0 hallazgos" no puede confundirse con "no corrio".
    inspeccionadas = len(archivos)

    hallazgos.sort(key=lambda f: (f.severity.value, f.id))
    return hallazgos, inspeccionadas
