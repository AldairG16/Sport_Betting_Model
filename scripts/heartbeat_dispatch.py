"""
scripts/heartbeat_dispatch.py
=============================
Dispara los trabajos de horario fijo desde el latido del closing (1-oct-26).

El closing corre cada 30 min (cron-job.org → workflow_dispatch) y es la
corrida más puntual del sistema: 336 corridas sin un hueco > 45 min del
25-sep al 1-oct-26. En cambio, el cron propio de GitHub corrió el reintento de
resultados y el watchdog con 4-6 h de atraso y se saltó algunos, y el
resolvedor de pendientes no tenía quién lo disparara (no corrió del 11-may al
1-oct-26). Al final de cada closing este script dispara:

  late_results.yml     00:00 y 06:30 (hora de México)
  resolve_pending.yml  07:00 y 19:00
  watchdog.yml         03:00, 09:00, 15:00 y 21:00 — además de su cron
                       propio, que se queda como respaldo independiente: si
                       el closing muere, el watchdog sigue y lo detecta.

Cada horario tiene una ventana de 30 min: el primer closing que cae en ella
dispara el trabajo, salvo que ya haya arrancado en los últimos 25 min (el
closing a veces corre 2-3 veces por media hora). Usa el CLI `gh` con el token
del workflow (permiso actions: write). Un disparo fallido deja el closing en
rojo: el watchdog también avisa si alguno de estos trabajos deja de correr.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Mexico_City")
SLOTS = {
    "late_results.yml":    ("00:00", "06:30"),
    "resolve_pending.yml": ("07:00", "19:00"),
    "watchdog.yml":        ("03:00", "09:00", "15:00", "21:00"),
}
WINDOW_MIN = 30
RECENT_MIN = 25


def due(now_local: datetime, slots, window_min: int = WINDOW_MIN) -> bool:
    """True si `now_local` cae dentro de la ventana de algún horario."""
    for s in slots:
        h, m = (int(x) for x in s.split(":"))
        start = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if start <= now_local < start + timedelta(minutes=window_min):
            return True
    return False


def _gh(*args) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def started_recently(workflow: str, now_utc: datetime, gh=_gh) -> bool:
    runs = json.loads(gh("run", "list", "--workflow", workflow, "--limit", "1",
                         "--json", "createdAt") or "[]")
    if not runs:
        return False
    created = datetime.fromisoformat(runs[0]["createdAt"].replace("Z", "+00:00"))
    return now_utc - created < timedelta(minutes=RECENT_MIN)


def main(now_utc: datetime | None = None, gh=_gh) -> int:
    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)
    failures = 0
    for workflow, slots in SLOTS.items():
        if not due(now_local, slots):
            continue
        try:
            if started_recently(workflow, now_utc, gh):
                print(f"   {workflow}: ya arrancó hace < {RECENT_MIN} min — no se duplica")
                continue
            gh("workflow", "run", workflow, "--ref", "master")
            print(f"   ▶️  {workflow} disparado ({now_local:%H:%M} hora de México)")
        except Exception as e:
            detail = getattr(e, "stderr", "") or str(e)
            print(f"::error::No se pudo disparar {workflow}: {str(detail)[:300]}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
