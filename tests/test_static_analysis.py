"""
Gate de análisis estático (pyflakes) sobre el código de producción Y los
tests.

pyflakes no opina de estilo: solo reporta lo que casi siempre es un error
— nombres sin definir, imports o variables sin uso, redefiniciones,
f-strings sin marcadores. El 22-sep-26 dashboard/app.py tenía 29 nombres
sin definir (NameError en cuanto se usara) y ningún test lo veía. En los
tests, una variable calculada y nunca usada suele ser una aserción que
falta (test_kelly_high_odds_penalized calculaba el stake "normal" y nunca
lo comparaba). La base quedó en cero hallazgos y este test la mantiene así.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PRODUCTION = ("src", "scripts", "config", "dashboard", "tests")


def test_production_code_has_no_pyflakes_findings():
    pytest.importorskip("pyflakes")
    from pyflakes.api import checkPath
    from pyflakes.reporter import Reporter

    class Collect(Reporter):
        def __init__(self):
            self.found = []

        def unexpectedError(self, filename, msg):
            self.found.append(f"{filename}: {msg}")

        def syntaxError(self, filename, msg, lineno, offset, text):
            self.found.append(f"{filename}:{lineno}: {msg}")

        def flake(self, message):
            self.found.append(str(message))

    rep = Collect()
    for pkg in PRODUCTION:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            checkPath(str(path), rep)
    assert not rep.found, "pyflakes:\n" + "\n".join(
        f.replace(str(ROOT) + "\\", "").replace(str(ROOT) + "/", "") for f in rep.found)


def test_no_test_reads_logs_with_caplog():
    """caplog no ve los logs del proyecto en CI (src/utils/log.py configura
    sus propios handlers): dos pruebas pasaron en local y fallaron en CI
    (PR #85 y #89). Los avisos se leen de RUN_ISSUES.errors() / .warnings(),
    que es además lo que llega a Telegram."""
    import re
    uses = [f"{p.name}: {m.group(1)}" for p in sorted((ROOT / "tests").glob("test_*.py"))
            for m in re.finditer(r"def (test_\w+)\([^)]*\bcaplog\b", p.read_text(encoding="utf-8"))]
    assert uses == []
