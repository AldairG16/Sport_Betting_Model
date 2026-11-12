# 02 · Arquitectura

## Las piezas

```
                 cron-job.org (externo)                    GitHub (cron propio, respaldo)
        :00/:30 cierre · 6:00 mañana · 21:05 noche          :07/:37 cierre · watchdog 4 al día
                 · lunes 13:00 semanal (hora MX)
                              │  workflow_dispatch contra master
                              ▼
   ┌───────────────────────── GitHub Actions (runner ubuntu-24.04, se borra al terminar) ─────────────────┐
   │  closing.yml / morning.yml / evening.yml / weekly.yml ──► scripts/orchestrator.py --mode <modo>     │
   │  closing.yml también corre heartbeat_dispatch.py ──► late_results · resolve_pending · watchdog ·    │
   │                                                     playdoit_checker (keepalive)                    │
   │  pre_kickoff.yml (analista) · resolve_pending.yml (resolvedor) · release.yml · tests.yml            │
   └──────────────┬───────────────────────────────┬──────────────────────────────────┬──────────────────┘
                  │ lee                           │ lee y escribe                    │ llama
                  ▼                               ▼                                  ▼
   Fuentes de datos                     PostgreSQL en Neon                    Anthropic (Claude)
   The Odds API (cuotas, marcadores)    (todo el estado; plan gratis)         analista pre-kickoff,
   ESPN (marcadores, alineaciones,              │                             resolvedor, revisor
     tiros, córners, tarjetas)                   │                             de capturas PlayDoit
   football-data.co.uk (historia)                │
   Understat / FotMob (xG)          ┌────────────┼──────────────────────┐
   TheSportsDB (K-League)           ▼            ▼                      ▼
   Kalshi (2a referencia)       Telegram     Dashboard (.exe en la    Respaldo semanal
   ClubElo (fuerza de liga)     (bot del     PC del dueño, Flask      (lo hace el dashboard
   OpenWeatherMap (clima)       dueño y      en 127.0.0.1:5050)       en installer/dist/respaldos)
                                canal)
```

## Reglas de la arquitectura

1. **Todo el estado vive en la base.** Cada corrida de GitHub empieza en una máquina limpia y se borra al
   terminar: nada de lo que escribe en disco sobrevive. Lo aprendido va a `model_state`, lo que se mide va a
   sus tablas. Un archivo local solo es espejo o caché.
2. **Un solo orquestador.** `scripts/orchestrator.py` corre cada modo como una lista de pasos (`run_step`).
   Un paso que falla no detiene a los demás; al final la corrida sale en rojo si algún paso falló, y los
   errores tolerados se avisan por Telegram.
3. **Nadie depende del reloj de GitHub.** El cron de GitHub se atrasa horas; lo dispara cron-job.org.
   El cron propio del cierre (:07/:37) y del watchdog solo son respaldo.
4. **La base es lenta desde fuera** (Oregon, ~165 ms por viaje desde la PC del dueño): consultas pocas y
   grandes, nunca una por fila; sin `pool_pre_ping` en el pipeline.
5. **La base es gratis y tiene límite** (5 GB de transferencia y 100 horas al mes): cada lectura se mide y
   un freno baja el ritmo antes del límite ([06](06-base-de-datos.md)).
6. **El dueño no hace nada a mano.** Lo que necesite un dato del dueño se busca automático o no se hace.
7. **Cada clase de error que ya pasó es un chequeo** que corre solo ([12](12-auditoria-y-chequeos.md)).

## Cómo viaja un pick

```
update_upcoming_matches   →  upcoming_matches (cuotas de las casas europeas, Pinnacle aparte)
                              odds_history (una foto cuando cambian)
prediction_pipeline       →  model_vs_sharp (probabilidades del modelo y de Pinnacle por partido)
                              shadow_bets (candidatas sombra) · model_proposals (lo que propuso y por qué no)
program_picks.snapshot    →  program_picks (el pick y el 🎯 de cada partido, con su motivo)
program_parlays.snapshot  →  program_parlays (parlays del día y de cada horario)
espn_lineups / lineup_alerts → lineups / lineup_alerts (quién juega y quién falta)
final_alerts / today_board →  Telegram (pick final por partido, "Partidos de hoy")
fetch_results (+ fbdata, ESPN, TheSportsDB) → matches (marcador y estadísticas)
resolve_picks / resolve_parlays / shadow → result y profit de cada fila
weekly: side_bias, anchor_learner, filter_learner, … → model_state (lo aprendido)
```

## Carpetas

| Carpeta | Qué hay |
|---|---|
| `config/` | `settings.py` (ligas, umbrales, variables de entorno con `env_int`/`env_float`), `database.py` (el engine), JSON de calibración |
| `scripts/` | Los programas que se ejecutan: el orquestador, cargadores de datos, el analista, el resolvedor, el watchdog, la auditoría, Telegram, arreglos de una vez (`fix_*`) y diagnósticos (`diag_*`) |
| `src/models/` | El modelo, los picks, los parlays, el aprendizaje, la liquidación y los reportes |
| `src/features/` | Lo que se calcula de los datos: Elo, forma, xG, entre-ellos, ESPN, Kalshi, ClubElo, Pinnacle, alineaciones |
| `src/utils/` | Piezas comunes: nombres de equipos, fechas, momios americanos, estado aprendido, presupuestos (créditos, Anthropic, base), chequeos, medidor y respaldo |
| `src/pipeline/` | `prediction_pipeline.py` (el análisis completo de un día) y el de la MLB (apagado) |
| `dashboard/` | El servidor Flask, la página (`ui_es5.js`), el supervisor y el punto de entrada del `.exe` |
| `installer/` | La receta de PyInstaller; `dist/` (fuera de git) tiene el `.exe`, su `.env`, los respaldos de cada versión y los de la base |
| `tests/` | ~1,670 pruebas; `fake_db.py` (motor falso), `pipeline_harness.py` (el pipeline sin base ni APIs), `golden/` |
| `docs/` | Esta documentación; `historial/` con auditorías viejas |
| `archive/` | Módulos retirados, con su motivo (se archiva, nunca se borra) |
| `data/` | Cachés y archivos de apoyo; nada de lo que vive aquí es fuente de verdad |
| `.github/workflows/` | Las corridas ([04](04-corridas-y-horarios.md)) |

El detalle de cada módulo está en [03](03-componentes.md).

## Versiones

- `VERSION` es la versión del dashboard (hoy 2.12.1). Un tag `v*` hace que `release.yml` compile, pruebe y
  publique el `.exe`.
- El pipeline no tiene versión propia: lo que está en `master` es lo que corre en la siguiente corrida.
