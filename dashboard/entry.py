"""
Entrypoint para el ejecutable (PyInstaller).

Cuando el programa se congela a .exe, config/settings.py ya no puede
encontrar el .env por rutas del repo. Este entrypoint carga el .env que
vive JUNTO AL EXE antes de importar nada de config.

Desde v1.6.0 (25-sep-26) el .exe tiene dos modos (dashboard/supervisor.py):
  BettingDashboard.exe          supervisor: abre el navegador y mantiene vivo
                                el servidor (lo vuelve a abrir si se cae o se
                                cuelga). Es lo que lanza el acceso directo.
  BettingDashboard.exe --serve  el servidor web (lo lanza el supervisor).
"""

import os
import sys
import time
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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


def _serve():
    import logging
    # Sin una línea por petición en la consola: si la consola se pausa, cada
    # escritura bloquea la respuesta y la página se queda esperando.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    from dashboard.app import app
    from dashboard.supervisor import PORT
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


def _supervise():
    from dashboard import supervisor as sv
    lock = sv.single_instance()          # vive mientras viva este proceso
    if lock is None:
        # Instancia única (fix 17-sep-26): cada doble clic acumulaba una
        # instancia zombi peleándose por el puerto. Ahora abre el navegador
        # hacia el que ya corre y esta ventana se cierra sola.
        print("  Ya hay un dashboard corriendo — abriendo el navegador...")
        sv.open_browser()
        time.sleep(5)
        return
    print(f"  Dashboard: {sv.URL}")
    print("  Se mantiene abierto solo: si el servidor se cae o se cuelga, se vuelve a abrir.")
    print("  Cierra esta ventana para apagar el dashboard.")
    sv.log_event("Supervisor iniciado")
    sv.Supervisor().run()


if __name__ == "__main__":
    # Consola Windows usa cp1252 y no soporta acentos/emojis del output
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _load_env_beside_exe()
    from dashboard.supervisor import quiet_console
    quiet_console()
    if "--serve" in sys.argv[1:]:
        _serve()
    else:
        _supervise()
