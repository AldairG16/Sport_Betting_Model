"""Redaccion de secretos para todo lo que se escriba a un artefacto.

El checker de `secret-leakage` reporta la UBICACION de una credencial, nunca
su VALOR: el reporte se commitea a `audits/` y se sube como artifact de CI,
asi que filtrar el valor ahi convertiria al auditor en el peor leak del repo.

Solo stdlib. Este modulo NUNCA debe importar `config.settings`.
"""

from __future__ import annotations

import re

__all__ = ["REDACTED", "redact"]

REDACTED = "<redacted>"

# Nombres de variable que denotan una credencial. El sufijo se ancla al final
# del identificador para que `API_KEY`, `TELEGRAM_BOT_TOKEN` o `db_password`
# entren, pero `KEYWORDS` o `TOKENIZER` no.
_SENSITIVE_NAME = (
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS|CREDENTIAL|AUTH)"
)

# `NAME = valor`, `"name": "valor"`, `NAME=valor`. Solo se captura el valor;
# el nombre se conserva intacto porque es justamente lo que hace accionable
# al hallazgo.
_ASSIGN_RE = re.compile(
    r"(?P<name>" + _SENSITIVE_NAME + r")"
    r"(?P<nclose>['\"]?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<open>['\"]?)"
    r"(?P<value>[^\s'\",;)\]}]+)",
    re.IGNORECASE,
)

# Credenciales embebidas en una cadena de conexion: `scheme://user:pass@host`.
_CONN_CRED_RE = re.compile(r"(://[^\s:/@]+:)(?P<value>[^\s@/]+)(@)")

# Formas de token reconocibles aunque no haya un nombre de variable alrededor.
# NOTA: a proposito NO se redacta "cualquier hex largo" — un SHA de commit de
# 40 hex es un dato legitimo del reporte (`AuditRun.commit_sha`) y borrarlo
# haria el diff corrida-a-corrida ilegible.
_TOKEN_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Token de bot de Telegram: `<bot_id>:<secreto>`.
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}"),
    re.compile(r"(?<=Bearer )[A-Za-z0-9._\-]{16,}"),
)

# Valores que NO son secretos sino referencias al secreto. Redactarlos
# destruiria la evidencia util: `TELEGRAM_BOT_TOKEN = env_str("...")` es
# precisamente la linea correcta que el auditor quiere poder citar.
_REFERENCE_PREFIXES = (
    "os.",
    "env_",
    "self.",
    "settings.",
    "config.",
    "${",
    "<",
)
_NON_SECRET_VALUES = {"none", "true", "false", "null", "''", '""'}


def _is_reference(value: str) -> bool:
    """True si el valor es una referencia/placeholder y no un secreto real."""
    stripped = value.strip()
    if not stripped:
        return True
    if stripped.lower() in _NON_SECRET_VALUES:
        return True
    return stripped.startswith(_REFERENCE_PREFIXES)


def _mask_assignment(match: re.Match[str]) -> str:
    if _is_reference(match.group("value")):
        return match.group(0)
    # La comilla de cierre no se consume, asi que sobrevive intacta despues
    # del reemplazo: `KEY="abc"` -> `KEY="<redacted>"`.
    return (
        f"{match.group('name')}{match.group('nclose')}"
        f"{match.group('sep')}{match.group('open')}{REDACTED}"
    )


def _mask_conn(match: re.Match[str]) -> str:
    if _is_reference(match.group("value")):
        return match.group(0)
    return f"{match.group(1)}{REDACTED}{match.group(3)}"


def redact(text: str) -> str:
    """Sustituye cualquier cosa con forma de llave/token/password por `<redacted>`.

    Conserva el nombre de la variable y el resto de la linea, que es lo que
    hace ubicable al hallazgo.

    >>> redact('ODDS_API_KEY = "a1b2c3d4e5f6g7h8"')
    'ODDS_API_KEY = "<redacted>"'
    """
    if not text:
        return text

    result = _CONN_CRED_RE.sub(_mask_conn, text)
    result = _ASSIGN_RE.sub(_mask_assignment, result)
    for token_re in _TOKEN_RES:
        result = token_re.sub(REDACTED, result)
    return result
