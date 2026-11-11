# 17 · Lecciones: código y base

Trampas del código que ya rompieron algo: pasos que salen OK sin hacer nada, avisos repetidos, transacciones, pandas y los NULL, el runner de GitHub, los secretos vacíos y el handicap asiático. Léelo antes de escribir código que toque la base o el pipeline.

<!-- detalle movido de OPERACION.md -->

## Gotchas que ya rompieron producción

**Un paso "OK" no prueba que hizo algo.** El paso del xG real imprimió ✅ OK desde el
17 hasta el 25-sep-2026 sin cargar nunca un dato: `soccerdata` se instalaba sin sus
dependencias y el `import` fallaba, el código lo trataba como "no instalado — omitido"
y seguía. Hoy lee Understat directo (`/getLeagueData/<liga>/<temporada>`) y, si no llega
ninguna liga, lo avisa: es **error** (llega a Telegram) cuando al xG anterior le queda
una semana o menos, y el morning reintenta cada día. Ante un "OK" sospechoso, busca
en el log qué cargó.

**Una sola causa, un solo aviso, con la causa correcta (3-nov-2026).** Con el xG de
Understat vencido, `get_real_xg` devuelve None para todos y el modelo usa el xG estimado con
tiros. La cobertura de cada corrida (`output_checks.feature_coverage`) habría dicho en cada
morning y evening "xG real de Understat en 0 de N partidos de las 5 grandes — nombres de
team_xg_real o la carga semanal", que manda a buscar donde no está el problema. Ahora el
pipeline guarda `xg_real_expired` (`soccerdata_feed.is_expired`, más de 21 días o sin carga)
y la cobertura no juzga el xG real vencido. El vencimiento lo siguen avisando
`report_stale` en el morning y `FEED_MAX_AGE_DAYS["team_xg_real"]` en el chequeo de salidas.

**`bets_history` está congelada desde el modo recolección (25-sep-2026).** Lo que mide al
modelo debe leer `program_picks`, que se liquida cada noche. La calibración rodante del
weekly (`scripts/weekly_sanity_audit.py`) medía las últimas 100 de `bets_history`: del 5-oct
al 2-nov mandó el mismo 🚨 cada lunes con apuestas del 19 al 22-sep, y se habría quedado sin
opinión para siempre. Desde el 2-nov-2026 mide los picks del programa y dice qué esperaba el
mercado. Siguen sobre `bets_history` el backtest walk-forward, el CLV audit y los chequeos de
apuestas (dobles, rezago, integridad), que solo cuentan si se vuelve a apostar.
- Desde el 27-oct-2026 el ✅ del chequeo de salidas dice lo que revisó: cuántos picks vieron
  su alineación publicada y cuántos partidos jugados tienen su final de ESPN.

**Un partido reprogramado contaba dos o tres veces (27-oct-2026).** Cada tabla guarda por
(partido, fecha). Cuando el feed cambia el horario, la fecha vieja se quedaba con su pick, sus
candidatas sombra y su análisis contra Pinnacle. La liquidación busca el resultado a ±1 día y le
daba el del partido real.
- Casos: PSG vs Lyon pasó del 25 al 26-oct. América vs Pachuca tenía tres horarios (23:00,
  01:00 y 01:05); el feed llegó a traer dos en la misma foto y se jugó a la 01:05. Toulouse vs
  Troyes pasó del 24 al 25.
- v2.4.3 solo lo escondía de Anteriores.
- Ahora `src/models/rescheduled.void_rescheduled` corre antes de liquidar, en el evening y en el
  weekly:
  - La fecha que vale es la de ESPN (`live_scores`) si la tiene. Si no, la de la fila escrita más
    recientemente en cualquier tabla, y en empate la más tardía.
  - Las otras fechas del partido a 3 días o menos quedan así: picks, sombra y propuestas pasan a
    'stale'; `model_vs_sharp` recibe `skipped_at`.
- `bets_history` no se toca: dos apuestas al mismo partido a distinta hora son dos apuestas y el
  bankroll las contó así. Hay 13 pares de abril y mayo.
- `backfill_picks` ya no reconstruye picks de análisis con `skipped_at`.
- El chequeo de salidas (sección 10, `counted_twice`) da ERROR si un partido vuelve a contar en
  dos fechas.
- La línea del log dice "N nuevos" (los que tuvieron filas anuladas en esta corrida) y "M ya
  anulados antes". Un partido anulado se vuelve a ver durante 30 días, y antes del 28-oct se
  anunciaba cada noche como si fuera nuevo.

**Una cobertura se mide donde la feature puede existir (27-oct-2026).** El chequeo de lo que
entra al modelo dividía el xG entre todos los partidos evaluados. Una noche con 32 de 42
partidos de Brasil, MLS, Suecia y Liga MX (ligas sin tiros en `matches`) dio "xG en 10 de
42", con los 10 de Serie A cubiertos: falsa alarma. Ahora xG, córners y tiros se juzgan
sobre `with_shots` (ver "Lo que entra al modelo se vigila", más abajo).

---

**No edites archivos con `Get-Content`/`Set-Content` de Windows PowerShell 5.1.** Lee
UTF-8 sin BOM como ANSI y escribe con BOM: rompe todos los acentos del archivo.

**Runner fijo: `ubuntu-24.04`.** GitHub anunció que `ubuntu-latest` pasa a Ubuntu 26.04
desde el 19-oct-2026. Un cambio de imagen puede romper la instalación de dependencias o
el Python de **todas** las corridas a la vez sin tocar el código, así que los workflows
fijan `ubuntu-24.04` (25-sep-2026). Subirlo es una decisión: en una rama, con `tests` y
el smoke test en verde. `release.yml` compila en `windows-latest`: si falla, solo se ve
al publicar una versión del dashboard.

**Secrets vacíos en GH Actions.** `int(os.environ.get("X", "default"))` devuelve
`int("")` y lanza `ValueError`, matando el pipeline antes de escribir nada. Usa
`env_int` / `env_float` / `env_str` de `config/settings.py`. El último módulo que lo
violó fue `src/utils/anthropic_budget.py` y tiró todas las corridas del analista un día.

**`bet["edge"]` no es el edge.** Es `edge_ev = prob*odds − 1`. El real es
`bet["edge_market"] = prob − 1/odds`. Filtrar por `edge` deja pasar longshots y costó
una semana a −23 % de ROI. Filtra siempre por `edge_market`.

**Las claves de handicap asiático están parametrizadas.** El mercado de una bet es
`ah_home_-0.5`, pero `MIN_EDGE_BY_MARKET` y `calibration_factors.json` se indexan por
grupo: `ah_home_fav` / `ah_home_pk` / `ah_home_dog`. Usa `_ah_group()` de
`src/models/calibration_monitor.py`. Hay precedente de un `dict.get(mkt, default)`
silencioso cayendo al default en todas las bets AH.

**La línea embebida en la clave AH es SIEMPRE la del local**, también en el lado
visitante: `ah_away_-0.50` es el visitante *recibiendo* +0.5. `_ah_group()` la
invierte para el visitante (`ah_away_-0.50` → `ah_away_dog`); hasta el 22-sep-2026
no lo hacía y los grupos del visitante estaban cruzados. El bloqueo de "AH con el
local favorito" es `("ah_home_fav", "ah_away_dog")`: las dos patas de la misma línea.

**AH, DNB y 1X2 apuestan al mismo resultado.** `ah_home_-0.50` es literalmente
`home_win`. Comparten grupo de exclusión (`_exclusive_group()`): máximo una bet por
partido entre ellos.

**Nada que escriba un runner sobrevive a su runner.** Cada corrida de Actions arranca
limpia: un archivo que genera el weekly no existe para el morning. Todo estado que
otra corrida deba leer va a `model_state` con `src/utils/model_state.py`
(`save_state` / `load_state`). Hasta el 22-sep-2026 la calibración y el caché de CLV
de producción eran la copia commiteada del 7-may, y el CLV gate nunca aplicó.

---

**pandas convierte los NULL de columnas enteras en `NaN` (float).** Si ese valor vuelve
a la DB en un `UPDATE`/`INSERT`, Postgres lo trata como `double` y al guardarlo en una
columna `INTEGER` revienta con *integer out of range* — y la transacción entera se
revierte. Convierte a `None` antes de escribir (`_sql_value` en
`scripts/weekly_sanity_audit.py`). Así estuvo roto el auto-fusionador de partidos
duplicados al menos desde el 21-sep-2026: detectaba, fallaba y no fusionaba nada.

**Un NULL leído con pandas puede ser `NaN`, no `None`.** Si otra fila de la misma
columna tiene valor, la columna es `float64` y el NULL llega como `NaN`: `x is not None`
es `True` y toda comparación con `NaN` da `False`. Así `resolve_market` liquidaba como
**perdidas** (over y under a la vez) las apuestas de córners, tarjetas y tiros cuyo
partido aún no tenía esos datos, hasta el 22-sep-2026. Para "¿hay dato?" usa `_has()` de
`src/models/save_bets.py` (o `pd.isna`). `audit_stat_settlements()` (en el smoke test)
cuenta, en solo lectura, cuántas se liquidaron así. El 23-sep-2026 se corrigieron las 4 que
encontró. Eran tarjetas del 12-sep, verificadas contra el CSV de football-data (+0.57u).
Se usó `scripts/fix_stat_settlements.py`: en seco por defecto, con `--apply` escribe. Ajusta
el bankroll por la diferencia y deja nota en `bankroll_history`.

**Un `try/except` dentro de una transacción no aísla nada.** En Postgres el primer
`execute` fallido aborta la transacción: los siguientes fallan también y el COMMIT final
se convierte en ROLLBACK de todo el lote, sin excepción visible. Aislar una fila exige
un savepoint: `with conn.begin_nested():` alrededor del `execute`. Así estaban
`fetch_results`, el respaldo de fbdata, las cargas del weekly y la creación de índices.

**`conn.execute(text(...), lista_de_dicts)` NO agrupa.** Con SQL textual, psycopg2 hace
un viaje a Neon por fila (~80 ms desde Actions): el weekly tardaba 25 min en 18k filas
de ligas extra y 21 min en 15k eventos. Para cargas masivas usa
`src/utils/db_batch.insert_ignore_conflicts`: una sentencia multi-VALUES por lote,
cuenta exacta de filas nuevas (RETURNING) y aislamiento de filas malas.

**Las `unresolved` se liquidan en cuanto llegan sus datos.** `update_bet_results`
toma `pending` (kickoff hace >3 h) **y** `unresolved`, y las liquida en la misma
pasada (`plan_bet_settlements`). Hasta el 22-sep-2026 las `unresolved` se re-marcaban
`pending` y el timeout de 3 días las devolvía a `unresolved` en la misma corrida: nunca
se liquidaban y terminaban `stale`, fuera del bankroll.

**Los nombres de equipo pasan por `normalize_team()` antes de consultar y de guardar.**
`matches` y `upcoming_matches` guardan el nombre canónico en minúsculas (las selecciones
del cargador internacional conservan sus mayúsculas: "China PR"). Un desajuste deja bets
en `pending` para siempre aunque `fetch_results` haya corrido, o duplica el partido con
otro nombre. Todo `INSERT` en `matches` usa `normalize_team`.

**`fetch_results.py` no tiene umbral de créditos**, pero desde el 29-oct-2026 solo pide las
ligas a las que les falta un marcador (`leagues_needing_results`); ver "El presupuesto no
cubría lo que más gastaba".
