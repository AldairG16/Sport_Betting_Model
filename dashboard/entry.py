"""
Entrypoint para el ejecutable (PyInstaller).

Cuando el programa se congela a .exe, config/settings.py ya no puede
encontrar el .env por rutas del repo. Este entrypoint carga el .env que
vive JUNTO AL EXE antes de importar nada de config.
"""

import os
import sys
from pathlib import Path


def _load_env_beside_exe():
    if not getattr(sys, "frozen", False):
        return
    exe_dir = Path(sys.executable).parent
    env_file = exe_dir / ".env"
    if not env_file.exists():
        print("=" * 55)
        print("  [ERROR] Falta el archivo .env junto al ejecutable")
        print(f"  Esperado en: {env_file}")
        print("  Crea el .env con tu DB_URL de Neon (misma que usa CI).")
        print("=" * 55)
        input("  Presiona Enter para salir...")
        sys.exit(1)
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


if __name__ == "__main__":
    # Consola Windows usa cp1252 y no soporta acentos/emojis del output
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _load_env_beside_exe()
    import webbrowser
    webbrowser.open("http://127.0.0.1:5050")
    from dashboard.app import app
    print("  Dashboard: http://127.0.0.1:5050  (Ctrl+C para salir)")
    app.run(host="127.0.0.1", port=5050, debug=False)
