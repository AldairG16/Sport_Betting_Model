"""
src/utils/log.py
================
Logging con niveles (22-sep-26). La consola muestra EXACTAMENTE el mismo
texto que los print() de antes (formato "%(message)s"), pero cada aviso y
cada error llevan nivel (WARNING / ERROR) y quedan registrados en
RUN_ISSUES durante la corrida.

Por qué: muchos fallos se "toleran" (el código los atrapa, imprime y
sigue). Eso está bien para no tumbar el slate, pero nadie se enteraba: el
apagón de 82 días de The Odds API fue una línea "❌ API error 401" por liga
dentro de pasos que terminaban "OK". Con los niveles, el orchestrator
reporta al final de cada corrida los errores tolerados (Telegram +
anotación en GitHub Actions).

Uso:
    from src.utils.log import get_logger
    log = get_logger(__name__)
    log.warning("⚠️  algo raro pero seguimos")
    log.error("❌ algo falló y lo toleramos")
"""

import logging
import sys

_ROOT = "betting"


class _LiveStdout(logging.StreamHandler):
    """Escribe en el sys.stdout ACTUAL (no el del momento de configurar):
    respeta las redirecciones de pytest y el reconfigure(utf-8) del
    orchestrator."""

    def emit(self, record):
        self.stream = sys.stdout
        super().emit(record)


class RunIssues(logging.Handler):
    """Guarda los WARNING/ERROR emitidos en la corrida."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def errors(self) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= logging.ERROR]

    def warnings(self) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == logging.WARNING]

    def reset(self):
        self.records.clear()


RUN_ISSUES = RunIssues()


def _configure():
    root = logging.getLogger(_ROOT)
    if getattr(root, "_betting_configured", False):
        return root
    root.setLevel(logging.INFO)
    handler = _LiveStdout()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.addHandler(RUN_ISSUES)
    root.propagate = False
    root._betting_configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(f"{_ROOT}.{name}")
