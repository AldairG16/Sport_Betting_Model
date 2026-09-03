"""Checker de violaciones a las seis convenciones documentadas en CLAUDE.md.

Cada convencion es una regla independiente (`ConventionRule`) con su propio
`rule_id`, para que un falso positivo en una se pueda afinar sin tocar las
otras cinco. Las seis:

1. `env-accessor`     — usar `env_int`/`env_float`/`env_str` en vez de leer
                         `os.environ` crudo (la "GH Actions secrets expand to
                         empty string" de CLAUDE.md).
2. `edge-market`       — filtrar apuestas por `edge_market`, nunca por `edge`
                         crudo (`edge = prob*odds - 1` deja pasar longshots).
3. `ah-group`          — mapear un mercado de Asian Handicap a su grupo con
                         `_ah_group()` antes de indexar `MIN_EDGE_BY_MARKET` /
                         `calibration_factors`.
4. `distinct-on-upcoming` — leer `upcoming_matches` con
                         `DISTINCT ON (...) ORDER BY updated_at DESC` (la
                         tabla acumula duplicados stale).
5. `normalize-team`    — pasar nombres de equipo por `normalize_team()` antes
                         de una consulta contra `matches`/`upcoming_matches`.
6. `off-peak-cron`     — un cron sub-horario (`*/N` o lista de minutos) nunca
                         debe caer en un minuto pico (`:00/:15/:30/:45`).

Todo el escaneo es TEXTUAL (regex sobre el codigo fuente), en la misma linea
que `tools/audit/sqlscan.py`: el SQL y las claves de diccionario viven dentro
de literales, y `ast` no ve dentro de un string. Solo stdlib + PyYAML (via
`tools.audit.workflows`). Este modulo NUNCA importa `config.settings`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tools.audit.graph import iter_python_files
from tools.audit.model import Category, Evidence, Finding, Severity
from tools.audit.redact import redact
from tools.audit.registry import check
from tools.audit.workflows import read_workflows

__all__ = [
    "ConventionRule",
    "RULES",
    "check_conventions",
]


# ---------------------------------------------------------------------------
# Alcance y utilidades compartidas
# ---------------------------------------------------------------------------

# El auditor y su propia suite no son codigo de produccion: sus patrones
# locales no violan ninguna convencion del sistema de apuestas.
_PREFIJOS_EXENTOS = ("tools/", "tests/")

# Fuente unica de verdad de los accesores de entorno: sus lecturas crudas
# SON la implementacion de `env_int`/`env_float`/`env_str`, no una violacion.
_RUTA_SETTINGS = "config/settings.py"


def _rel(root: Path, ruta: Path) -> str:
    """Ruta relativa a la raiz, siempre con `/` (identica en Windows y ubuntu)."""
    return ruta.relative_to(root).as_posix()


def _texto(ruta: Path) -> str:
    try:
        return ruta.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _linea_de(texto: str, offset: int) -> int:
    """Numero de linea (1-based) de un offset de caracter dentro de `texto`."""
    return texto.count("\n", 0, offset) + 1


def _cita(texto: str, numero: int) -> str:
    """Texto redactado de una linea concreta, listo para el artefacto."""
    lineas = texto.splitlines()
    if 1 <= numero <= len(lineas):
        return redact(lineas[numero - 1].strip())[:200]
    return ""


def _archivos_produccion(root: Path) -> list[Path]:
    """Todo `.py` del arbol salvo el propio auditor y su suite de tests."""
    raiz = Path(root)
    return [
        ruta
        for ruta in iter_python_files(raiz)
        if not _rel(raiz, ruta).startswith(_PREFIJOS_EXENTOS)
    ]


# ---------------------------------------------------------------------------
# Regla 1: env-accessor
# ---------------------------------------------------------------------------

_PATRON_ENV_CRUDO = re.compile(
    r"\bos\.environ\.get\(|\bos\.getenv\(|\bos\.environ\[|\benviron\.get\("
)


def _regla_env_accessor(root: Path) -> tuple[list[Finding], int]:
    """Lecturas de `os.environ` fuera de `config/settings.py`.

    `config/settings.py` queda excluido por completo: sus propias lecturas
    crudas SON el cuerpo de `env_int`/`env_float`/`env_str`, no una violacion
    de la convencion que ellos mismos definen.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    for ruta in _archivos_produccion(raiz):
        relativa = _rel(raiz, ruta)
        if relativa == _RUTA_SETTINGS:
            continue
        texto = _texto(ruta)
        inspeccionadas += 1
        for coincidencia in _PATRON_ENV_CRUDO.finditer(texto):
            numero = _linea_de(texto, coincidencia.start())
            hallazgos.append(
                Finding(
                    category=Category.CONVENTION_VIOLATION,
                    severity=Severity.S2,
                    evidence=[
                        Evidence(
                            path=relativa,
                            line=numero,
                            key="env-accessor",
                            quote=_cita(texto, numero),
                        )
                    ],
                    impact=(
                        f"`{relativa}:{numero}` lee `os.environ` directamente "
                        "en vez de usar `env_int`/`env_float`/`env_str` de "
                        f"`{_RUTA_SETTINGS}`. Un secreto de GitHub Actions sin "
                        "configurar expande a cadena vacia y una coercion "
                        "cruda (`int('')`) revienta el pipeline antes de "
                        "escribir nada."
                    ),
                    remediation=(
                        "Reemplazar la lectura cruda por el accesor "
                        f"correspondiente de `{_RUTA_SETTINGS}`."
                    ),
                    effort="S",
                )
            )

    return hallazgos, inspeccionadas


# ---------------------------------------------------------------------------
# Regla 2: edge-market
# ---------------------------------------------------------------------------

# Coincide con `["edge"]`/['edge'] o `.get("edge"...)` — exactamente, nunca
# `edge_market` ni `edge_ev`: las comillas de cierre pegadas a `edge` impiden
# que un sufijo mas largo haga match parcial.
_PATRON_EDGE_CRUDO = re.compile(r"\[\s*['\"]edge['\"]\s*\]|\.get\(\s*['\"]edge['\"]\s*[,)]")
_PATRON_COMPARADOR = re.compile(r">=|<=|==|!=|(?<![<>=!])[<>](?!=)")


def _regla_edge_market(root: Path) -> tuple[list[Finding], int]:
    """`bet["edge"]` usado como filtro en vez de `bet["edge_market"]`.

    `edge = prob*odds - 1` (EV) no es el edge real; `edge_market = prob -
    1/odds` si lo es. Filtrar sobre el primero dejo pasar longshots y causo
    una semana de -23% ROI (CLAUDE.md). Solo se acusa cuando la clave cruda
    aparece en la MISMA linea que un operador de comparacion: una asignacion
    simple (`bet["edge"] = ...`) no es un filtro.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    for ruta in _archivos_produccion(raiz):
        relativa = _rel(raiz, ruta)
        texto = _texto(ruta)
        lineas = texto.splitlines()
        inspeccionadas += 1
        for coincidencia in _PATRON_EDGE_CRUDO.finditer(texto):
            numero = _linea_de(texto, coincidencia.start())
            linea_texto = lineas[numero - 1] if 1 <= numero <= len(lineas) else ""
            if not _PATRON_COMPARADOR.search(linea_texto):
                continue
            hallazgos.append(
                Finding(
                    category=Category.CONVENTION_VIOLATION,
                    severity=Severity.S1,
                    evidence=[
                        Evidence(
                            path=relativa,
                            line=numero,
                            key="edge-market",
                            quote=_cita(texto, numero),
                        )
                    ],
                    impact=(
                        f"`{relativa}:{numero}` filtra sobre `edge` (EV crudo) "
                        "en vez de `edge_market` (el edge real). Deja pasar "
                        "apuestas de longshot que no deberian calificar y "
                        "puede repetir la semana de -23% ROI documentada en "
                        "CLAUDE.md."
                    ),
                    remediation="Filtrar sobre `bet[\"edge_market\"]`, no sobre `bet[\"edge\"]`.",
                    effort="S",
                )
            )

    return hallazgos, inspeccionadas


# ---------------------------------------------------------------------------
# Regla 3: ah-group
# ---------------------------------------------------------------------------

# Indexado por variable (no por literal) de un diccionario de Asian Handicap.
# El grupo capturado exige un identificador que EMPIECE con letra/guion bajo:
# una clave literal ('ah_home_fav') arranca con comilla y nunca cae en este
# grupo, que es exactamente la distincion que importa.
_PATRON_DICT_AH = re.compile(
    r"\b(?:MIN_EDGE_BY_MARKET|\w*calibration_factors\w*)\s*"
    r"(?:\[\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\]"
    r"|\.get\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*[,)])",
    re.IGNORECASE,
)
_PATRON_AH_GROUP_CALL = re.compile(r"\b_ah_group\s*\(")


def _regla_ah_group(root: Path) -> tuple[list[Finding], int]:
    """Un mercado AH indexando `MIN_EDGE_BY_MARKET`/`calibration_factors` sin pasar por `_ah_group()`.

    La clave real es `ah_home_-0.5`; los diccionarios estan agrupados por
    `ah_home_fav`/`ah_home_pk`/`ah_home_dog`. Sin `_ah_group()` un
    `dict.get(mkt, default)` cae en el default para TODA apuesta de AH en
    silencio — el bug historico que motivo `ah_home_fav`/`ah_away_fav` con
    factor ~0.78 en mayo. Si el ARCHIVO llama a `_ah_group()` en algun lado
    se asume que la resolucion ya paso por ahi.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    for ruta in _archivos_produccion(raiz):
        relativa = _rel(raiz, ruta)
        texto = _texto(ruta)
        inspeccionadas += 1
        if _PATRON_AH_GROUP_CALL.search(texto):
            continue
        for coincidencia in _PATRON_DICT_AH.finditer(texto):
            clave = coincidencia.group(1) or coincidencia.group(2)
            if not clave:
                continue
            numero = _linea_de(texto, coincidencia.start())
            hallazgos.append(
                Finding(
                    category=Category.CONVENTION_VIOLATION,
                    severity=Severity.S1,
                    evidence=[
                        Evidence(
                            path=relativa,
                            line=numero,
                            key="ah-group",
                            quote=_cita(texto, numero),
                        )
                    ],
                    impact=(
                        f"`{relativa}:{numero}` indexa un diccionario por "
                        "mercado con una variable sin pasar antes por "
                        "`_ah_group()`. Para cualquier mercado de Asian "
                        "Handicap (`ah_home_-0.5`) la clave real no existe en "
                        "el diccionario y `.get()`/`[]` cae en el default en "
                        "silencio para toda la familia de mercados AH."
                    ),
                    remediation=(
                        "Resolver la clave con `_ah_group()` de "
                        "`src/models/calibration_monitor.py` antes de indexar."
                    ),
                    effort="S",
                )
            )

    return hallazgos, inspeccionadas


# ---------------------------------------------------------------------------
# Regla 4: distinct-on-upcoming
# ---------------------------------------------------------------------------

_PATRON_SELECT_UPCOMING = re.compile(
    r"(?is)\bselect\b.{0,600}?\bfrom\s+upcoming_matches\b.{0,300}?"
    r"(?=;|\"\"\"|'''|$)"
)


def _regla_distinct_on_upcoming(root: Path) -> tuple[list[Finding], int]:
    """Un `SELECT ... FROM upcoming_matches` sin `DISTINCT ON`.

    El upsert de `upcoming_matches` incluye `match_day` en su clave, asi que
    cuando la API corrige un kickoff se inserta una fila nueva en vez de
    actualizar la vieja. Cualquier lector tiene que usar
    `DISTINCT ON (home_team_norm, away_team_norm, sport_key) ... ORDER BY
    updated_at DESC` o puede leer una fila stale.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    for ruta in _archivos_produccion(raiz):
        relativa = _rel(raiz, ruta)
        texto = _texto(ruta)
        inspeccionadas += 1
        for coincidencia in _PATRON_SELECT_UPCOMING.finditer(texto):
            bloque = coincidencia.group(0)
            if "distinct on" in bloque.lower():
                continue
            numero = _linea_de(texto, coincidencia.start())
            hallazgos.append(
                Finding(
                    category=Category.CONVENTION_VIOLATION,
                    severity=Severity.S1,
                    evidence=[
                        Evidence(
                            path=relativa,
                            line=numero,
                            key="distinct-on-upcoming",
                            quote=_cita(texto, numero),
                        )
                    ],
                    impact=(
                        f"`{relativa}:{numero}` lee `upcoming_matches` sin "
                        "`DISTINCT ON ... ORDER BY updated_at DESC`. La tabla "
                        "acumula duplicados stale cuando la API corrige un "
                        "kickoff, y este SELECT puede devolver la fila vieja."
                    ),
                    remediation=(
                        "Agregar `DISTINCT ON (home_team_norm, away_team_norm, "
                        "sport_key) ... ORDER BY updated_at DESC`, igual que "
                        "en `prediction_pipeline.py`."
                    ),
                    effort="S",
                )
            )

    return hallazgos, inspeccionadas


# ---------------------------------------------------------------------------
# Regla 5: normalize-team
# ---------------------------------------------------------------------------

_PATRON_TABLA_EQUIPOS = re.compile(r"(?i)\bfrom\s+(?:upcoming_)?matches\b")
_PATRON_WHERE_EQUIPO = re.compile(
    r"(?is)\bwhere\b.{0,250}?\b(?:home_team|away_team|team)\w*\s*(?:=|in\b|ilike\b|like\b)"
)
_PATRON_NORMALIZE_CALL = re.compile(r"\bnormalize_team\s*\(")


def _regla_normalize_team(root: Path) -> tuple[list[Finding], int]:
    """Una consulta contra `matches`/`upcoming_matches` filtrada por equipo sin `normalize_team()` en el archivo.

    `matches` y `upcoming_matches` guardan nombres normalizados en minuscula;
    un mismatch deja bets varados en `pending` para siempre aunque
    `fetch_results` ya haya corrido. Regla a nivel de archivo (no de funcion):
    si el archivo consulta por equipo Y en ningun lado llama a
    `normalize_team()`, el archivo entero corre el riesgo.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    for ruta in _archivos_produccion(raiz):
        relativa = _rel(raiz, ruta)
        texto = _texto(ruta)
        inspeccionadas += 1
        if not _PATRON_TABLA_EQUIPOS.search(texto):
            continue
        if _PATRON_NORMALIZE_CALL.search(texto):
            continue
        coincidencia = _PATRON_WHERE_EQUIPO.search(texto)
        if coincidencia is None:
            continue
        numero = _linea_de(texto, coincidencia.start())
        hallazgos.append(
            Finding(
                category=Category.CONVENTION_VIOLATION,
                severity=Severity.S2,
                evidence=[
                    Evidence(
                        path=relativa,
                        line=numero,
                        key="normalize-team",
                        quote=_cita(texto, numero),
                    )
                ],
                impact=(
                    f"`{relativa}` consulta `matches`/`upcoming_matches` "
                    "filtrando por equipo pero nunca llama a "
                    "`normalize_team()`. Un mismatch de nombre deja bets "
                    "atascadas en `pending` para siempre aunque el resultado "
                    "ya este disponible."
                ),
                remediation=(
                    "Pasar el nombre del equipo por `normalize_team()` "
                    "(`src/utils/team_normalizer.py`) antes de usarlo en el "
                    "filtro de la consulta."
                ),
                effort="S",
            )
        )

    return hallazgos, inspeccionadas


# ---------------------------------------------------------------------------
# Regla 6: off-peak-cron
# ---------------------------------------------------------------------------

# GH Actions descarta programaciones `*/N` que caen en un minuto pico.
_MINUTOS_PICO = frozenset({0, 15, 30, 45})


def _minutos_declarados(campo_minuto: str) -> list[int]:
    """Minutos concretos que dispara el campo de minuto de un cron.

    Solo resuelve las dos formas sub-horarias que usa este repositorio:
    `*/N` (paso fijo) y una lista `a,b,c` de minutos explicitos.
    """
    campo = campo_minuto.strip()
    paso = re.fullmatch(r"\*/(\d+)", campo)
    if paso:
        n = int(paso.group(1))
        return list(range(0, 60, n)) if n > 0 else []
    if "," in campo:
        return [int(p) for p in campo.split(",") if p.strip().isdigit()]
    if campo.isdigit():
        return [int(campo)]
    return []


def _es_sub_horaria(campo_minuto: str) -> bool:
    """True si el campo de minuto dispara mas de una vez por hora."""
    campo = campo_minuto.strip()
    if campo == "*":
        return True
    if re.fullmatch(r"\*/(\d+)", campo):
        return True
    return "," in campo


def _regla_off_peak_cron(root: Path) -> tuple[list[Finding], int]:
    """Un cron sub-horario que cae en un minuto pico (`:00/:15/:30/:45`).

    `pre_kickoff.yml` (`7,22,37,52 * * * *`) es la referencia conforme. Un
    `*/15` cae en 0/15/30/45 SIEMPRE: es exactamente el patron que GH Actions
    descarta, y el sintoma es un heartbeat con `bets_found > 0,
    bets_analyzed = 0` durante horas sin que nadie lo note.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    for info in read_workflows(raiz):
        for cron in info.crons:
            inspeccionadas += 1
            campos = cron.split()
            if not campos:
                continue
            campo_minuto = campos[0]
            if not _es_sub_horaria(campo_minuto):
                continue
            en_pico = sorted(
                m for m in _minutos_declarados(campo_minuto) if m in _MINUTOS_PICO
            )
            if not en_pico:
                continue
            hallazgos.append(
                Finding(
                    category=Category.CONVENTION_VIOLATION,
                    severity=Severity.S2,
                    evidence=[
                        Evidence(path=info.path, key=f"cron:{cron}")
                    ],
                    impact=(
                        f"`{info.path}` programa `{cron}`, sub-horario y con "
                        f"minuto(s) pico {en_pico}. GitHub Actions descarta "
                        "programaciones `*/N` que caen en `:00/:15/:30/:45`; "
                        "el sintoma es un heartbeat con actividad detectada "
                        "pero nunca procesada."
                    ),
                    remediation=(
                        "Usar minutos fuera de pico, como "
                        f"`{info.path}` deberia imitar a `pre_kickoff.yml` "
                        "(`7,22,37,52 * * * *`)."
                    ),
                    effort="S",
                )
            )

    return hallazgos, inspeccionadas


# ---------------------------------------------------------------------------
# Registro de reglas y checker
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConventionRule:
    """Una de las seis convenciones documentadas, con su propio detector.

    `detection` sigue el mismo contrato que un checker registrado:
    `(root) -> (list[Finding], int)`. Cada `Finding` que produce ya trae la
    severidad correcta; `severity` queda aqui ademas como metadato de
    referencia rapida sin tener que abrir cada hallazgo.
    """

    rule_id: str
    description: str
    detection: Callable[[Path], tuple[list[Finding], int]]
    severity: Severity


RULES: list[ConventionRule] = [
    ConventionRule(
        rule_id="env-accessor",
        description=(
            "Usar env_int/env_float/env_str de config/settings.py; nunca "
            "os.environ crudo fuera de esos accesores."
        ),
        detection=_regla_env_accessor,
        severity=Severity.S2,
    ),
    ConventionRule(
        rule_id="edge-market",
        description="Filtrar apuestas por edge_market, nunca por edge (EV crudo).",
        detection=_regla_edge_market,
        severity=Severity.S1,
    ),
    ConventionRule(
        rule_id="ah-group",
        description=(
            "Resolver un mercado de Asian Handicap con _ah_group() antes de "
            "indexar MIN_EDGE_BY_MARKET/calibration_factors."
        ),
        detection=_regla_ah_group,
        severity=Severity.S1,
    ),
    ConventionRule(
        rule_id="distinct-on-upcoming",
        description=(
            "Leer upcoming_matches con DISTINCT ON (...) ORDER BY updated_at "
            "DESC para evitar duplicados stale."
        ),
        detection=_regla_distinct_on_upcoming,
        severity=Severity.S1,
    ),
    ConventionRule(
        rule_id="normalize-team",
        description=(
            "Pasar nombres de equipo por normalize_team() antes de una "
            "consulta contra matches/upcoming_matches."
        ),
        detection=_regla_normalize_team,
        severity=Severity.S2,
    ),
    ConventionRule(
        rule_id="off-peak-cron",
        description=(
            "Un cron sub-horario nunca debe caer en un minuto pico "
            "(:00/:15/:30/:45)."
        ),
        detection=_regla_off_peak_cron,
        severity=Severity.S2,
    ),
]


@check("convention-violation", Category.CONVENTION_VIOLATION)
def check_conventions(root: Path) -> tuple[list[Finding], int]:
    """Corre las seis reglas de convencion y agrega sus hallazgos.

    Cada regla es independiente: un falso positivo de `off-peak-cron` no
    puede tocar la deteccion de `edge-market`. El orden final es
    determinstico (severidad, luego ID) para que el artefacto sea
    byte-identico entre corridas.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas = 0

    for regla in RULES:
        parciales, contadas = regla.detection(raiz)
        hallazgos.extend(parciales)
        inspeccionadas += contadas

    hallazgos.sort(key=lambda hallazgo: (hallazgo.severity.value, hallazgo.id))
    return hallazgos, inspeccionadas
