"""
dashboard/supervisor.py
=======================
Mantiene vivo el servidor del dashboard (v1.6.0, 25-sep-26).

El .exe arranca como SUPERVISOR: levanta el servidor (el mismo .exe con
--serve) como proceso hijo, le pregunta cada CHECK_EVERY_S si responde
(/api/health) y lo vuelve a levantar si se cerró o si deja de responder
MAX_FAILS veces seguidas (~45 s). Antes, si el servidor moría o se colgaba
(24-sep: Windows lo cerró con el evento 1002 al terminar otra sesión), la
página mostraba "HTTP 0" hasta que alguien lo abría a mano.

Solo mata procesos que son del dashboard: su propio hijo (con todo su
árbol: el .exe de un solo archivo son dos procesos) o, si el puerto lo
ocupa un servidor colgado de una versión anterior, ese proceso solo si su
imagen es BettingDashboard.exe.
"""

import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# DASHBOARD_PORT (en el .env junto al .exe) solo si el 5050 lo usa otro programa
PORT = int(os.environ.get("DASHBOARD_PORT") or 5050)
URL = f"http://127.0.0.1:{PORT}"
HEALTH_URL = URL + "/api/health"
CHECK_EVERY_S = 15
HEALTH_TIMEOUT_S = 10
STARTUP_GRACE_S = 90          # el .exe de un solo archivo se descomprime al arrancar
MAX_FAILS = 3                 # 3 × 15 s sin responder = colgado
BACKOFF_S = (5, 15, 30, 60)   # espera antes de relanzar tras cierres seguidos
IMAGE_NAME = "BettingDashboard.exe"
MUTEX_NAME = "Local\\BettingDashboardSupervisor"
LOG_NAME = "dashboard_supervisor.log"


# ─────────────────────────────────────────────────────────────────────────────
# Piezas del sistema (se sustituyen en las pruebas)
# ─────────────────────────────────────────────────────────────────────────────

def base_dir() -> Path:
    """Carpeta del .exe (o del repo en desarrollo): ahí vive el .env y el log."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def log_event(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(f"  {line}", flush=True)
    try:
        with open(base_dir() / LOG_NAME, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def healthy(timeout: float = HEALTH_TIMEOUT_S) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def port_busy(port: int = PORT) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def server_cmd() -> list:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--serve"]
    return [sys.executable, str(Path(__file__).resolve().parent / "entry.py"), "--serve"]


def spawn():
    return subprocess.Popen(server_cmd(), cwd=str(base_dir()))


def kill_tree(pid: int) -> None:
    """Termina un proceso con TODO su árbol (el .exe de un solo archivo es
    un lanzador + el Python real: matar solo el primero deja el puerto tomado)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def listening_pid(port: int = PORT) -> int | None:
    """PID que escucha en el puerto (solo Windows; None si no se sabe). Se
    reconoce por la dirección remota vacía, no por la palabra LISTENING, que
    Windows traduce ("ESCUCHANDO" en español)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, check=False).stdout
    except OSError:
        return None
    return parse_listening_pid(out, port)


def parse_listening_pid(netstat_out: str, port: int = PORT) -> int | None:
    for line in netstat_out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[1].endswith(f":{port}")
                and parts[2] in ("0.0.0.0:0", "[::]:0") and parts[-1].isdigit()):
            return int(parts[-1])
    return None


def image_name(pid: int) -> str:
    if sys.platform != "win32":
        return ""
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, check=False).stdout
    first = out.strip().split(",")[0].strip('"') if out.strip() else ""
    return first


def open_browser() -> None:
    import webbrowser
    webbrowser.open(URL)


def quiet_console() -> None:
    """Quita la "edición rápida" de la consola de Windows: con ella, un clic
    en la ventana pausa toda escritura del programa y el servidor se congela
    hasta que alguien presiona una tecla. Solo afecta a esta ventana."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)                 # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
            k32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
    except Exception:
        pass


def single_instance():
    """Candado de un solo supervisor por sesión de Windows: el handle del
    mutex vive mientras viva el proceso. None si ya hay otro supervisor."""
    if sys.platform != "win32":
        return object()
    import ctypes
    k32 = ctypes.windll.kernel32
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    ERROR_ALREADY_EXISTS = 183
    if not handle or k32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


# ─────────────────────────────────────────────────────────────────────────────
# La lógica (probada sin procesos reales)
# ─────────────────────────────────────────────────────────────────────────────

class Supervisor:
    def __init__(self, spawn=spawn, is_healthy=healthy, is_port_busy=port_busy,
                 kill=kill_tree, find_listener=listening_pid, image_of=image_name,
                 browser=open_browser, log=log_event, sleep=time.sleep, clock=time.monotonic):
        self.spawn, self.is_healthy, self.is_port_busy = spawn, is_healthy, is_port_busy
        self.kill, self.find_listener, self.image_of = kill, find_listener, image_of
        self.browser, self.log, self.sleep, self.clock = browser, log, sleep, clock
        self.child = None
        self.started_at = None
        self.fails = 0
        self.crashes_in_row = 0
        self.browser_opened = False

    def _in_grace(self) -> bool:
        return (self.child is not None and self.started_at is not None
                and self.clock() - self.started_at < STARTUP_GRACE_S)

    def _stop_child(self) -> None:
        if self.child is not None:
            self.kill(self.child.pid)
            self.child = None

    def _stop_foreign_server(self) -> None:
        """El puerto lo ocupa un servidor que no es nuestro hijo y no responde
        (p. ej. un .exe anterior colgado): se cierra solo si es el dashboard."""
        pid = self.find_listener()
        if pid and self.image_of(pid).lower() == IMAGE_NAME.lower():
            self.log(f"Servidor anterior colgado (PID {pid}) — se cierra")
            self.kill(pid)
        elif pid:
            self.log(f"El puerto {PORT} lo ocupa otro programa (PID {pid}) — no se toca")

    def step(self) -> None:
        """Una vuelta del ciclo."""
        # 1. el hijo se cerró solo
        if self.child is not None and self.child.poll() is not None:
            self.log(f"El servidor se cerró (código {self.child.returncode})")
            self.child = None
            wait = BACKOFF_S[min(self.crashes_in_row, len(BACKOFF_S) - 1)]
            self.crashes_in_row += 1
            self.log(f"Se vuelve a abrir en {wait} s")
            self.sleep(wait)

        # 2. nadie atiende el puerto: levantar el servidor
        if self.child is None and not self.is_port_busy():
            self.child = self.spawn()
            self.started_at = self.clock()
            self.fails = 0
            self.log(f"Servidor iniciado (PID {self.child.pid})")
            return

        # 3. ¿responde?
        if self.is_healthy():
            self.fails = 0
            self.crashes_in_row = 0
            if not self.browser_opened:
                self.browser()
                self.browser_opened = True
            return
        if self._in_grace():
            return
        self.fails += 1
        if self.fails < MAX_FAILS:
            return
        self.log(f"El servidor no responde hace ~{self.fails * CHECK_EVERY_S} s — se reinicia")
        self.fails = 0
        if self.child is not None:
            self._stop_child()
        else:
            self._stop_foreign_server()

    def shutdown(self) -> None:
        self._stop_child()

    def run(self) -> None:
        try:
            while True:
                self.step()
                self.sleep(CHECK_EVERY_S)
        finally:
            self.shutdown()
