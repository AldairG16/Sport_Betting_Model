# 06 · Base de datos

## Resumen

PostgreSQL en **Neon** (proyecto "Sports-Betting", AWS us-west-2), con todo el estado del programa: 213 MB y
~50 tablas en noviembre de 2026. Las tablas las crea el propio código (`CREATE TABLE IF NOT EXISTS`) la
primera vez que las usa.

- **Plan:** gratis hasta el 9-nov-26, cuando se bloqueó por pasarse de la transferencia (5.58 de 5 GB).
  Launch pagado del 11-nov al 1-dic para medir y corregir; desde el 1-dic, gratis otra vez, con la base fija en
  **0.25 CU** (Neon → Computes).
- **Límites del plan gratis:** 5 GB de transferencia (lo que se descarga) y 100 horas de cómputo al mes por
  proyecto, 1 GB de espacio. Al pasarse, Neon bloquea la base hasta el día 1.
- **Medición, presupuesto y freno:** cada proceso mide lo que lee (`db_meter`), el presupuesto pronostica el
  mes (`db_budget`) y frena antes del límite: modo ahorro desde el 75%, esencial desde el 90%.
- **Respaldo:** el dashboard copia la base a la PC del dueño cada semana (`db_backup`), y
  `scripts/restore_backup.py` la rehace.

## Las tablas

### Partidos y cuotas

| Tabla | Filas (nov-26) | Qué guarda |
|---|---|---|
| `matches` | 132 mil | Cada partido jugado con su marcador y estadísticas (córners, tarjetas, tiros, xG estimado, medio tiempo). Base del modelo. Vienen de football-data, ESPN, TheSportsDB y The Odds API |
| `upcoming_matches` | ~400 | Los próximos partidos con las cuotas de cada mercado: el promedio y la mejor de las casas, y Pinnacle aparte. Se reescribe en cada recarga; se guarda 7 días |
| `odds_history` | 91 mil | Una foto de las cuotas de un partido cada vez que cambian (para el movimiento de línea y "el precio de cuando se analizó") |
| `historical_odds` | 137 mil | Momios de cierre históricos de football-data (Pinnacle, promedio, mejor). Se vuelven a bajar; no van en el respaldo |
| `match_events` | 16 mil | Goles con minuto y autor (para goleadores y el pago anticipado) |
| `live_scores` | ~2 mil | El marcador de ESPN de los partidos de hoy (en juego, terminados y pospuestos) |
| `espn_kickoffs` | ~160 | La hora de inicio según ESPN (fija el pick al empezar) |
| `espn_matches`, `espn_teams` | ~1,500 / ~400 | Historia de copas, Europa y amistosos de ESPN |
| `lineups` | 5,400 | El once de cada equipo por partido (ESPN): titulares, goles, nombres |
| `kalshi_history` | 70 mil | Probabilidades de Kalshi (segunda referencia sin margen) |
| `club_elo` | ~600 | Fuerza de cada club según ClubElo |
| `team_xg_real`, `player_club_goals` | ~100 / ~1,000 | xG real por equipo y goles por jugador (Understat/FotMob) |

### Lo que el programa analiza y decide

| Tabla | Qué guarda |
|---|---|
| `model_vs_sharp` | El análisis de cada partido: probabilidades del modelo y de Pinnacle, goles esperados, y si ya no vale (`skipped_at`, `skip_reason`) |
| `program_picks` | El pick de cada partido (mercado, momio mínimo en PlayDoit, si es 🎯 y por qué no, y el aviso de que un grande de nombre viene mal), su resultado y ganancia; el pick antes de la alineación y los cambios |
| `program_parlays` | Los parlays del día y de cada horario, con sus piernas, bono y resultado |
| `shadow_bets` | Candidatas sombra: lo que el modelo propondría en cada mercado, con cuota, cierre y resultado. De aquí aprende |
| `model_proposals` | Lo que el modelo propuso y por qué no se apostó |
| `lineup_alerts` | Avisos de alineación (4+ titulares fuera) con el veredicto de Claude |
| `goalscorer_picks` | Picks de goleador (en papel) |
| `playdoit_prices` | Precios de PlayDoit leídos de capturas (el revisor de Telegram); hoy casi vacía |
| `pre_kickoff_analyses`, `analyst_heartbeat` | Lo que dijo el analista previo al partido y su latido |
| `weekly_narrative` | El texto del reporte semanal |

### Lo aprendido y la operación

| Tabla | Qué guarda |
|---|---|
| `model_state` / `model_state_history` | Todo lo aprendido (peso del modelo, sesgo por lado, filtros, referencia Pinnacle, presupuesto de créditos, canal de Telegram, plan de Neon…) por clave, y su historia |
| `pipeline_runs` | Cada corrida: modo, duración, pasos fallidos y lo que leyó de la base (`rows_read`, `cells_read`, `bytes_read`, `awake_s`). La usan el watchdog y el presupuesto |
| `anthropic_usage` | Gasto en Claude por llamada (presupuesto diario) |
| `bets_history` | Las apuestas registradas hasta el 25-sep-26. **Congelada** desde el modo recolección |
| `bankroll`, `bankroll_history` | El bankroll de la época de `bets_history`. Congelado |

### Sin uso o respaldos

- **Respaldos de arreglos** (`*_backup`, `backup_*`): la copia de lo que cambió un arreglo de una vez, por si
  hay que revertir. No se borran.
- **Heredadas**, que ningún código usa hoy: `bets`, `closing_odds` (vacía), `matches_history`, `team_stats`,
  `teams`, `team_mapping`, `team_name_mapping`, `team_aliases`. Se dejan (regla de archivar, no borrar).

## Reglas al usar la base

- Consultas pocas y grandes: la base está en Oregon y cada viaje cuesta ~165 ms desde la PC del dueño.
- Nada de leer tablas enteras en cada corrida: multiplica por 48 cierres al día y por las recargas del dashboard.
  El medidor y el chequeo 21 lo detectan.
- Ningún borrado sin respaldo antes (`*_backup` o el respaldo semanal).
- Un `try/except` dentro de una transacción no aísla nada; `conn.execute(text(...), lista)` no agrupa
  ([17](17-lecciones-codigo.md)).

<!-- detalle movido de OPERACION.md -->

**La base está en Oregon: cada consulta es un viaje de red (22-oct-2026).** Neon está en
`us-west-2`. Una consulta típica tarda 3.5 ms dentro del servidor y ~165 ms en total desde la
PC del dueño. Esa PC sale a internet por el exit node de Tailscale "GDL-VPN" (ping a 8.8.8.8:
97 ms), y una consulta de pandas son ~3 viajes (pedir la conexión, la consulta, el rollback).
Un ensayo del pipeline desde esa PC (miles de consultas) tarda ~1 h; en GitHub, ~7 min.
- `config/database.py` lleva `pool_recycle=240` (Neon corta conexiones inactivas) y
  keepalives de TCP, sin `pool_pre_ping`: agrega un viaje por consulta (+166 ms medidos).
- El 22-oct se agregó `pool_pre_ping` creyendo que los ensayos se colgaban en conexiones
  muertas. Eran lentos, no estaban colgados; se quitó el mismo día.

**La transferencia del plan de Neon (10-nov-2026, v2.11.2).** El 9-nov a las 13:55 UTC Neon bloqueó
la base ("exceeded the quota"). No fueron las horas de cómputo (37.8 de 100): fue la
**transferencia**, lo que el programa descarga de la base, 5.58 GB en 9 días contra 5 GB al mes del
plan gratis, con una base de 236 MB. La base vuelve al empezar el mes o al subir de plan.
- **Lo que se releía:**
  - Titulares habituales (`lineup_alerts.load_profiles`): bajaba el historial entero de cada equipo,
    con los nombres de toda la plantilla, tres veces por closing (picks, parlays y alertas) y en cada
    recarga de `/api/lineups`. Ahora `PROFILES_SQL` trae solo los últimos `HISTORY_MATCHES` (15) de
    cada equipo, que es lo que usa `team_profile`, y el dashboard guarda los perfiles 30 min
    (`_cached_profiles`).
  - Córners y tarjetas (`props_fair.load_history`): 900 días de todas las ligas en cada closing.
    Ahora el closing pide solo el año de las ligas de sus partidos y todos los partidos de sus
    equipos (`program_picks.props_scope`). El resultado es el mismo (`tests/test_db_transfer.py`).
  - Ya corregidos en v2.11.1: el dashboard recargaba cada 5 min aunque no se viera, y la fusión de
    repetidos leía todo el historial en cada corrida de resultados.
- **Medición** (`src/utils/db_meter.py`): cuenta las filas y celdas (filas × columnas) que devuelve
  cada consulta del engine del pipeline. Al final de cada corrida el log dice "🗄️ Lecturas de la
  base" con las 5 consultas que más leen, y `pipeline_runs` guarda `rows_read` y `cells_read`.
  No son bytes (psycopg2 no los da), pero ordenan bien qué pesa.
- **Chequeo de salidas 21** (`reads_jump`): si el closing de las últimas 24 h lee el doble o más que
  la mediana de los 7 días anteriores (y más de 50 mil celdas), avisa.
- **Antes de agregar una lectura**, multiplícala por 48 closings al día y por las recargas del
  dashboard. Por medir con el medidor: las fotos de momios de 72 h (`_SNAPS_SQL`) y el
  `SELECT * FROM model_vs_sharp` de Próximos.
- **Cierres de la sombra (11-nov-26, medido por db_meter):**
  - Cada closing releía todas las apuestas sombra de 10 días con un cierre "mejorable"
    (descargado antes del kickoff), y buscaba su partido una por una: 2,705 filas, 263
    consultas y 0.41 MB cada 30 min.
  - Un cierre solo mejora con cuotas de antes del kickoff (`should_update_closing`, más
    `CLOSING_MAX_LAG_MIN`), y esas ya no llegan horas después del partido.
  - Ahora (`save_bets.SHADOW_CLOSING_SQL`, `IMPROVE_HOURS` = 6) solo se mejoran los de las
    últimas 6 h: 154 filas. Lo que no tiene cierre se sigue recuperando los 10 días. Lo mismo
    en `bets_history`.

**El presupuesto del plan gratis (11-nov-2026, v2.12.0).** El dueño, después del bloqueo: "esto tiene
que funcionar 100% con planes gratis… no debemos sufrir estos ataques y pausar todo por eso". Neon se
pagó (Launch) del 11-nov al 1-dic para medir y corregir. Desde el 1-dic se vuelve al plan gratis, con
la base fija en 0.25 CU (Neon → Computes; Launch la había subido a 0.25↔8).
- **Medición** (`src/utils/db_meter.py`):
  - Bytes de cada lectura, en el protocolo de texto de Postgres. Con el cursor de psycopg2 del lado
    del cliente se leen las filas ya recibidas y se rebobina el cursor. Si no se puede rebobinar, la
    consulta falla (`RewindError`), para que nunca falten filas. Un resultado vacío no se rebobina:
    psycopg2 no lo deja, y el 11-nov eso tumbaba la consulta.
  - Base despierta: unión de [consulta, consulta + 5 min] del proceso.
  - Verificado contra la base real el 11-nov: 9 formas de consultar devuelven lo mismo con y sin el
    medidor. 132 mil partidos × 5 columnas = 7.9 MB, ~12 bytes por celda.
- **Quién lo guarda en `pipeline_runs`** (`bytes_read`, `awake_s`):
  - el orquestador y el resolvedor, en su fila;
  - los demás procesos de GitHub Actions, al salir, como `proc:<script>`;
  - el dashboard, cada 30 min, como `dashboard`;
  - el respaldo, como `respaldo`.
- **Presupuesto** (`src/utils/db_budget.py`):
  - Lo gastado en el mes de calendario (UTC, como cuenta Neon) más el ritmo de los últimos 3 días
    da el pronóstico del día último, en transferencia (5 GB) y en horas (100 CU-h a 0.25 CU).
  - `WIRE_FACTOR` 1.15 cubre TLS, TCP y lo que no se mide. Calíbralo contra la consola de Neon
    (Billing → "Network transfer").
  - Si la medición empezó a medio mes, los días de antes se estiman con el mismo ritmo.
  - Lo semanal (`WEEKLY_MODES`: weekly, respaldo, sanidad y CLV del weekly) se reparte en 7 días.
    Con 3 días de ritmo, recién medido, el weekly y la primera copia contaban como si pasaran a
    diario.
  - Sin 24 h de medición no opina.
  - Con `model_state` → `db_budget_plan` = `{"paid_until": ..., "since": ..., "cap_usd": 15}`, mide
    pero no frena. Así quedó del 11-nov al 1-dic-26 (Launch). El log dice en qué modo estaría con el
    gratis.
  - **La cuenta del plan de pago** (`paid_bill`, 11-nov-26). La alerta de $15 que el dueño puso en
    Neon (Billing → Spending notifications) es un correo de Neon: existe y está activa (revisada el
    11-nov: "$0.10 of $15"), pero no se puede probar. El programa lleva su propia cuenta:
    - Horas encendida desde `since` × 0.25 CU × $0.106, más el almacenamiento ($0.55 por GB al mes).
      Lo que corrió antes de que el medidor empezara se estima con el ritmo.
    - Avisa por Telegram una vez cuando, a ese ritmo, el mes llega al 80% del tope (`cap_usd`), y
      otra si lo pasa. El medidor no ve las consultas hechas a mano: por eso el margen.
    - El chequeo diario la muestra en lo revisado ("cuenta de Neon estimada $X de $15").
  - **La vuelta al gratis** (`downgrade_message`). El programa no puede saber si el dueño ya bajó el
    plan, y un mes olvidado se cobra entero. 12 h después de `paid_until` (con el mes de Neon ya
    cerrado en UTC) manda una vez "Hoy toca bajar Neon al plan gratis", con los pasos.
- **Avisos y freno:**
  - Por Telegram al pasar del 50% y del 75%, una vez cada uno por mes, y cuando cambia el modo
    (`model_state` → `db_budget`).
  - **Modo ahorro (≥75%):**
    - El closing corre solo en la primera media hora de cada hora, salvo que haya partidos en las
      próximas 2 h (`closing_skip_reason`). El cierre saltado igual se registra, para que el
      watchdog no lo cuente como parado.
    - Se saltan los pasos con `skip_at="ahorro"`: Kalshi, Backtest, Historial ESPN, Holdout,
      Momios históricos, Buscador de ventajas, Drift, Market regime y Walk-forward.
    - El dashboard se actualiza cada 15 min (`budget_level` en `/api/board/header`).
  - **Modo esencial (≥90%):**
    - El closing corre solo con partidos en las próximas 2 h.
    - Se saltan además los pasos con `skip_at="esencial"`: xG de respaldo, ClubElo, CLV update,
      datos internacionales, eventos y K-League.
  - Sin medición suficiente o sin base, el nivel es normal: el freno nunca apaga nada a ciegas.
- **Base caída** (`_db_preflight`):
  - Antes, cada corrida fallaba paso por paso, con errores a Telegram y correos de GitHub cada
    30 min.
  - Ahora la corrida termina al principio, sin error y con un `::warning::`.
  - El único aviso es el del watchdog (4 al día), en palabras: bloqueada por el plan, contraseña o
    sin conexión.
- **Respaldo semanal en la PC del dueño** (`src/utils/db_backup.py`, hilo del dashboard):
  - El hilo empieza 2 min después de abrir y revisa cada 6 h.
  - Cada 7 días copia todas las tablas menos `historical_odds` (se vuelve a bajar) y las
    `*_backup`. Cada 28 días incluye también `matches`, `match_events`, `matches_history` y
    `espn_matches`.
  - Formato: CSV comprimido por COPY, `schema.json` (tablas, restricciones, índices y
    secuencias) y `manifest.json` (filas).
  - Escribe en `<carpeta>.tmp` y renombra al terminar, así que una copia cortada nunca cuenta.
    No se borra ninguna.
  - Dónde: `installer/dist/respaldos/`. El log va en `respaldos/respaldo.log`.
  - El 11-nov la copia completa fue de 61 MB de transferencia en 49 s, 11.6 MB comprimida en disco.
    La semanal ronda los 41 MB. En total, ~4% del plan gratis al mes.
- **Restaurar:** `scripts/restore_backup.py <carpeta> [--schema x] [--apply]`. Sin `--apply` es un
  ensayo: crea todo en una transacción, compara filas y lo deshace. El 11-nov el ensayo en un esquema
  aparte rehízo las 37 tablas y 365,941 filas en 87 s.
- **Chequeos de salidas:**
  - 22: sin medición en 24 h, o modo esencial, es problema.
  - 23: la última copia tiene más de 10 días.
