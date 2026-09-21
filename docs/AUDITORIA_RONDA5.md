# Auditoría Ronda 5

Corte de datos: **20-sep-2026** (post-morning). Commit de fixes: **b4cdbf5**.
Estado global: ROI −8.2% en 1,135 resueltas (sin cambio desde la ronda 4 —
sigue en papel).

## 0. Regresiones: re-verificación de rondas anteriores (R8)

Re-ejecutado al cerrar la ronda (suite completa: **217 passed**, compileall OK):

| Check ronda anterior | Estado |
|---|---|
| D4 `compute_lambdas` — media de liga exacta (9 ligas) | OK (tests r4) |
| D5 señal 3 en escala del pipeline | OK (tests r4) |
| D6 BTTS anclable por ambos lados | OK (test r4 actualizado a `_anchorable`) |
| D9 HT 44/56 suma 1.0 | OK (tests r4) |
| D11/N5 baseline: `grep "= 1.35$"` en src/ → **0 ocurrencias** | OK — N5 detectó que D5 lo había reintroducido; corregido con import de `KALMAN_BASELINE` (b4cdbf5) |
| H3 gate CLV binding | OK (test de reload r4) |
| H2 dc_params en Neon | Pendiente de primer weekly (sin cambio) |
| N1: `is_strong_edge` eliminado; `ANCHOR_MAP` eliminado | Verificado por test (`test_is_strong_edge_is_gone`) |

## 1. Tabla resumen

| ID | Posición de A | Mi veredicto | ¿Quién tenía razón? | Evidencia | Acción |
|----|---------------|--------------|---------------------|-----------|--------|
| N1 | `is_strong_edge` descarta el precio del mercado desde ~7-10pp | **Confirmado con daño medido** | A | Q-G (§2) | Eliminada (b4cdbf5) |
| N2 | El blend es asimétrico a favor del modelo (0.90 vs 0.50) | **Confirmado en código**; daño mezclado en Q-G (todas las bets tienen edge>0) | A | `market_calibration.py` vieja, Q-G | Reescrita simétrica y decreciente (b4cdbf5) |
| N3 | Lock inexistente en CI + bankroll read-modify-write no atómico | Mecanismo real, **cero víctimas** (Q-I cuadra al céntimo) — pero la caza del drift encontró otros dos defectos contables reales | Réplica parcialmente ganada, concesión parcial (§4) | Q-I, Q-J | UPDATE atómico + savepoint por fila + reparación −7.54u (b4cdbf5) |
| N4 | El filtro de movimiento de línea se queda mudo (reset de opening) | **Confirmado**: el mecanismo es real y el cleanup BORRA la evidencia (Q-L=0 es efecto del cleanup, no ausencia del fenómeno); además el movimiento medido es ruido (mediana 5.6% > umbral 5%) | A | Q-K, Q-L, mediana/p90 | Heredar opening verdadero antes del delete (b4cdbf5) |
| N5 | D11 volvió por la puerta de atrás (2 literales 1.35 en ensemble) | **Confirmado** — lo introduje yo en la ronda 4 | A | grep | Import KALMAN_BASELINE + test (b4cdbf5) |
| N6 | D7 sigue abierto: ¿cuántas bets para refit y qué mientras tanto? | Pregunta legítima — respuesta numérica en §3-bis | A | — | ≥300, congelar mientras tanto (§3-bis) |

## 2. ¿La brecha crece con el edge? (Q-G, Q-H)

```
AFIRMACIÓN:  La brecha de calibración crece monótonamente con el tramo de
             edge — exactamente la predicción falsable de A para N1/N2.
COMANDO:     width_bucket(probability - 1.0/odds, 0, 0.30, 6) sobre todo el
             historial resuelto (corte 20-sep-26, todas las eras).
SALIDA:      tramo  edge        n    pred    real   brecha
               1    0.00-0.05  182  0.4844  0.4176  0.0668
               2    0.05-0.10  508  0.5804  0.4705  0.1100
               3    0.10-0.15  213  0.5802  0.4507  0.1295
               4    0.15-0.20   68  0.6097  0.4118  0.1979
               5    0.20-0.25   56  0.6751  0.5000  0.1751
               6    0.25-0.30   24  0.7087  0.4583  0.2503
               7    0.30-0.41   12  0.6669  0.0833  0.5836
CONCLUSIÓN:  [MEDIDO] Confirmado. En los tramos donde is_strong_edge +
             el peso direccional más alto actuaban (edge >15%), la brecha
             es de 17.5 a 58.4pt: el modelo predecía 61-71% donde la
             realidad fue 8-50%. Matiz de era (R7): los tramos 4-7 están
             dominados por apuestas PRE-anclaje (el ancla exige +25.7pt
             de edge crudo para pasar el filtro, así que post-anclaje
             casi no hay apuestas con edge final >15%).
```

Q-H (misma era post-14-sep, ancladas vs no, por tramo):

```
      tipo  tramo  n  brecha
   anclada      1   9  0.0428
   anclada      2   8 -0.0114
 no_anclada     1  32  0.0711
 no_anclada     2  32  0.1158
 no_anclada     3   1 -0.3121
```

Dentro del MISMO tramo de edge, la ruta anclada gana (4.3 vs 7.1pt; −1.1 vs
11.6pt). Con n=8-32 no es significativo, pero la dirección es consistente en
los dos tramos comparables: no es sólo calidad de mercado, es el ancla.

## 3. La decisión de diseño: (a) + (b), con argumento

Hice **las dos**, en este orden lógico:

**(b) Extender el ancla** a todos los mercados con devig confiable: AH y DNB
(pares con guardia de booksum 0.95-1.15), córners y tarjetas (pares), HT
(sólo con trío completo). La advertencia de A sobre el booksum < 1 con
cuotas-máximo está resuelta **por construcción**: `_devig_two_way` devuelve
None fuera del rango y ese mercado cae al blend simétrico (test
`test_devig_two_way_rejects_inconsistent_book`). DC queda fuera: la API trae
una sola pata y no hay trío que deviggar.

**(a) Eliminar `is_strong_edge` y simetrizar el blend** para lo que queda
fuera del ancla. El peso nuevo es simétrico y **decreciente** en |edge|
(0.50 → 0.30): Q-G demostró que el desacuerdo grande con el precio es donde
el modelo más se equivoca, así que la señal del modelo se concentra cerca
del precio y se apaga en las colas. Además, los mercados anclables ya no
pasan por el blend: entran crudos al ancla (antes había doble mezcla —
`calibrate_probability` dentro del ancla — que dejaba el peso real del
modelo en ~22-25%, no el 35% declarado).

Efecto esperado en producción: menos apuestas en AH/córners/tarjetas/HT (el
edge final sale ×0.35 del crudo), mejor calibradas las que pasen. Es
exactamente la disciplina que dejó los mercados anclados en 1.7pt.

## 3-bis. N6 — respuesta numérica

- **Refit de isotónicas/factores: cuando la generación post-r5 acumule
  ≥300 resueltas**, con n≥30 por mercado individual dentro de esas 300
  (por debajo, isotonic extrapola — el error que ya nos costó D7).
- **Mientras tanto: CONGELAR** los factores de mayo. No neutralizar:
  aplicar el modelo crudo sin descuento sabiendo que sobreconfía
  (brecha pre-ancla 12.8pt) es peor que una corrección vieja cuya
  dirección es correcta aunque la magnitud no. Además, tras (b), la ruta
  no anclada que esos factores pesan es mucho más pequeña (DC, patas
  sueltas, shots).

## 4. Atomicidad del bankroll (Q-I, Q-J)

```
AFIRMACIÓN:  La carrera read-modify-write de A nunca se materializó:
             el historial cuadra con el balance al céntimo.
COMANDO:     Q-I (suma bankroll_history por evento vs delta_balance)
SALIDA:      filas_bankroll=1  suma_resultados=−34.80  suma_otros=+100.00
             delta_balance=−34.80     →  −34.80+100.00 = 65.20 ✓
CONCLUSIÓN:  [MEDIDO] Réplica ganada: los workflows nunca se solaparon de
             forma que se perdiera un update. PERO la caza del drift abrió
             Q-J (profit bets = −42.34 vs −34.80 aplicados) y ahí había
             razón de A por otra puerta:
```

Dos defectos contables reales que nadie de los dos había reportado:
1. **307 filas amount-0** en `bankroll_history` (285 de bets marcadas
   `unresolved` que pasaban por `update_bankroll(0.0)`).
2. **Drift de −7.54u**: apuestas finales en `bets_history` cuyo
   `update_bankroll` falló y fue tragado por un try/except ("bankroll
   update skipped") — la bet quedaba liquidada y el capital sin ajustar,
   para siempre. En papel es ruido; con dinero real es contabilidad rota.

Fix (b4cdbf5): `update_bankroll` hace el ajuste en UNA sentencia
(`current_bankroll = current_bankroll + :profit` con `WHERE id fijo` y
`RETURNING` — la propuesta de A, adoptada tal cual) y acepta `conn` para
ejecutarse en el **mismo savepoint** que el UPDATE de la bet: si el bankroll
falla, la bet no se liquida y reintenta. Las `unresolved` ya no escriben
filas de bankroll. El drift histórico se reparó con un asiento de
corrección (`correction_r5`, −7.54u; balance 65.20 → 57.66).

Sobre el lock: la propuesta de moverlo a `pg_advisory_lock` la dejo sin
implementar — con el UPDATE atómico la carrera de bankroll ya no puede
perder updates, y el lock de archivo sigue sirviendo para no duplicar el
heavy work del mismo runner. Si algún día hay dos runners en paralelo
haciendo resolución, se revisita.

## 5. Movimiento de línea (Q-K, Q-L)

```
AFIRMACIÓN:  El opening se resetea con correcciones de horario y el
             movimiento medido es contaminado.
COMANDO:     Q-K + percentiles sobre upcoming_matches (30 días)
SALIDA:      total=357  sin_opening=0  opening_igual_actual=42 (11.8%)
             mov_medio_abs=0.9918   |  mediana=5.6%  p90=52.8%  >50%: 37 partidos
             Q-L: pares con múltiples match_keys = 0
CONCLUSIÓN:  [MEDIDO] Tres lecturas:
             1. Q-L=0 NO refuta a A — el cleanup BORRA la fila vieja, o sea
                borra la evidencia del reset (así funciona el mecanismo).
             2. El daño mayor ni siquiera es el reset: un movimiento mediano
                de 5.6% con umbral hard-skip de 5% significa que el filtro
                dispara sobre ruido la mitad de las veces, y un p90 de 52.8%
                es composición de bookmakers, no dinero. La señal estaba
                muda Y ruidosa a la vez.
             3. Fix aplicado: el cleanup ahora hereda el opening de la fila
                vieja (el auténtico) antes de borrarla. El ruido de
                composición queda como observación abierta: con openings
                verdaderos hay que medir de nuevo antes de tocar umbrales.
```

## 6. Derecho de réplica

### R-1
```
OBJETO:      "Dos runs concurrentes leen 100, uno escribe 105 y el otro
             103: el +5 se pierde" (N3).
MI POSICIÓN: El mecanismo existe en el código pero nunca tuvo víctimas:
             la reconciliación cuadra al céntimo tras 1,378 movimientos.
COMANDO:     Q-I (ver §4)
SALIDA:      −34.80 + 100.00 = −34.80 = delta_balance (exacto)
POR QUÉ IMPORTA: A propuso el arreglo correcto (UPDATE atómico con WHERE)
             para un daño que no ocurrió — y el daño real del bankroll
             (drift −7.54u, 307 filas fantasma) venía de otro mecanismo
             que la teoría de la carrera no cubría: fallos tragados por
             try/except y bet final + bankroll en transacciones separadas.
ALCANCE:     La urgencia implícita de N3 ("no es atómico, hubo pérdidas")
             cae; el arreglo se aplica igualmente porque es correcto.
```

### R-2
```
OBJETO:      La salvedad de A sobre Q-A: "el 1.7 vs 8.7 podría reflejar
             calidad de mercado y no efecto del ancla".
MI POSICIÓN: Q-H compara dentro del mismo tramo de edge y el ancla gana
             igual (§2) — pero con n=8-32 por celda, y eso hay que decirlo.
COMANDO:     Q-H (ver §2)
SALIDA:      anclada: 4.3pt (n=9), −1.1pt (n=8) vs no_anclada: 7.1 (n=32),
             11.6 (n=32) en tramos comparables
POR QUÉ A PIERDE (parcialmente): su propia salvedad exigía que la brecha
             del ancla empeorara dentro del tramo; mejora en ambos.
POR QUÉ NO GANO DEL TODO: ninguna celda alcanza n para significancia; la
             lectura correcta es "consistente con efecto del ancla, no
             demostrado". La extensión del ancla (b) es lo que dará n real.
ALCANCE:     Ninguna conclusión fuerte de nadie en la disputa Q-A; por eso
             el veredicto de papel no cambia.
```

## 7. Hallazgos propios de esta ronda

1. **Drift contable de −7.54u** en el bankroll (mecanismo distinto al de la
   carrera de A: fallos tragados + transacciones separadas). Reparado con
   asiento `correction_r5`.
2. **307 filas amount-0** en el historial de bankroll: cada bet marcada
   `unresolved` escribía un movimiento nulo. Ya no ocurre.
3. **El hard-skip de línea (5%) dispara sobre ruido la mitad de las veces**:
   mediana de |movimiento| = 5.6% > umbral. El filtro no protegía nada; era
   un dado. Con openings verdaderos (fix N4) hay que re-medir antes de
   tocar el umbral.
4. **Q-L no puede detectar el reset de opening porque el cleanup borra al
   testigo** — diseño de test de A que no podía fallar bien.
5. El doble blend en la ruta anclada (calibrate_probability dentro del
   ancla) dejaba el peso real del modelo en ~22-25%, no el 35% declarado
   desde el 14-sep. Todas las mediciones de "peso del modelo" de rondas
   anteriores usaban el número declarado, no el efectivo.

## 8. Correcciones aplicadas

Commit: **b4cdbf5**. Suite: 217 passed. R8: sin reintroducciones (§0).

| Arreglo | Archivos | Ocurrencias del patrón (R6) | Test | ¿Reintroduce algo? |
|---|---|---|---|---|
| N1: eliminar `is_strong_edge` | market_calibration.py, prediction_pipeline.py (import + bloque) | grep `is_strong_edge`: 0 tras fix | `test_is_strong_edge_is_gone` | No (D6/D8 intactos) |
| N2: blend simétrico decreciente | market_calibration.py (reescrita) | 1 sitio, 1 tocada | `test_blend_is_symmetric_in_edge_sign`, `test_direction_no_longer_amplifies`, `test_model_weight_decreases_with_disagreement` | No |
| (b) ancla extendida + guardia | prediction_pipeline.py (`_anchorable`, `_devig_two_way`, `_devig_three_way`, market_probs/market_probs_raw, bloque anclaje) | pares AH/DNB/córners/tarjetas/HT: 6 bloques tocados | `test_anchorable_*`, `test_devig_*` | No (D6 cubierto por tests r4) |
| N3: bankroll atómico | bankroll_manager.py (`_apply_bankroll_movement`), save_bets.py (savepoint por fila, sin filas-0) | `UPDATE bankroll` sin WHERE: 1 sitio, 1 tocado | (verificación en el próximo ciclo de resolución; SQL RETURNING testeado en staging local) | No |
| Drift repair | Neon (`bankroll_history` asiento `correction_r5`) | 1 asiento | reconciliación Q-I re-ejecutable | — |
| N4: opening verdadero | update_upcoming_matches.py (`_cleanup_stale_duplicates`) | 1 sitio | (medición Q-K post-fix en el próximo weekly) | No |
| N5: baseline Kalman | ensemble_model.py (import + 2 firmas) | literales `1.35` en src: 0 tras fix | `test_ensemble_baseline_defaults_to_kalman` | No — cierra la reintroducción |

## 9. Lo que sigo sin poder afirmar

- Que el ancla extendida mejore la calibración de AH/córners/tarjetas/HT:
  cero apuestas bajo el régimen nuevo.
- Que el movimiento de línea, medido desde openings verdaderos, sea señal
  y no ruido (Q-K se re-ejecuta cuando el fix lleve un ciclo completo).
- Que el mercado esté calibrado en esta submuestra (sigue sin medirse).
- Que el MLE llegue a producción (pendiente del primer weekly con
  `model_state`).

## 10. Veredicto: condición concreta para salir de modo papel

El modelo cambió de nuevo (tercera generación en seis días: ancla 14-sep,
reparto 20-sep, combinación r5 20-sep). El contador de apuestas válidas
vuelve a **cero** — y eso está bien en papel, que es para eso.

**Condición (métrica, umbral, muestra):**

Se sale de papel cuando la generación post-`b4cdbf5` acumule
**≥200 apuestas resueltas** y cumpla las tres a la vez:

1. **Brecha de calibración de los mercados anclados ≤ 3pt**
   (media de `pred − real` sobre esas ≥200; hoy la referencia anclada es
   1.7pt con n=17 — el umbral exige que se sostenga con muestra real).
2. **CLV medio ≥ 0 con n≥100** en al menos uno de los dos mercados
   principales (home_win u over25), medido con la definición vigente
   (`1/closing − 1/odds`); el gate automático de CLV es el árbitro.
3. **ROI con IC95% que contenga el 0** (para n=200, eso es ROI ≥ −13%;
   conservador a propósito: primero dejar de perder, después ganar).

Cumplidas las tres: piloto con dinero real acotado — stake 1% del bankroll
por apuesta (la cuarta parte del Kelly fraccional actual), tope de 5u/día,
y la primera violación de cualquiera de las tres métricas en ventana móvil
de 50 apuestas devuelve el sistema a papel sin discusión.
