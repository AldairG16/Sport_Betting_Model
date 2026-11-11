# 16 · Lecciones: cuotas y créditos

Errores que ya pasaron con The Odds API, Kalshi, los créditos, los cierres y las líneas de goles, con su fecha y su arreglo. Léelo antes de tocar la descarga de cuotas, el presupuesto de créditos o los cierres.

<!-- detalle movido de OPERACION.md -->

**Kalshi: reintento ante el límite de llamadas.** Desde GitHub, 5-9 ligas por corrida
fallaban con HTTPError, y las mismas respondían bien desde otra máquina. Ahora `_get`
espera 0.25 s entre llamadas y reintenta ante 429/5xx (1, 2 y 4 s).

**The Odds API: reintento ante 5xx y errores de red (24-oct-2026).** Un 502 pasajero dejó la
MLS sin recargar en un closing. `update_upcoming_matches.get_with_retry` reintenta 2 veces,
tras 3 y 10 s, en los momios por liga y por evento. Un 5xx no cobra créditos; un 4xx (clave,
plan, sin créditos) no se reintenta.

**Un cierre solo vale si se DESCARGÓ cerca del kickoff.** La cuota de una fila de
`upcoming_matches` puede tener horas: sin una recarga cerca del partido, el "cierre"
de una bet o candidata shadow era la misma cuota de apertura (CLV 0 exacto — se veía
en tarjetas, DNB, 1T y AH). Desde el 22-sep-2026 cada fila guarda
`odds_fetched_at` (fetch de la liga: h2h/totals/spreads) y `specialty_fetched_at`
(fetch por evento: btts, DNB, DC, 1T/2T, córners, tarjetas); cada cierre guarda
`closing_fetched_at`. El CLV gate, el caché de Kelly, la reactivación y el peso
del modelo usan **solo** cierres descargados entre 150 min antes y 2 min después
del kickoff (y después de la apertura): `src/utils/closing_quality.py`. El closing
recarga de forma dirigida las ligas con kickoff en 10-80 min que tengan bets o
shadow (`refresh_for_closing`), con topes por corrida: `CLOSING_MAX_LEAGUES_PER_RUN`
(6 × 3 créditos), `CLOSING_MAX_EVENTS_PER_RUN` (5 × ~7) y `CLOSING_MIN_CREDITS`
(600 desde el 29-oct; antes 1500). Los mercados por evento (btts, DNB, DC, 1T/2T, córners, tarjetas) se recargan
en **cada** corrida para los eventos con **bets** en ellos. Para los que solo tienen
candidatas **shadow** en esos mercados, desde el 24-sep-2026 hay **una** recarga por
evento: si su último enrichment ya cae en la ventana de cierre no paga otra vez. Tiene
tope propio (`CLOSING_MAX_SHADOW_EVENTS_PER_RUN`, 2 desde el 20-oct; antes 6) y solo corre
con más de `CLOSING_SHADOW_MIN_CREDITS` (1500 desde el 29-oct; antes 3000) créditos. Antes quedaban sin cierre válido: el
24-sep, ambos anotan y empate anulado de Seattle–Salt Lake tenían "cierre" de 12 h
antes, y así el aprendizaje de esas familias nunca recibía datos. El costo estimado eran
8-12 eventos por día de fin de semana; medido del 10 al 20 de octubre fueron 55-78 (× 7).

**Presupuesto diario de créditos (20-oct-2026).** Del 10 al 20 de octubre se gastaron ~925
créditos por día, el 90% en el closing: unas 90 recargas de liga (× 3) y 55-78 recargas por
evento solo para candidatas sombra (× 7) en un día de fin de semana. A ese ritmo el saldo
se acababa el 26-oct y los topes fijos (sin cierres bajo 1,500, sin cuotas bajo 500) dejaban
al sistema sin aprender la última semana del mes. Ahora el closing gasta como máximo
(saldo al empezar el día UTC − 600 de reserva) / días hasta el día 1
(`src/utils/credit_budget.py`; el estado del día va en `model_state.odds_credit_day`). El
gasto del día cuenta todo lo que bajó el saldo (morning, evening y closing); lo que no se
gasta queda para los días siguientes. Orden: primero las recargas de liga; los eventos
sombra, solo con lo que sobre. Los partidos con una **bet real** se recargan siempre,
aunque el presupuesto del día ya se haya usado (marca `bet` de `plan_closing_targets`).
Morning y evening no se frenan: sin sus cuotas no hay predicciones. Variables:
`ODDS_API_RESET_DAY` (1) y `ODDS_API_RESERVE` (600).

**El presupuesto no cubría lo que más gastaba (29-oct-2026).** El saldo cruzó el umbral de
parada (500) el 28-oct a las 14:30 UTC, 3.5 días antes de la renovación, con el presupuesto
puesto. Desde ahí no se descargaron cuotas, y cada corrida mandaba su aviso por Telegram
(cada 30 min).
- El presupuesto solo frenaba la recarga dirigida. La actualización normal del closing
  (`update_all`: ligas cuando vence su caché de 12 h y mercados por evento de las próximas
  48 h) no tenía tope. El 23 y 24-oct fue ~85% del gasto (~2,200 créditos, ~60% en mercados
  por evento), contra ~440 por día de presupuesto.
- Morning, evening y resultados (~200 por día) salían del mismo presupuesto, así que el
  último día el saldo terminaba ~200 por debajo de la reserva, bajo el umbral.
- `fetch_results` pedía las 19 ligas en cada corrida (2 créditos cada una con `daysFrom`,
  ~38 por corrida, ~114 al día) y contaba 1 por liga con resultados.

Ahora:
- El closing llama `update_all(budgeted=True)`: solo ligas y eventos que empiezan en las
  próximas `CLOSING_HORIZON_HOURS` (6), y con el presupuesto del día. Lo demás lo
  actualizan el morning y el evening, sin cambios.
- El presupuesto del closing es el del día menos `ODDS_API_MANDATORY_PER_DAY` (200).
- `fetch_results` pide solo las ligas con un partido analizado (o una apuesta pendiente) ya
  jugado y sin marcador en `matches` (`leagues_needing_results`). Si no puede leer la base,
  pide todas, como antes. Cuenta los créditos con el header `x-requests-last`.
- Los avisos de créditos bajos van una vez por día UTC (`credit_budget.alert_once`, estado
  en `model_state.odds_credit_alert`).
- El comentario de costo en `.github/workflows/late_results.yml` quedó viejo. No se editó
  porque un push a ese YAML dispara una corrida.

**Los cierres primero (29-oct-2026).** Del 24 al 28 de octubre casi no se guardaron cierres
válidos, y nada avisó: 15% de los partidos el 24, 19% el 25, 53% el 26, 17% el 27 y 0% el
28. Del 9 al 23 eran 98-100%. Sin cierres, el aprendizaje del peso del modelo no recibe
datos.
- El sábado 24 a las 13:30 UTC, antes de los partidos de la tarde, el closing ya decía
  "Recarga de cierre en pausa: presupuesto del día usado (803 de 364 créditos)". Los
  mercados por evento del morning, el evening y la actualización normal del closing se
  comían el día antes de los partidos.
- `CLOSING_MIN_CREDITS` (1500) apagaba los cierres con menos de 1,500 créditos: los últimos
  ~2 días de cada mes, aunque el presupuesto alcanzara (27 y 28 de octubre).
- Cada partido pagaba sus mercados por evento hasta 4 veces: caché de 12 h dentro de las
  48 h previas.
- Medido en un fin de semana completo (10-11 y 17-18 oct): los cierres por liga cuestan
  270-325 por día (2.0-2.2 recargas por horario de liga) y los mercados por evento de las
  candidatas sombra al cierre 460-550. El plan da ~645 por día.

Ahora (`credit_budget.capture_reserve` y `extras_budget`, `update_upcoming_matches._budgets`):
- La reserva de cierres es cada horario (liga, hora de inicio) cuya ventana de cierre cae
  antes de que acabe el día UTC, × 2 recargas × 3 créditos. Sale de la caché de cuotas.
- Los mercados por evento (en todos los modos), la actualización normal del closing y la
  sombra por evento solo gastan lo que queda libre: lo del closing menos esa reserva. Las
  ligas del morning y el evening no se frenan.
- El closing hace primero la recarga dirigida y después la actualización normal.
- `ENRICH_CACHE_TTL_HOURS` pasó de 12 a 48: un pago por partido antes del cierre. La cuota
  cerca del kickoff es la de la recarga de cierre (bets en cada corrida, sombra una vez), que
  no usa ese TTL. Lo vigente en la caché se vuelve a pegar gratis y no cuenta para los topes.
- `CLOSING_MIN_CREDITS` bajó a 600 (la reserva) y `CLOSING_SHADOW_MIN_CREDITS` a 1500.
- Consecuencia esperada: en fin de semana menos partidos tienen mercados por evento (ambos
  anotan, córners, tarjetas) y menos candidatas sombra de esos mercados tienen cierre. Los
  picks salen igual (nunca se ocultan); los que no tienen esos mercados no se comparan con
  ellos.
- El ✅ del "Chequeo de salidas" dice "cierres (N de M partidos)" de ayer (sección 12,
  `closing_coverage`). Con menos de 80% y 10+ partidos es ERROR. Si el aviso de "sin
  créditos" salió hoy, es solo WARNING: la causa ya se avisó.

**La renovación tarda unos minutos en llegar a la API (1-nov-2026).** El plan se renueva a
las 00:00 UTC del día 1, pero el closing de las 00:00 UTC del 1-nov todavía vio 349 créditos
a las 00:00:52. Abortó, como debía, y mandó el aviso de "sin créditos" por Telegram. A las
00:32 ya había 19,988.
- El aviso era falso: salió un minuto antes de que la renovación llegara.
- Su marca (`model_state.odds_credit_alert`, "stop" = hoy) dejaba a `output_checks` y al
  watchdog en "sin créditos" todo el día UTC, con 20,000 créditos. Cierres faltantes y
  cuotas viejas habrían salido como WARNING en vez de ERROR hasta el día siguiente.

Ahora:
- En la primera hora después de la renovación (`credit_budget.in_reset_grace`,
  `ODDS_API_RESET_GRACE_MIN` = 60) una corrida con saldo bajo aborta igual, pero sin aviso.
  Si a la hora sigue sin créditos, la siguiente corrida lo manda.
- Cada preflight con el saldo arriba del umbral borra la marca (`credit_budget.clear_stop`):
  "sin créditos" vale solo mientras de verdad no hay.

**Ligas fuera y líneas 1.5 / 3.5 de goles (30-oct-2026).** El dueño: "quiero tener ahorro
de créditos para poder valorar más varios mercados de picks".
- **MLS y Championship salieron de `SPORT_KEYS` (PR #97) y regresaron el mismo día.** El
  dueño: "hay que ver cómo funciona con las nuevas reglas y vemos cómo se va gastando los
  créditos para ver si aumentamos las descargas o nos alcanza justo". Primero se mide el
  gasto real de noviembre; con las 19 ligas, la simulación de abajo daba ~6,500 de sobra.
  Las protecciones de una liga quitada (sus marcadores y sus filas viejas) se quedan.
  Sus picks del 26-sep al 29-oct:
  - MLS: 92 picks, 39 ganados de 53 esperados (z −3.1) y −20.1u. Quedó debajo de lo
    esperado las 5 semanas.
  - Championship: 49 picks, 19 de 25.5 esperados y −6.9u. Ya estaba bloqueada para
    apuestas desde junio.
  - Brasil, Bélgica y Turquía, que el dueño también propuso, se quedaron: van parejo, tienen
    muy pocos datos y van ganando, respectivamente.
  - Las dos eran ~17% de los partidos.
- **Si se quita una liga, lo ya analizado se sigue liquidando.**
  - `fetch_results` también pide una liga fuera de `SPORT_KEYS` mientras le falte un
    marcador.
  - El pipeline ya no analiza sus filas viejas de `upcoming_matches`
    (`prediction_pipeline.only_fetched_leagues`). Esas filas duran 7 días después de su
    fecha.
- **El mercado por evento `alternate_totals` trae el más/menos 1.5 y 3.5 con precio real.**
  - Se guarda el mejor momio y el promedio de las casas que dan los dos lados:
    `over15_odds`, `consensus_under35_odds`, etc. (`GOAL_LINE_COLS`).
  - Antes esas líneas solo se estimaban del 2.5 (`estimated_lines`) y nunca eran pick.
    Ahora son candidatas (`program_picks.candidates`): mercado sin margen del promedio, y
    momio de PlayDoit = promedio × la razón de PlayDoit.
  - Si la línea principal de Pinnacle es 1.5 o 3.5, manda Pinnacle.
- **Costo.** El endpoint por evento cobra solo los mercados que devuelve (guía v4, "usage
  quota costs"). Un evento cuesta hasta 8, y `alternate_totals` no cuesta nada donde ninguna
  casa lo da.
- **Simulación** con el calendario de octubre y el presupuesto de 20,000:

  | | Antes | Ahora |
  |---|---|---|
  | Partidos con mercados por evento | 94% | 99% |
  | Mercados por evento con cierre | 43% | 56% |
  | Saldo de sobra al final del mes | ~6,500 | ~7,600 |

**Cuotas de cierre: mismo mercado, misma línea.** Hay UN mapeo mercado → cierre
(`_closing_odds_for` en `src/models/save_bets.py`) para apuestas y shadow. Si la línea
se movió (AH −0.5 → −0.75, córners 9.5 → 10.5) no hay cierre comparable y devuelve
`None`; nunca compares contra otra línea. El closing de producción tenía su propia
copia que buscaba `"btts_yes"` cuando la bet se llama `"btts"`: ninguna bet BTTS tuvo
CLV hasta el 22-sep-2026.

**`upcoming_matches` acumula duplicados stale.** Su clave de upsert incluye
`match_day`, derivado del kickoff, así que cuando la API corrige un horario se inserta
fila nueva en vez de actualizar. Todo consumidor debe leer con
`DISTINCT ON (home_team_norm, away_team_norm, sport_key) … ORDER BY updated_at DESC`.
- **Con un alias nuevo pasaba lo mismo (23-oct-2026).** La limpieza de duplicados reconoce
  el partido por los nombres normalizados. Cuando un alias cambiaba uno ("erzurum bb" →
  "erzurumspor"), la fila vieja quedaba con el nombre anterior y el pipeline analizaba el
  partido dos veces, hasta 7 días después.
- Ahora cada recarga corre `_renormalize_names` antes de `_cleanup_stale_duplicates`, y la
  auditoría del sistema lo vigila (chequeo "Nombres de los próximos partidos").
- Las tablas derivadas (`model_vs_sharp`, `program_picks`) pueden tener nombres de antes de un
  alias en partidos ya jugados. No importa: quien las lee normaliza (`match_key`, la
  liquidación).

**El cron de GH descarta `*/N` en los minutos punta** (`:00/:15/:30/:45`). Para algo
más frecuente que cada hora usa minutos off-peak — `pre_kickoff.yml` usa
`7,22,37,52 * * * *`. El síntoma es heartbeats con `bets_found > 0, bets_analyzed = 0`
durante horas.

---

**La hora de The Odds API puede venir tarde (1-nov-2026, arreglado en v2.8.0).** Panathinaikos–AEK
(sistema 19:00 UTC, real 17:30) y Vancouver–Seattle (sistema 21:30, real 20:30): el closing creyó
que faltaba y siguió rehaciendo el pick con momios EN JUEGO (Panathinaikos a 1.36 ganando, el
pick pasó a "más de 2.5" a 1.83). 2 de 307 partidos con hora de ESPN; se vio midiendo los
cambios con la alineación (el precio no cuadraba).
- Cada closing, "Marcadores del día (ESPN)" guarda también la hora de ESPN de los partidos que
  empiezan en las próximas 3 h (`live_scores.AHEAD_H`) en `espn_kickoffs` (kickoff = la del
  sistema, `espn_kickoff` = la de ESPN; mismo emparejamiento estricto, ±90 min). Si difieren más
  de 5 min sale un WARNING "⏰ … ESPN dice …" en el log.
- `snapshot_picks` no toca el pick de un partido que ESPN ya da por empezado (o a 5 min,
  `program_picks.started_early`), aunque la hora del sistema diga que falta.
- `snapshot_parlays`: un horario con un partido así queda fijo como estaba (no se borra el
  borrador ni se escribe otro en ese horario) y ese partido no entra a otro parlay.
- Sin `espn_kickoffs` (antes del primer closing de v2.8.0, o si no se pudo leer) manda la hora
  del sistema, como antes. Lo que ESPN no empareja (más de 90 min de diferencia o ligas fuera de
  ESPN) tampoco se corrige.
- El chequeo de salidas §14 avisa si vuelve a pasar.
