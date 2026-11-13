# 09 · Aprendizaje

## Resumen

El dueño quiere un sistema que "solo necesite recolectar apuestas para recalibrar todo". El programa junta
datos todo el tiempo y cada lunes (weekly) recalcula lo que aprende. Todo va a `model_state` (la base),
porque las corridas de GitHub no guardan nada.

**De dónde aprende:**

- **Apuestas sombra** (`shadow_bets`): lo que el modelo propondría en cada mercado, con la cuota del momento
  y la de cierre de Pinnacle. Miden si el mercado se mueve hacia el modelo (CLV) y si gana.
- **Picks guardados** (`program_picks`): el resultado real de cada pick y de cada 🎯.
- **Análisis** (`model_vs_sharp`): las probabilidades del modelo contra Pinnacle en cada partido.

**Qué aprende (pasos del weekly):**

| Paso | Módulo | Qué decide |
|---|---|---|
| Peso del modelo | `anchor_learner.py` | Cuánto creerle al modelo frente a Pinnacle (encogido a 0.35, entre 0 y 0.5, ±0.10 por semana). Hoy ~0.07 |
| Sesgo por lado | `side_bias.py` | La corrección que le falta al modelo por lado (local, empate, visitante), por región y en goles |
| Filtros aprendidos | `filter_learner.py` | Qué regiones pausan el 🎯 (América desde el 3-nov; se quita solo cuando deje de perder) |
| Supuestos del 🎯 | `favourite_watch.py` | No decide nada: mide con todos los muy favoritos analizados (con pick o sin él) si siguen ganando más de lo que dice Pinnacle y cada cuánto paga el pago anticipado. Avisa si el veredicto cambia |
| Evidencia de reglas | `rule_evidence.py` | Si las reglas a mano tienen evidencia contra resultados reales |
| Referencia Pinnacle | `sharp_reference.py` | Si hay ventaja real contra el cierre de Pinnacle |
| Horario de apuesta | `timing_learner.py` | Si conviene apostar antes o después |
| Reactivación por sombra | (orquestador) | Si un mercado apagado vuelve por evidencia de la sombra |
| Calibración y CLV gate | `calibration_monitor.py`, `scripts/clv_gate.py` | Bloquear mercados o ligas mal calibrados o sin CLV |

**Frenos estadísticos:** mínimo de datos, encogimiento, histéresis y una cohorte (solo desde que el modelo
quedó bien, `LEARNING_SINCE`). Lo que no tiene evidencia no cambia nada.

**Modo recolección (desde el 25-sep-26):** el programa no registra apuestas propias; junta picks y sombra.
`bets_history` quedó congelada.

<!-- detalle movido de OPERACION.md -->

## Aprendizaje y calibración

### Dónde vive lo aprendido

Tabla `model_state` (clave → JSON), historizada en `model_state_history`. La DB es la
fuente de verdad; los archivos de `config/` y `data/` son espejo local.

| Clave | Lo escribe (weekly) | Lo usa |
|---|---|---|
| `dc_params` | `dc_mle_fitter` | lambdas DC-MLE |
| `calibration_factors` | `calibration_monitor.compute_calibration` | mercados sin ancla (hoy: doble oportunidad) |
| `clv_gate_markets` / `clv_gate_leagues` | `clv_gate.run_clv_gate` | kill-switch por mercado / liga |
| `clv_cache` | `betting_engine.refresh_clv_cache` | fracción de Kelly por CLV |
| `anchor_weights` | `anchor_learner.run_anchor_learning` | **peso del modelo** frente al mercado |
| `side_bias` | `side_bias.run_side_bias` | **sesgo del modelo** (por lado, por región, en más de 2.5 y ambos anotan) que se descuenta antes del ancla |
| `shadow_reactivation` | `clv_gate.run_shadow_reactivation` | `away_win`, AH con local favorito y grupos `sinref:*` |
| `shade_scales` | `shade_learner.run_shade_learning` | **escala de los ajustes manuales** (FLB, tabla, empates) |
| `rule_evidence` | `rule_evidence.run_rule_evidence` | veredicto de cada regla contra resultados (reporte; los filtros lo usan abajo) |
| `filter_switches` | `filter_learner.run_filter_learning` | filtros de selección prendidos/apagados (entre semana, rango de cuota, ligas difíciles) y regiones con el 🎯 en pausa (`paused`) |
| `timing_windows` | `timing_learner.run_timing_learning` | horarios antes del kickoff sin dinero real |
| `edge_finder` | `edge_finder.run_edge_finder` | solo reporte: qué ideas le ganan al momio histórico en los tres periodos |
| `sharp_reference` | `sharp_reference.run_sharp_reference` | solo reporte: apuestas y modelo contra Pinnacle |

**Cierres válidos:** todo lo que aprende del CLV (gate, caché de Kelly, reactivación,
peso del modelo) usa solo cierres descargados cerca del kickoff ([16](16-lecciones-cuotas-y-creditos.md)). Los cierres
anteriores al 22-sep-2026 no tienen hora de descarga y no cuentan: los learners
arrancan con esa muestra en cero y se llenan con cada closing.

**Cohorte:** todo aprende solo de datos desde `LEARNING_SINCE` (`config/settings.py`,
default `2026-09-14`, inicio de la arquitectura anclada). Si la arquitectura vuelve a
cambiar, se mueve esa fecha (variable de GitHub, sin tocar código). Con cohorte nueva
los learners arrancan en neutro y se van llenando: es a propósito.

### Peso del modelo (lo que más importa)

Un mercado anclado vale `p = p_mercado + w·(p_modelo − p_mercado)`. `w` era 0.35 fijo;
ahora `anchor_learner` lo aprende por familia (1x2, totales, BTTS, AH/DNB, medio tiempo,
córners/tarjetas): regresa el movimiento del mercado hasta el cierre (Δ = 1/cierre −
1/apertura) sobre el desvío crudo del modelo en las candidatas shadow. La pendiente es la
fracción de la opinión del modelo que el mercado termina confirmando. Límites:
encogimiento bayesiano hacia 0.35, rango [0, 0.50], ±0.10 por semana, mínimo 100
candidatas con cierre por familia (si no, estimado agregado; si no, 0.35). Si los datos
dicen que el modelo no anticipa al mercado, `w` baja y el sistema **apuesta menos**:
es el comportamiento correcto, y el shadow sigue midiendo.

### Reactivación por shadow

`away_win` y los AH con el local favorito están bloqueados de forma fija. Vuelven solos
si sus candidatas shadow que se habrían apostado (desvío > 0, edge ≥ 5 pt) muestran
CLV ≥ 0 con n ≥ 30; se re-bloquean con CLV significativamente negativo. Con el mismo
criterio vuelven los mercados **sin referencia de Pinnacle** (grupos `sinref:<familia>`:
btts, córners/tarjetas, medio tiempo, goles sin línea...): cuentan sus candidatas que
no tenían `pin_prob`, solo desde el 25-sep-2026 03:00 UTC (`PIN_RECORDING_SINCE`:
antes el vacío significaba "no se guardaba", no "Pinnacle no cotiza").

### Reglas manuales contra resultados reales

El CLV juzga bien el peso del modelo, pero **no** los ajustes escritos a mano (sesgo
favorito-longshot, "la tabla miente", empates contextuales): esas reglas afirman que el
precio está sesgado incluso al cierre, así que solo el resultado real puede
confirmarlas. Por eso cada candidata shadow guarda la probabilidad anclada antes de los
ajustes (`p_pre_shade`) y cuánto la movieron (`shade_delta`, ya acotado por el tope D12
y **sin escalar**), y el evening la liquida (`result`, `profit` por unidad).

- **Escala de los ajustes** (`shade_learner`): la probabilidad final de un mercado
  anclado es `p = p_ancla + s·δ`. El weekly estima `s` por familia con la regresión sin
  intercepto `y − p_ancla = s·δ` (la escala que minimiza el Brier), con la misma política
  que el peso del modelo: prior `s = 1` (las reglas tal como se diseñaron, sd 0.5),
  rango [0, 1] (puede apagarlas, no amplificarlas), ±0.25 por semana, mínimo 300
  candidatas win/loss con δ ≠ 0 y 30 partidos por familia (si no, agregado; si no, 1).
  Un win/loss es mucho más ruidoso que el CLV: con δ típico de 2-5 pt hacen falta
  ~1,000 candidatas para que los datos pesen lo mismo que el prior. Se mueve despacio a
  propósito.
- **Evidencia por regla** (`rule_evidence`, solo reporte): modelo vs mercado (Brier),
  ajustes con vs sin, sesgo favorito-longshot del propio mercado por lado y banda de
  cuota, si el edge de las apostables es real (brecha prob. − acierto y ROI), y los
  filtros de selección (entre semana, sweet spots, ligas duras) comparando la brecha de
  su grupo penalizado contra el resto. IC 95% robusto por partido; sin veredicto con
  n < 100 o < 30 partidos.
- **Filtros que se apagan solos** (`filter_learner`, clave `filter_switches`, desde el
  19-oct-2026, a pedido del dueño). Los tres filtros afirman que su grupo es peor. Cada
  uno se **apaga** solo si los resultados lo contradicen con fuerza:
  - qué cuenta como evidencia fuerte: n ≥ 100 en ≥ 30 partidos en cada grupo, y el IC
    entero de la diferencia de brechas por debajo de 0 (su grupo acierta mejor que el
    resto);
  - cuándo vuelve: apagado, regresa cuando la media deja de ser negativa;
  - con pocos datos, o si el estado no se lee, quedan prendidos.

  Nunca agrega ni endurece un filtro. El filtro Pinnacle sigue igual. Al 12-oct ninguno
  tenía datos suficientes (ligas difíciles 57-63, entre semana 31-36 apostables).
  Telegram solo cuando uno cambia.
- **Filtro por región del 🎯** (`filter_learner.REGION_FILTERS`, hoy solo `america`; 2-nov-2026,
  el dueño: "haz el filtro aprendido por región para América"). Los 🎯 de América (MLS, Liga MX,
  Brasil, Argentina, Libertadores; `side_bias.region_of`) iban 25 de 59 contra 31.1 que
  esperaba el mercado (−10.3 pt, −14.15u); las 5 grandes +15.1 pt y el resto de Europa +1.9.
  Perdían con cualquier ventaja (3-4 pt, 5-6, 6-8), así que pedir más ventaja no servía.
  - Se mide con los picks del programa, no con la sombra: brecha = acierto − probabilidad del
    mercado de los picks que pasan la regla (los 🎯 y los que el filtro pausó, que se liquidan
    igual), de los últimos 90 días; diferencia = la del resto − la de la región.
  - Nace de los datos, al revés que los tres de arriba: apagado, se **prende** solo si la región
    pierde contra el resto con el IC 95% entero > 0 (50+ picks en 15+ partidos por grupo);
    prendido, se **apaga** en cuanto deja de perder en promedio; con menos datos se queda como
    estaba.
  - Prendido: el pick de la región se ve igual, nunca 🎯, con el motivo "Región América en
    pausa…" (`program_picks.decide`, solo en los picks en vivo de `snapshot_picks`).
  - Si el estado no se lee, ninguna región en pausa: el 🎯 sale con la regla de siempre.
  - Al prenderse (2-nov): América −10.3 pt contra el resto +8.6 pt, diferencia +18.9 ± 15.2.
    Coincide con los tiros que no tenía (ver "Tiros y córners de ESPN"); con ellos, el filtro
    dirá solo si América deja de perder.

Llega a Telegram en el weekly solo si algún veredicto ya tiene datos suficientes o si
la escala se movió. Push y medias (AH de cuarto, DNB con empate) cuentan en el ROI pero
no en Brier ni calibración. Las candidatas sin datos del partido a los 10 días quedan
`result = 'stale'`.

### Referencia Pinnacle (¿hay ventaja real?)

Desde el 24-sep-2026 el sistema se mide también contra **Pinnacle**, la casa de
referencia de los profesionales. Su precio sin margen es el mejor estimador público de
la probabilidad real, y su cierre es el patrón estándar para saber si alguien tiene
ventaja. Viene en la misma descarga (región `eu`), sin créditos extra. Desde el
25-sep-2026 además **decide qué lleva dinero real** (modo recolección, abajo) y corrige
el sesgo por lado del modelo; en las probabilidades del ancla y en los stakes no entra.

- **Precios** (`src/features/pinnacle.py`): cada fetch guarda en `upcoming_matches`
  las cuotas de Pinnacle de 1X2 y de su línea principal de goles (`pin_*`). No usa
  COALESCE: un fetch con hora escribe el precio tal cual (NULL si ya no cotiza), para que
  un precio viejo nunca quede con hora nueva. Se derivan exactamente del 1X2: empate
  anulado (P(local | no empate), la misma definición del modelo), hándicap ±0.5 y doble
  oportunidad. Más/menos 2.5: directo si Pinnacle cotiza 2.5 (solo ~14% de los partidos);
  si no, derivado de su línea principal (2.25, 2.75, 3…) despejando el λ de Poisson que
  hace justa su cuota, con devolución en líneas enteras y mitad y mitad en las de cuarto
  (`over25_from_line`). El resto de mercados queda sin referencia.
- **Captura**: cada candidata shadow guarda `pin_prob` al registrarse, y cada apuesta
  lo guarda en `decision_log.market_ctx`. El closing guarda `pin_close_prob` y
  `pin_close_at` en ambas tablas (`save_bets.closing_updates`), con **su propia**
  frescura: el cierre del mercado puede venir de otro fetch (btts, DNB de la API).
- **Reporte** (`src/models/sharp_reference.py`, weekly, clave `sharp_reference`):
  valor esperado por unidad contra el cierre sin margen de Pinnacle (apuestas y
  candidatas; no espera el resultado), cuánto se mueve Pinnacle hacia el modelo entre
  apertura y cierre, y modelo vs cierre de Pinnacle en Brier. Usa solo cierres válidos
  ([16](16-lecciones-cuotas-y-creditos.md)), IC 95% robusto por partido y nada con n < 100 o < 30 partidos. Llega a Telegram
  cada semana en cuanto hay cierres válidos. Las apuestas se miden a la mejor cuota
  europea; en PlayDoit suele ser menor.

### Modo recolección: el filtro Pinnacle (25-sep-2026)

La auditoría del 25-sep midió que 7 de 11 apuestas pendientes tenían valor **negativo**
contra Pinnacle aun a la mejor cuota europea, y el simulacro de revalidación de ese día
no dejó en pie ninguna de las 14 pendientes. El modelo todavía no demuestra ventaja
sobre Pinnacle, así que el dinero real solo va donde Pinnacle confirma el valor, y todo
lo demás se sigue midiendo en sombra:

- **Pipeline** (`_stage_select_bets`): una apuesta real exige
  `p_pinnacle − 1/cuota ≥ 2 pt` (`SHARP_MIN_EDGE` = `MIN_EDGE_TO_PLACE`). Sin precio de
  Pinnacle solo pasa si su grupo `sinref:<familia>` ya fue reactivado con evidencia
  (más arriba, reactivación). Las candidatas shadow no cambian: se registran igual, con su
  `pin_prob`. El resumen de la corrida imprime `🔒 Filtro Pinnacle: N sin valor · M sin
  referencia`.
- **Revalidación pre-kickoff** (`scripts/revalidate_pending_bets.py`, cada closing):
  la misma regla a la cuota fresca, con el precio fresco de Pinnacle (o, si la fila ya
  no lo trae, el guardado con la bet). Cancela con `cancelled_no_value_pinnacle` o
  `cancelled_no_reference`; Telegram dice "canceladas: sin valor real frente a
  Pinnacle (no las apuestes)" y la confirmación pre-kickoff ya no sale.
- **Días sin picks son lo esperado.** Los mensajes lo dicen: "Sin apuestas con valor
  real frente a Pinnacle. No apuestes nada: el sistema sigue recolectando datos en
  sombra." Si el modelo demuestra ventaja (referencia Pinnacle con veredicto, peso del
  modelo al alza), el filtro se puede relajar; es una decisión del dueño.

### Horario de apuesta (`src/models/timing_learner.py`, 19-oct-2026)

El filtro Pinnacle compara con el precio de Pinnacle **de ese momento**, pero Pinnacle se
sigue moviendo hasta el kickoff (13-19 oct: 2.4 pt de media con más de 48 h, 1.2 pt en
las últimas 6 h). El weekly mide, por horario (menos de 12 h, 12-24, 24-48, más de 48 h
antes del kickoff), cuánto valían al cierre de Pinnacle las cuotas que pasaban el
filtro, y bloquea para dinero real el horario que pierde.

- **Datos:** `odds_history` en 1X2 y más/menos 2.5, desde el 13-oct-2026, cuando se le
  sumaron Pinnacle y la mejor cuota. El cierre es la última foto de los 150 min previos
  al kickoff; sin ella el partido no cuenta.
- **Señal:** una por partido, mercado y horario: la primera foto del horario que pasaba
  el filtro. Su valor es `p_pinnacle_cierre × cuota − 1`.
- **Política:** la de siempre (n ≥ 100 en 30 partidos, IC 95% por partido). Se bloquea
  si el IC entero es negativo; vuelve cuando la media deja de ser negativa. Con pocos
  datos no se bloquea nada.
- **Pipeline:** la apuesta que ya pasó el filtro Pinnacle en un horario bloqueado queda
  como propuesta con el motivo "a esta hora la ventaja no aguanta al cierre". Se vuelve
  a evaluar en la corrida siguiente (morning 12:00 UTC, evening 03:00 UTC). Rige en
  todos los mercados con precio de Pinnacle; los demás no pasan por aquí.
- **Ritmo:** la primera semana hubo 19 señales en 13 partidos (11 con más de 48 h). Con
  ese ritmo, el horario de más de 48 h tiene veredicto en ~2 meses; los demás tardan
  más. Contar cada foto como señal daba cientos por semana, pero repetía la misma
  cuota.
- **Telegram:** si un horario se bloquea o vuelve, o si alguno ya tiene veredicto.

---

### Sesgo por lado (`src/models/side_bias.py`)

Medido el 25-sep: el modelo daba en promedio +5.1 pt al local, −2.2 al empate y −3.3 al
visitante frente a Pinnacle. Pinnacle no tiene sesgo por lado, así que es un error
sistemático del modelo. Cada corrida guarda, en **cada** partido con precio de Pinnacle
(no solo en las candidatas, que sesgarían la muestra), el 1X2 **crudo** del modelo y el
de Pinnacle (tabla `model_vs_sharp`, una fila por partido). El weekly estima el sesgo
medio por lado en los últimos 21 días (`WINDOW_DAYS`), lo encoge hacia 0
(`n/(n+60)`), limita el cambio a ±3 pt por semana y el total a ±8 pt, y con < 30
partidos conserva el anterior (arranca en 0). El pipeline lo descuenta del 1X2 del
modelo y de lo que se deriva de él (doble oportunidad, empate anulado, hándicap) antes
del ancla; el `decision_log.model.side_bias` guarda el cambio aplicado. Telegram solo
cuando la corrección se mueve.

- **El pick y el dashboard usan el 1X2 corregido** (`program_picks.model_1x2`, 22-oct-2026).
  Antes usaban el crudo de `model_vs_sharp`, que se guarda así para aprender el sesgo.
- **Recalculado a mano el 22-oct** con el modelo corregido (Elo arreglado): 141 partidos con
  Pinnacle, `learn()` normal. Medido local +1.6 / empate −0.8 / visita −0.8 pts → corrección
  +1.1 / −0.5 / −0.6. La de antes, +3.0 / −1.8 / −1.2, venía del modelo con el error y
  sobrecorregía; respaldo en `model_state` `side_bias_backup_1019`.
**Por región y en goles (23-oct-2026).** Con el modelo del 22-oct (142 partidos) el sesgo no
era parejo y una sola corrección global (+1.1 al local) no servía:

| | local vs Pinnacle | más de 2.5 vs promedio de las casas | ambos anotan vs mejor precio |
|---|---|---|---|
| 5 grandes | +3.9 ± 1.1 | −0.1 ± 1.1 | −1.8 ± 1.1 |
| Resto de Europa | +1.3 ± 0.9 | −0.4 ± 0.9 | −1.9 ± 1.1 |
| América | −0.6 ± 1.2 | −3.8 ± 1.3 | −4.2 ± 1.3 |
| Todo | +1.5 ± 0.6 | −1.3 ± 0.6 | −2.7 ± 0.7 |

Con el modelo bajo en goles, los picks se iban al "menos" y al "no anotan ambos" sin razón.
- **Regiones** (`region_of`): 5 grandes (`BIG5`, la misma lista del pipeline), América (MLS,
  México, Brasil, Argentina, Conmebol, Concacaf), Asia (Japón, Corea, China, AFC) y Europa
  (el resto, con las copas de la UEFA).
- **Cómo aprende una región:** se separa de lo global en (su media − la global)·n/(n + 60)
  (`REGION_SHRINK_N`); con menos de 15 partidos (`MIN_N_REGION`) usa lo global. Su paso
  semanal parte de su valor anterior o, la primera vez, del global que se le venía aplicando
  (±3 pt por semana, como siempre).
- **Goles:** más de 2.5 y ambos anotan se aprenden igual (encogido, ±3 pt por semana, tope
  ±6 pt `GOAL_MAX_ABS`), contra la **misma referencia de los picks**: promedio de las casas
  sin margen en goles (si no hay, el mejor precio) y el mejor precio sin margen en ambos
  anotan (Pinnacle no lo cotiza). Cada corrida la guarda en `model_vs_sharp.mkt_over25` y
  `mkt_btts` (`market_goal_refs`). Los partidos analizados desde el 22-oct la tomaron de su
  foto de momios con `scripts/backfill_goal_refs.py --apply` (una vez, solo llena vacíos).
- **Dónde se aplica:** el pipeline descuenta el sesgo de la región del partido
  (`bias_for` + `debias`): 1X2 y derivados como antes, y además más/menos de 2.5 y ambos
  anotan / no anotan ambos, antes del ancla. Las líneas 1.5 y 3.5 del pipeline siguen con el
  Poisson crudo; en los picks salen de la matriz calibrada al 2.5 ya corregido.
- **Todo lo que lee el modelo guardado usa el corregido** (`side_bias.program_probs`): el pick,
  el dashboard y el revisor de capturas de PlayDoit. El revisor usaba el crudo hasta el
  23-oct.
- **Primera corrida (23-oct):** desde la global +1.1 / −0.5 / −0.6 → 5 grandes +2.1 / −0.8 /
  −1.4, resto de Europa +1.0 / −0.6 / −0.4, América +0.4 / −0.3 / −0.2; más de 2.5 −1.0
  (América −2.1), ambos anotan −1.7 (América −2.3). Fuera de muestra (aprende con 82
  partidos, mide en otros 66): el local en las 5 grandes queda a +0.3 de Pinnacle (antes
  +1.3) y ambos anotan a −0.7 (antes −2.7); el resto, parejo.
- **Lo vigila la auditoría del sistema** (chequeo "Sesgo del modelo", [12](12-auditoria-y-chequeos.md)): lo que queda después
  de corregir, global y por región, y que el weekly lo siga aprendiendo.

### Los supuestos del 🎯, vigilados (`src/models/favourite_watch.py`, 11-nov-2026)

El 🎯 de v2.9.0 no se puede juzgar por ganados y perdidos: el 11-nov había 17 cobrados con esa regla
(el encabezado los mezcla con los ~195 de la regla anterior) y la ventaja es delgada. Lo que sí se
puede medir cada semana es si se siguen cumpliendo las dos cosas en que descansa.

- **Lo que dice el historial** (`historical_odds`, 10 ligas con pago anticipado, 2012-26, al precio
  promedio de las casas; medido el 11-nov con `audit/r2_fav60.py`):

  | Favorito según Pinnacle | Partidos | Gana de más | Deja antes del pago anticipado |
  |---|---|---|---|
  | 70 de cada 100 o más | 6,147 | +3.6 puntos | +1.3% ± 0.6 |
  | 65 a 70 | 2,921 | +1.3 | −1.6% ± 1.2 |
  | 60 a 65 | 3,472 | +1.2 | −1.7% ± 1.3 |
  | 55 a 60 | 4,352 | +0.9 | −1.8% ± 1.3 |

  La ventaja vive en el de 70 o más (en las tres épocas: +2.6, −0.1, +1.0). El de 65 a 70 queda en
  tablas con el pago anticipado (~+1.3 de cada 100). Bajar el corte a 60 no agrega nada; subirlo a
  70 es decisión del dueño (pendiente).
- **Lo que mide el weekly:** todos los muy favoritos (65+) de `model_vs_sharp` de los últimos 120
  días, con pick o sin él, cobrados con la misma función que las apuestas. Por banda (65-70, 70+):
  ganados contra lo que esperaba Pinnacle y su z. Y el pago anticipado: de las apuestas al favorito
  con la ventaja de ESPN guardada (`live_scores`, desde v2.9.0), cuántas no ganaron pero iban 2
  arriba.
- **Veredicto** (ligas con pago anticipado, mínimo 100 partidos, z 1.96): a favor, sin evidencia,
  en contra, insuficiente. Telegram solo cuando cambia. No toca ningún pick.
- El chequeo diario avisa si deja de correr (`WEEKLY_KEYS`).
- **Dos señales en observación** (desde el 11-nov; se miden, no deciden nada y no cambian el veredicto):
  - **El precio.** Hoy el 🎯 no exige que el momio estimado de PlayDoit llegue al mínimo
    (`program_picks.value`). En el historial, el favorito de 65 a 70 cuyo precio promedio no llegaba
    al mínimo de hoy (probabilidad + 3.5 puntos) perdió 6.8 por cada 100 apostados (841 partidos;
    −10.2, −3.6 y −8.3 en las tres épocas) y con el pago anticipado sigue perdiendo ~4; el que sí
    llegaba dejó +0.5. En 70 a 80 no hay diferencia (+2.2 contra +1.7); en 80 o más, −2.2 contra
    +1.2 (no concluyente). El weekly lo mide con el momio del pick guardado de cada favorito de 65 a
    70 (`state["price"]`: partidos, ganados, lo que deja por cada 100 y en pesos apostando 50).
    Regla simulada en 14 años (`audit/m6_rule.py`): hoy (65+) deja +0.4 por cada 100 (+2.0 con pago
    anticipado), 632 🎯 al año; "70+ siempre y 65 a 70 solo si paga el mínimo" deja +1.1 (+2.6), 573
    al año, positiva en las tres épocas (+1.9, +0.3, +0.8); solo 70+, +1.3 (+2.5), 428 al año.
    **Ojo, con los precios de hoy quita más:** el promedio de las casas contra Pinnacle se ha ido
    cerrando para el favorito (65 a 70: −2.8% en 2012-18, −3.9% en 2018-23, −4.2% en 2023-26) y con
    los momios en vivo (`odds_history`, 12-oct a 11-nov-26, última foto antes del partido, 55
    favoritos de 65+ en ligas con pago anticipado; `audit/m9_live_price.py`) va en −5.0%. Al mínimo
    llegaban 74 de cada 100 favoritos de 65 a 70 en el historial reciente; en vivo, 6 de 14. Con
    precios de hoy la regla quitaría más de la mitad de los 🎯 de 65 a 70, no 3 de cada 10 (muestra
    chica: volver a medir con `state["price"]` cuando junte más).
    **Decidido el 12-nov-2026: el dueño eligió (1a)**, el 🎯 de 65 a 70 solo si el momio estimado llega
    al mínimo (v2.12.1, [08](08-picks-y-parlays.md)). Desde entonces el weekly lee el precio de la última
    foto de momios antes del partido (`odds_history`), no del pick guardado, para seguir cobrando también
    a los que se quedaron sin 🎯. Esos tienen su propio veredicto (`price_verdict`, 13-nov): con 100 o más,
    si dejan dinero más allá del azar el weekly avisa por Telegram que la regla conviene revisarla.
  - **La suerte de gol.** Por equipo, la media en sus últimos 10 partidos de liga (mínimo 8) de
    (diferencia de goles) − 0.31 × (diferencia de tiros a puerta); se compara favorito menos rival y
    más de +0.33 cuenta como "con suerte". En el historial, el favorito de 70+ sin suerte ganó 4.8
    más de cada 100 de lo que decía Pinnacle y dejó +2.9, +2.7 y +3.4 por cada 100 en las tres
    épocas; con suerte, 2.6 más y +2.2, −2.2 y −0.2. La diferencia (3.0 ± 1.5) todavía puede ser
    azar: prometedor, no probado. El weekly lo mide con `matches` (`state["luck"]`).
    Desde el 12-nov el buscador de ventajas la prueba además cada semana contra todo el historial
    ([07](07-modelo.md)): donde más se nota es en el visitante muy favorito (+11%, 303 partidos),
    pero esa idea salió de estos mismos datos y queda como prometedora. Por eso `state["luck"]`
    guarda al visitante aparte (`away_low`, `away_high`): son los partidos nuevos los que la
    confirman o la tiran. A ~22 partidos al año, tarda.

**Probado el 11-nov con 137 mil partidos y tres épocas** (2012-18, 2018-23, 2023-26;
`audit/m3_build.py`, `m4_test.py`, `m5_reg.py`, `m6_rule.py`), para no repetirlo:

| Idea | Resultado |
|---|---|
| Favorito local o visitante | Nada: +2.1 y +2.9 puntos, sin diferencia |
| Descanso de 4 días o menos (favorito o rival) | Nada |
| Primeras 4 jornadas | Nada |
| Rival o favorito recién ascendido | Nada |
| Partido de muchos goles según el mercado | Nada consistente entre épocas |
| Las demás casas contra Pinnacle, como información | Nada: lo que cambia es el precio, no quién gana |
| Titulares habituales fuera, contra el precio de cierre (ESPN 2019-26) | Nada: con 4+ fuera el favorito ganó +1.7 puntos (z 0.6) |
| Racha de los últimos 5 (puntos contra lo esperado) | Real pero chico: el mercado la exagera (z −4.1, igual en las tres épocas), ~0.7 puntos por desviación; no paga el margen y no se ve entre los favoritos de 65+ |
| La curva "Pinnacle → real" aprendida con 2012-23 | Le gana al precio de Pinnacle en 2023-26 (z +3.6; con suerte y racha, +4.4). Pero la ventaja es un escalón en 70, no una curva: 60-65 +1.5, 65-70 +1.3, 70-75 +4.0, 75-80 +3.2, 80+ +2.9 |
| Dar el 🎯 por "precio corregido" (curva × momio + pago anticipado ≥ 0, 1 o 2%) | **Descartado**: +1.9 y +3.7 en las dos primeras épocas, −0.7 ± 3.0 en 2023-26. Las bandas simples aguantaron |
| Pago anticipado por banda (ESPN 2019-26, fuera de América) | Rescata 1.8 de cada 100 en 65-70 y 0.9 en 70+: vale ~2.5% en 65-70 y ~1.2% en 70+ |

**Ya probado y descartado el 11-nov** (`audit/r1_rules.py`, 523 partidos sin 🎯 repetidos con lo que
había antes del inicio): cambiar el pick de los partidos sin 🎯 a "lo más probable" acierta más (59
contra 55 de cada 100) pero no deja más (−6.3% ± 3.5 contra −1.6% ± 4.1). El momio mínimo ya usa lo
más prudente entre el modelo y Pinnacle (`min_odds._floor_decimal`). No volver a proponerlo.

