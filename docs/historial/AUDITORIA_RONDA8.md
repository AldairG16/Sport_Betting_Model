# Auditoría Ronda 8

Corte de datos: **domingo 20-sep-2026, 21:42 CST** (≈ lunes 03:42 UTC).
Commit de fixes: **9ca2542**. Suite: **245 passed**.

## 0. Regresiones: re-verificación de rondas 4-7 (R8)

245 tests en verde, incluidos los de r4 (D4-D13), r5 (N1/N2/N5), r6 (A1-A5)
y r7 (B1/B2) — con UNA actualización declarada: `test_shadow_capture_points_exist`
fijaba los 7 puntos de captura de r7 que C1 sustituye; reescrito como
`test_shadow_captures_before_value_filter` para la arquitectura nueva
(sweep antes de `find_value_bets`). Sin regresiones funcionales.

## 1. Tabla resumen

| ID | Posición de A | Mi veredicto | ¿Quién tenía razón? | Evidencia | Acción |
|----|---------------|--------------|---------------------|-----------|--------|
| C1 | El shadow no observa la región baja: `find_value_bets` filtra edge<0.02 antes de los puntos de captura | **Confirmado** — y su aritmética del slip verificada contra `calculate_edges` (edge contra implícita CON vig) | A | betting_engine.py, Q-U/Q-V | Sweep sobre clean_probabilities×odds con piso declarado (9ca2542) |
| C2 | El gate oscila: bloquear seca su propia muestra → n<30 → desbloquea | **Confirmado por lectura de código**: el gate recalculaba de cero cada semana y el propio docstring trataba la sequía como desbloqueo legítimo | A | `run_clv_gate` anterior | Histéresis: 2 ventanas consecutivas para bloquear, solo evidencia positiva para desbloquear (9ca2542) |
| C3 | La reactivación de ah_*_fav es circular: exige la evidencia que su instrumentación no produce | **Confirmado** — pero la solución no es "una línea": la instrumentación correcta es el sweep de C1, que precede al bloqueo por construcción | A en el diagnóstico; implementación distinta | test_sweep_precedes_ah_fav_block | Cubierto por C1 + Q-W documentado |

## 2. C1 — Rango observable del shadow

### 2.1 Q-U: volumen sin piso, medido con la corrida instrumentada

```
AFIRMACIÓN:  Mover la captura encima del filtro produce 1,200-1,800 filas
             por slate (estimación de A).
COMANDO:     corrida en seco instrumentada sobre el slate vigente (153
             partidos; save_bets interceptado, shadow persistido)
SALIDA:      40 candidatas barridas (~280/semana proyectado)
             bandas: 2-5:1 · 5-10:3 · 10-15:11 · 15-19:7 · 19-25:6 ·
                     25-30:8 · 30+:4
CONCLUSIÓN:  [MEDIDO] La conclusión de A (mover la captura) se aplica;
             su magnitud estaba inflada ~30-45×: ignoraba que el sweep
             hereda los filtros de sanidad (prob 0.03-0.90, favoritos
             extremos fuera) y exige precio de referencia y par de cuotas
             desmargnable. Con 40/slate, la captura completa es barata —
             no hace falta muestrear.
```

### 2.2 La opción elegida

**(ii) formalizado como (i) acotado**: barrido completo SIN el piso de edge
de `find_value_bets`, con piso PROPIO de desvío `SHADOW_MIN_DEV = 0.02`.
Razones: (1) con 280 filas/semana, muestrear (iii) complica sin necesidad;
(2) el piso de 2pt declara explícitamente que la banda [0-2) no se mide —
es ruido de cuota por debajo de la granularidad del devig; (3) el barrido
corre ANTES de `find_value_bets`, así que ve lo que ese filtro descarta.

Costo de implementación descubierto en la primera corrida (hallazgos §7):
los escalares numpy del barrido no son adaptados por psycopg2 y abortaban el
lote entero; `persist_shadow_bets` ahora convierte a nativos, sanea NaN/NaT
y usa savepoint por fila. Segunda corrida: **40/40 insertadas**.

### 2.3 Rango observable resultante por mercado (R11) + Q-V

| Mercado | rango observable del shadow | banda apostable | región ahora medible |
|---|---|---|---|
| todos | **desvío ∈ [2pt, ∞) con precio de referencia** | la de cada mercado (§3.1 r7) | [2, ~16-20pt) — la que C1 reclama |
| fuera por diseño | [0, 2pt) | — | ruido de devig |

Q-V (cobertura tras el cambio, un slate): la banda [10-15) pasa de 0 a 11
filas; [5-10) de 0 a 3; [2-5) de 0 a 1. **El disparador de la decisión B2
("CLV positivo en [5-15) con n≥50 → bajar el piso") vuelve a ser
evaluable**: a 280 filas/semana, la banda [5-15) alcanza n≥50 en ~3-4
semanas, dentro de la ventana que ya habíamos comunicado.

## 3. C2 — Histéresis del gate

### 3.1 ¿Oscila? Verificación

Confirmado por lectura del código anterior: `_write_blocked` recalculaba la
lista cada semana desde cero, y el docstring declaraba "se desbloquea solo
si el CLV trailing recupera **(o la muestra cae por rotación de la
ventana)**" — la segunda condición es la secuencia: bloqueo → 0 apuestas →
ventana vacía → n<30 → desbloqueo sin mejora de CLV. El ciclo de over25
(≈24 semanas, apostando el 27% del tiempo) que A calcula es consistente
con 4.7 bets/semana con closing.

### 3.2 blocked_since + evidencia positiva; dependencia de C1/C3

Implementado (`merge_gate_state`, función pura testeada):

- **Bloqueo**: exige **2 ventanas semanales consecutivas** con CLV
  significativamente negativo (IC95 unilateral <0, n≥30).
- **Desbloqueo**: solo con **evidencia positiva** — n≥30 y CLV medio ≥0.
  La ausencia de datos congela el bloqueo (streak congelada, mercado
  permanece), nunca lo revierte.
- **Estado persistido**: el JSON del gate ahora guarda `negative_streak` y
  `blocked_since` (fecha de bloqueo por mercado), además de la política.

**Dependencia declarada**: la evidencia positiva que necesita un mercado
bloqueado solo puede venir de apuestas nuevas (1x2, O/U — que el bloqueo
mismo seca) o del shadow (btts, dc, ah — que miden sin apostar). C2 depende
de C1: sin el sweep, un mercado bloqueado jamás acumularía la evidencia
para salir. Con el sweep, sí — y por eso el orden de esta ronda era C1
primero.

### 3.3 Decisión sobre multiplicidad

Elijo **"dos ventanas consecutivas"** (sobre Bonferroni y sobre aceptar el
1.2/año). Argumento: con las 3 ventanas independientes/año por mercado que
calcula A, exigir 2 consecutivas baja P(espurio) de ~14% a ~2% por
mercado-año (≈0.16 bloqueos espurios/año en los ~8 mercados testeables) a
costo de UNA semana de retraso en bloqueos genuinos — y con la histéresis,
un bloqueo espurio ya no se revierte solo, así que evitarlo en origen vale
más que barato. Bonferrono (z≈2.5) era la alternativa seria: la descarto
porque el sd del CLV por mercado es inestable con n~30 y el z alto ralentiza
también los bloqueos verdaderos.

## 4. C3 — Instrumentación de la reactivación AH (Q-W)

**Resuelta por construcción, no por aproximación.** El sweep de C1 corre
antes del filtro de candidatos, así que los favoritos AH bloqueados SÍ se
registran en shadow_bets con desvío y precio de referencia — su CLV de
reactivación es medible desde el primer slate (test
`test_sweep_precedes_ah_fav_block` fija el orden).

Q-W (la correlación que A pide para validar la aproximación con AH
adyacentes): **no computable en el histórico** — las filas de
bets_history guardan el mercado paramétrico (`ah_away_-0.2`) sin la etiqueta
fav/pk/dog, que solo el clasificador con odds vivas puede asignar. Lo
pooled: n=110, 51 con closing, CLV medio −0.16pp ≈ 0. Conclusión: la
aproximación era indefendible (no se puede ni estimar), y con el sweep ya
no hace falta — desde esta semana los favoritos AH acumulan su propia
medida.

## 5. Q-X — H2 en producción: ¿incidente o cerrado?

```
COMANDO:    SELECT 1 FROM model_state  (y mle_weight de bets de 72h)
SALIDA:     psycopg2.errors.UndefinedTable: relation "model_state" does
            not exist · mle_weight=0.0 en 77 bets, última 21-sep 03:08 UTC
CONCLUSIÓN: [MEDIDO] Ni incidente ni cerrado: el weekly AÚN NO HA CORRIDO.
            El corte de esta ronda es domingo 21:42 CST = lunes 03:42 UTC;
            el weekly dispara lunes 13:12 UTC — faltaban ~9.5 horas. El
            supuesto del prompt ("el lunes ya pasó") era falso en tiempo
            real. Verificación definida y pendiente: martes por la mañana
            (CST), decision_log del morning debe mostrar mle_weight > 0.0;
            si sigue 0.0 con model_state poblado, H2 se reabre como
            incidente. Ejecutada la comprobación, como exige la prohibición 14.
```

## 6. Derecho de réplica

### R-1
```
OBJETO:     "el orden de magnitud es 1,200-1,800 filas por slate
            (~10k/semana)" (Q-U, estimación que justificaba evaluar
            muestreo).
MI POSICIÓN: Medido: 40 filas por slate (280/semana) — inflada ~30-45×.
COMANDO:    corrida en seco instrumentada (sweep + spy sobre
            persist_shadow_bets), slate de 153 partidos
SALIDA:     40 candidatas; por banda: 2-5:1, 5-10:3, 10-15:11, 15-19:7,
            19-25:6, 25-30:8, 30+:4
POR QUÉ A SE EQUIVOCA: multiplicó partidos × mercados con cuota y olvidó
            tres filtros aguas arriba del sweep: sanidad de
            clean_probabilities (prob 0.03-0.90, favoritos extremos fuera),
            exigencia de par desmargnable (p_ref no nulo) y dev no nulo.
            La mayoría de partidos del slate no tiene AH+DNB+BTTS completos.
ALCANCE:    la conclusión de A (mover la captura) queda; las opciones (iii)
            muestreo y (ii) piso alto eran innecesarias — captura completa
            con piso declarado de 2pt.
```

### R-2
```
OBJETO:     "si el weekly del lunes falló, H2 es prioridad por encima de
            C1: son 457 líneas... que llevan ocho rondas sin llegar a
            producción".
MI POSICIÓN: El weekly no falló — no había llegado. Q-X ejecutado en el
            instante de corte: domingo 21:42 CST, faltaban ~9.5h para el
            cron del lunes 13:12 UTC.
COMANDO:    SELECT 1 FROM model_state + `date`
SALIDA:     UndefinedTable (esperable: la tabla la crea el fit del weekly)
            · fecha local del sistema: Sun Sep 20 21:42 CST 2026
POR QUÉ A SE EQUIVOCA: asumió el calendario sin consultarlo. La urgencia
            era ficticia; la verificación existe y quedó definida para el
            martes (mle_weight > 0 en decision_log o incidente).
ALCANCE:    ninguno sobre C1/C2/C3 — el orden de implementación que A
            propuso (C1→C3→C2) se respetó igualmente.
```

## 7. Hallazgos propios

1. **El batch-killer silencioso**: los escalares numpy (`np.float64`) no
   son adaptados por psycopg2; el primer INSERT fallaba, abortaba la
   transacción y las 39 filas restantes morían en cascada con
   `InternalError` genérico. Corregido (conversión a nativos + savepoint por
   fila + saneo NaN/NaT) — y la segunda corrida insertó 40/40.
2. **El estado del gate era stateless incluso para sí mismo**: el JSON se
   reescribía cada semana sin leerse — una pérdida del archivo habría
   desbloqueado todo. Ahora el payload lleva streaks y blocked_since.
3. **La etiqueta fav/pk/dog no existe en el histórico**: cualquier análisis
   retrospectivo por grupo AH (incluido el que A propuso en ronda 7 para
   justificar su piso) era irreplicable — la clasificación solo existía en
   memoria del pipeline con odds vivas. El shadow la habilita hacia adelante.
4. El print del sweep contaba partidos sobre un `df` reasignado por bloques
   intermedios ("4 partidos" siendo 153): corregido con contador propio.

## 8. Correcciones aplicadas

Commit: **9ca2542**. Suite: 245 passed.

| Arreglo | Archivos | Ocurrencias (R6) | Test | R8 | R9 | R10 | R11: rango observable y tasa de conclusión |
|---|---|---|---|---|---|---|---|
| C1 shadow sweep | prediction_pipeline.py (sweep, SHADOW_MIN_DEV), save_bets.py (persist endurecido) | 7 puntos r7 → 1 sweep | 3 tests (orden, piso, referencia) | actualizado test r7 (declarado) | Sí (piso 2pt derivado del ruido de devig) | cobertura: 40/slate medido | **[2pt, ∞) con p_ref; bandas [0-2) fuera; [5-15) con n≥50 en ~3-4 sem** |
| C2 histéresis | scripts/clv_gate.py (`merge_gate_state`, `significant_negative`, `positive_evidence`, estado persistido) | 1 flujo de decisión, reescrito | 4 tests | No | Sí (2 ventanas, evidencia positiva) | ciclos declarados por mercado (§3.1-3.2) | conclusión del gate: ~6 sem para over25, nunca sin shadow para btts/dc |
| C3 reactivación AH | (por construcción de C1) | 0 líneas nuevas — orden sweep<block | `test_sweep_precedes_ah_fav_block` | No | Sí (medición propia desde el primer slate) | — | favoritos AH observables desde desvío 2pt |
| Q-X | — (verificación, no arreglo) | — | — | — | — | — | H2: verificación martes; incidente solo si mle_weight=0 con model_state poblado |

## 9. Lo que sigo sin poder afirmar

- Que el gate con histéresis no acumule bloqueos falsos-positivos en
  rachas: dos ventanas correlacionadas (misma liga, misma semana rara) aún
  pueden pasar el filtro doble. Vigilancia: `blocked_since` ahora es
  auditable.
- El CLV de las bandas bajas: primera fila del sweep es de HOY. El corte
  con potencia ([5-15) con n≥50) llega en ~3-4 semanas al ritmo medido.
- El MLE en producción: verificación del martes (§5). Hasta entonces, ocho
  rondas sin MLE en producción siguen siendo ocho rondas.
- La estabilidad del sd del CLV por mercado con n~30 (afecta tanto al gate
  como al criterio de reactivación).

## 10. Veredicto y condición de salida de papel

**Papel**, sin cambios en las tres puertas (brecha anclada ≤3pt; CLV ≥0 con
n≥100 en un mercado principal; ROI con IC95 conteniendo 0) ni en el
horizonte de 4-6 meses al ritmo real de 13.1 apuestas/semana.

Lo que esta ronda añade al veredicto es la **agenda de revisión con
instrumentos que alcanzan su umbral**:

1. **~3-4 semanas**: primer `shadow_clv_bands` con n≥50 en [5-15) → decide
   si el piso baja (B2/(c)).
2. **Martes**: verificación H2 (mle_weight>0) → o se cierra H2 o se abre
   incidente.
3. **Continuo**: `blocked_since` audita que ningún bloqueo supere las ~24
   semanas sin evidencia de por qué sigue.
```
