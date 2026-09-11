"""
Lanza el dashboard web local y abre el navegador.

Uso:
    python scripts/run_dashboard.py

Requiere DB_URL en .env (la misma de Neon que usa CI).
Escucha solo en http://127.0.0.1:5050 — nadie fuera de esta PC.
"""

import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DB_URL  # valida que exista .env / entorno

print("=" * 55)
print("  SPORT BETTING MODEL — DASHBOARD LOCAL")
print("=" * 55)
print(f"  DB: {DB_URL.split('@')[-1][:40]}...")
print("  Modo: SOLO LECTURA (ningún dato se modifica)")
print("  URL: http://127.0.0.1:5050")
print("  Cierra esta ventana o Ctrl+C para salir.")
print("=" * 55)

webbrowser.open("http://127.0.0.1:5050")

from dashboard.app import app
app.run(host="127.0.0.1", port=5050, debug=False)
