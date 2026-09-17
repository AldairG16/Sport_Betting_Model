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
    _load_env_beside_exe()

    # ── Instancia única (fix 17-sep-26) ──────────────────────────────────
    # Si el puerto ya está atendido, otro dashboard vive: abrir el navegador
    # hacia él y salir limpio. Antes, cada doble clic acumulaba una instancia
    # zombi peleándose por el puerto 5050 y el programa "no abría".
    import socket
    _port_busy = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        _s.settimeout(1.0)
        _port_busy = _s.connect_ex(("127.0.0.1", 5050)) == 0

    if _port_busy:
        print("  Ya hay un dashboard corriendo — abriendo navegador...")
        import webbrowser
        webbrowser.open("http://127.0.0.1:5050")
        print("  (Esa otra ventana de consola es la que puedes cerrar)")
        try:
            input("  Presiona Enter para cerrar esta ventana...")
        except EOFError:
            pass
        sys.exit(0)

    import webbrowser
    webbrowser.open("http://127.0.0.1:5050")
    from dashboard.app import app
    print("  Dashboard: http://127.0.0.1:5050  (Ctrl+C para salir)")
    app.run(host="127.0.0.1", port=5050, debug=False)
