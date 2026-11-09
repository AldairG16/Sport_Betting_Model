"""
tests/test_db_usage.py
======================
Lo que gasta la base (9-nov-26). Ese día Neon cortó la base por pasarse de la
cuota del plan gratis ("Your account or project has exceeded the quota").
Dos cosas la gastaban sin necesidad:

- La fusión de repetidos (PR #124) leía los ~120 mil partidos en cada corrida
  de resultados. Ahora lee completos los últimos 60 días, y de antes solo los
  candidatos (tests/test_sanity_audit.py).
- El dashboard recargaba los datos cada 5 min aunque la pestaña estuviera en
  segundo plano. Neon apaga la base tras 5 min sin consultas, así que con la
  página abierta todo el día la base no se apagaba nunca.
"""

from pathlib import Path

JS = (Path(__file__).parent.parent / "dashboard" / "ui_es5.js").read_text(encoding="utf-8")


def test_the_dashboard_only_refreshes_while_the_page_is_visible():
    assert "setInterval(function(){ refreshAll(false); }, 5 * 60000)" not in JS
    assert "if (document.hidden ||" in JS
    assert "document.addEventListener('visibilitychange', autoRefresh)" in JS
    assert "setInterval(autoRefresh, AUTO_MS)" in JS


def test_the_heartbeat_does_not_touch_the_database():
    import inspect
    from dashboard import app as A
    src = inspect.getsource(A.api_health)
    assert "_q(" not in src and "engine" not in src and "read_sql" not in src
