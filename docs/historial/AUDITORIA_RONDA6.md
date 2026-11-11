# Auditoría Ronda 6

Corte de datos: **20-sep-2026** (noche, slate vigente: 153 partidos).
Commit de fixes: **a26cda3**. Suite: **226 passed**.

## 0. Regresiones: re-verificación de rondas 4 y 5 (R8)

| Check | Estado |
|---|---|
| D4 compute_lambdas (media de liga, 9 ligas) | OK (tests r4) |
| D5/D13-3 señal 3 en escala del pipeline | OK (tests r4) |
| D6/D9 HT 44/56, binarios anclables | OK (tests r4) |
| N1 `is_strong_edge` | grep: 0 definiciones (test r5 lo fija) |
| N5 literales baseline | grep `= 1.35`: 1 restante = `OVER15_ODDS` (cuota, otro dominio; documentado desde r4) |
| H3 gate CLV binding | OK (test de reload r4) |
| N3 bankroll atómico | intacto (b4cdbf5) |

## 1. Tabla resumen

| ID | Posición de A | Mi veredicto | ¿Quién tenía razón? | Evidencia | Acción |
|----|---------------|--------------|---------------------|-----------|--------|
| A1 | Umbrales desfasados del ancla: ah_fav imposible (64pt), dc como vía libre (17pt) | **Confirmado el desalineo (R9)**; la cura inicial que propuse (0.085) era incoherente y la corrida en seco lo demostró | A en el diagnóstico; la re-derivación final es distinta a la suya (§2) | Q-M, corridas en seco ×3 | Piso 0.05 + techo MAX_MODEL_DEVIATION 0.30 (a26cda3) |
| A2 | Córners/tarjetas apuestan contra cuota fabricada 1.80 | **Confirmado con víctimas**: 21 bets a precio inexistente (19 corners_over, 1 cards_under, 1 corners_under) | A | Q-N | Sin fallbacks + regla anclable-sin-ancla ⇒ no apuesta + 21 bets marcadas en decision_log (a26cda3) |
| A3 | `_devig_three_way` sin guardia | **Confirmado**: 25% de los tríos con booksum < 1.00 (mediana 0.99), mínimo 0.139 | A en el defecto; matiz en el rango (§4) | Q-O | Guardia [0.90, 1.15] en trío y en Shin 1x2 (a26cda3) |
| A4 | `blend_weight` es código muerto | **Confirmado** — llamada sin `edge`, peso plano 0.35 | A | `calibrate_probability(model_prob, market_probs_raw.get(market))` | Edge pasado; escalera ejercida + test que lo fija (a26cda3) |
| A5 | Cuartos AH destruidos por `:+.1f` (5 rondas abierto) | **Confirmado**: 41 bets de cuarto, 7 mal liquidadas (reconstruidas una a una), error neto +0.29u | A | Q-P + reconstrucción (§6) | `+.2f` en las 8 ocurrencias; el resolver ya era format-agnóstico — la "doble compatibilidad" que A pidió sobra (a26cda3) |

## 2. Re-derivación de MIN_EDGE_BY_MARKET (A1)

### 2.1 El número, derivado (y mi primer intento, fallado)

Mi primera re-derivación puso `MIN_EDGE = 0.085` (0.35 × 30pt − slip). La
corrida en seco la desmintió de inmediato: **0 apuestas**. El error de
concepto: bajo el ancla, `edge ≥ E` exige desviarse `(E + slip)/0.35`, así
que min_edge es un **PISO de desviación**, no un techo — 0.085 obligaba a
apostar solo con desvío ≥30pt, justo el borde de la región confiable. Un
techo de desvío necesita su propio filtro, y eso es lo que ahora existe:

- `MIN_EDGE = 0.05` (piso operativo): cubre el slip de vig medido
  (booksum de pares AH = 1.034 → slip ≈2pt) más ruido de cuota.
- `MAX_MODEL_DEVIATION = 0.30` (techo de calibración, filtro nuevo): se
  captura `|p_modelo − p_mercado_ref|` en el momento de combinar y se
  rechaza la apuesta si lo supera o si no hay precio de referencia.

**Derivación del 30pt — y por qué NO uso el 10pt de A.** A propone derivar
de Q-G un corte de ~10pt. Objeción (R7): los tramos de Q-G están dominados
por apuestas **pre-anclaje y pre-fix-λ** — describen una generación muerta;
el modelo anclado actual no tiene ninguna bet en esa muestra con desvío
conocido alto. La única medición directa de la generación vigente es
Q-A/Q-H (ronda 4): bets a desvíos de 18-36pt con brecha **1.7-4.3pt**. El
cap corta en 30pt, conservadoramente dentro de esa región medida. Si Q-G
fuera transferible, ni 10pt ni 30pt serían seguros: sería papel para
siempre; no lo es, y la corrida en seco así lo muestra.

> **[ERRATA, ronda 7]:** honestidad plena sobre el 30: es una **elección
> prudencial dentro de una región observada con n=8-32 por celda**, no una
> derivación. El propio §10 de este documento ya lo admitía ("el techo lo
> excluye por diseño, no porque lo haya medido"); esta sección daba a
> entender más de lo que hay (prohibición 11 de la ronda 7). Qué evidencia
> lo movería: CLV shadow positivo sostenido en la banda [25-30+] con
> n≥50 → revisar subida; CLV negativo significativo en [19-25) → bajar.
> Mecanismo de medición: `shadow_bets` + `shadow_clv_bands` (ronda 7).

### 2.2 Tabla nueva

> **[ERRATA, ronda 7 — B3]:** la frase siguiente era falsa tal como estaba
> escrita y contradecía al §10 de este mismo documento (R5). El gate de CLV
> con n≥100 fijo es INALCANZABLE para casi todos los mercados con las tasas
> reales (Q-Q ronda 7: máximo 94 bets/120d en el mercado más grande), y fue
> sustituido por un criterio estadístico (IC95 unilateral < 0, n≥30) en
> `scripts/clv_gate.py` — commit de la ronda 7. Además, los favoritos AH
> quedaron bloqueados explícitamente ese mismo commit. Ver
> docs/AUDITORIA_RONDA7.md §2.

Todos los mercados: `0.05` (piso único). ~~Justificación por mercado ya no
vive en esta tabla: la decide el gate dinámico de CLV (n≥100, CLV ≤−5% →
bloqueo automático, desbloqueo por recuperación).~~ *(Corregido: ver errata
anterior.)* Las entradas
`ah_*_fav: 0.20` morían por aritmética (64pt de desvío); ahora son
apostables dentro del techo y con el mismo piso que todos — si los
favoritos AH vuelven a comportarse mal, el gate de CLV los bloquea con
datos, no con un número congelado de mayo. *(Corregido en ronda 7: el gate
no alcanzaba para protegerlos — bloqueo explícito con ruta de reactivación
por CLV shadow.)*

### 2.3 Corrida en seco (slate del 20-sep, 153 partidos, bets capturadas sin escribir)

| Configuración | Bets | Mercados | Desvío implícito |
|---|---|---|---|
| HEAD (pre-r6) + umbrales viejos | 3 | under25 ×3 | 15.1-23.9pt |
| Código r6 + umbrales viejos | 3 | under25 ×3 | 15.1-23.9pt |
| Código r6 + piso 0.085 (primer intento) | **0** | — | — |
| Código r6 + piso 0.05 + techo 30pt (final) | **4** | btts_no ×2, under25 ×2 | 19.2-23.9pt |

La segunda fila aísla el efecto de A2/A3/A4: no matan volumen. Todo el
cambio de volumen viene de A1.

**La predicción de concentración en DC de A queda refutada hoy**: cero
apuestas DC en las cuatro corridas. La causa: DC va por el blend y encima
le caen GLOBAL_CALIBRATION (0.85) y el factor de mayo (0.78) — su edge
efectivo queda por debajo incluso del piso viejo de 0.06. Menos flujo DC,
no más.

### 2.4 Impacto en la condición de salida de papel

4 apuestas sobre un slate de 153 partidos (día de semana; el fin de semana
el slate más que duplica) ⇒ ritmo estimado **15-40 apuestas/semana** ⇒
200-300 apuestas acumuladas en **~2-3 meses**, no en semanas. La condición
de la ronda 5 se mantiene en sus tres puertas pero re-baseada en la
generación post-`a26cda3` con horizonte realista (§11). El sesgo de
composición que A advierte es real y se acepta explícitamente: hoy el
flujo es O/U + BTTS; la condición usa brecha anclada global, y si el
panel por mercado muestra que la muestra es demasiado estrecha, el
veredicto se pospone en vez de relajarse.

## 3. Rutas sin precio real (A2, Q-N)

```
AFIRMACIÓN:  Hubo apuestas registradas contra una cuota que nunca existió.
COMANDO:     Q-N: markets corners/cards/shots con ABS(odds − 1.80) < 1e-9
SALIDA:      corners_over_9.5: 19/63 a 1.80 (ROI −9.5%)
             cards_under_4.5: 1/21 a 1.80 · corners_under_9.5: 1/1 a 1.80
             → 21 bets a precio fabricado
CONCLUSIÓN:  [MEDIDO] Confirmado, con víctimas. Además el patrón era peor:
             shots_over/under_5.5 usaban cuota fabricada SIEMPRE (inerte:
             el mercado está disabled). Hallazgo adicional: el riesgo real
             no era el fallback del odds-dict sino la regla estructural —
             un mercado anclable cuyo devig falla caía al camino más
             permisivo (prob cruda 100% modelo). Ambos cerrados:
             sin pata API no hay entrada en odds, y anclable-sin-ancla
             ya no produce probabilidad (⇒ no hay candidata). Las 21 bets
             quedaron marcadas `cuota_fabricada: true` en decision_log
             para excluirse del refit de calibración (§3-bis ronda 5).
```

## 4. Guardia de booksum en tríos (A3, Q-O)

```
COMANDO:     Q-O sobre upcoming_matches (30 días, n=353)
SALIDA:      booksum_medio=0.9927  booksum_min=0.1391
             bajo_1=89 (25.2%)     bajo_098=29 (8.2%)
CONCLUSIÓN:  [MEDIDO] A tiene razón en el defecto y en que es material —
             con un matiz que cambia el rango correcto: con cuotas-máximo
             entre ~10 casas, un booksum de trío bajo 1.00 es NORMAL (25%
             de las filas; mediana 0.99). Es un consenso válido, no una
             fila rota. La patología es el extremo (0.139). Por eso la
             guardia es [0.90, 1.15]: bloquea lo incoherente, acepta el
             consenso. Aplicada en _devig_three_way (HT) y —nuevo— en el
             Shin 1x2 (market_probabilities), que antes normalizaba un
             booksum 0.139 "inventando" probabilidades. Rango distinto al
             [0.95, 1.15] de dos patas: medido, no copiado.
```

## 5. blend_weight: activar o eliminar (A4)

Activada. La escalera decreciente era el diseño documentado de la ronda 5
y la llamada sin `edge` un descuido mío, no una decisión. Ahora el bloque
de combinación pasa `model_prob − implied` y el test
`test_pipeline_passes_edge_to_calibration` fija la llamada con tres
argumentos — si el pipeline vuelve a llamarla sin edge, el test falla.

## 6. Cuartos de línea AH (A5, Q-P)

```
COMANDO:     reconstrucción bet a bet: línea real (−0.2→−0.25, −0.8→−0.75),
             resolución quarter correcta (dos medias líneas) vs profit
             registrado, con goles reales de cada partido
SALIDA:      41 bets de cuarto · 7 mal liquidadas · error neto +0.29u
             (el sistema se auto-adeudó 0.29u; impactos individuales
             entre −0.41u y +0.30u por bet, todos en empates o márgenes
             de 1 gol en líneas x.25/x.75)
CONCLUSIÓN:  [MEDIDO] Confirmado el defecto; pequeño el daño histórico.
             Relevancia futura: 18 partidos del slate vigente tienen línea
             cuarto. Fix: formato +.2f en las 8 ocurrencias. Nota de
             réplica menor: el resolver SIEMPRE parseó el sufijo con
             float() y es agnóstico al formato — la "doble compatibilidad"
             que A pidió implementar no era necesaria; los dos formatos
             conviven y el detector de cuartos (fórmula real, correcta
             desde la ronda 1) ahora sí recibe cuartos.
```

Test: `test_ah_line_format_preserves_quarters` (exactamente 8 ocurrencias
de `+.2f`, cero de `+.1f`) y `test_resolver_parses_two_decimal_lines`.

## 7. Derecho de réplica

### R-1
```
OBJETO:     "Q-G da la base empírica: por encima de ~15pt la brecha es
            20-58pt, o sea el modelo no tiene nada que aportar ahí"
            (uso de Q-G para derivar el corte de ~10pt).
MI POSICIÓN: Q-G no es transferible a la generación actual — viola R7 tal
            como A la definió.
COMANDO:    Q-B (ronda 4): SELECT ... CASE WHEN created_at < '2026-09-19' ...
SALIDA:     era pre-fix: n=113, λ_total=4.003, 80/113 al cap;
            las bets de Q-G tramo 4-7 son mayoritariamente de esa era.
POR QUÉ A SE EQUIVOCA: el modelo que generó los tramos catastróficos de
            Q-G tenía λ inflados +40% y sin ancla; su error a desvíos
            altos no predice el error del modelo con λ 1e-16 contra la
            media de liga y ancla 0.65. La única medición de la generación
            vigente (Q-A/Q-H) muestra brecha 1.7-4.3pt JUSTO en desvíos
            de 18-36pt — la región que el corte de 10pt prohibiría.
ALCANCE:    cae el "0.01-0.015" como umbral derivado; queda en pie el
            mecanismo R9 (re-derivar), que aplico con otro número.
```

### R-2
```
OBJETO:     "El flujo de apuestas debería concentrarse en dc_1x/dc_x2"
            (vía de menor resistencia, 17.1pt).
MI POSICIÓN: Refutado empíricamente hoy mismo, con la corrida en seco que
            A pidió.
COMANDO:    corrida en seco del pipeline sobre el slate vigente (4 configs)
SALIDA:     0 apuestas DC en todas; el flujo real es under25/btts_no.
POR QUÉ A SE EQUIVOCA: ignoró que DC no ancla pero TAMPOCO es modelo
            crudo: le caen el blend, GLOBAL_CALIBRATION 0.85 y el factor
            de mayo 0.78 — su edge efectivo colapsa por debajo del piso.
ALCANCE:    cae la predicción de sesgo hacia DC; el sesgo real (O/U+BTTS)
            se documenta en §2.4.
```

### R-3 (menor)
```
OBJETO:     "Arreglo: :+.2f en las ocho ocurrencias + doble formato en el
            resolver por retrocompatibilidad".
MI POSICIÓN: El resolver no necesita cambios: parsea el sufijo con float()
            y siempre fue agnóstico al formato (parts[2] → float).
COMANDO:    lectura de save_bets.py: `home_line = float(parts[2])`
SALIDA:     float("-0.25") y float("-0.2") parsean igual; el detector de
            cuartos recibe lo que el formato del pipeline le mande.
POR QUÉ IMPORTA: el fix se reduce al pipeline (8 strings), sin tocar el
            resolver — menos superficie, mismo efecto.
ALCANCE:    ninguno sobre la conclusión de A (el defecto y su fixes son
            reales); solo sobre el alcance del arreglo.
```

## 8. Hallazgos propios de esta ronda

1. **Mi primera re-derivación fue incoherente y la corrida en seco la
   cazó antes de llegar a producción** (0.085 ⇒ 0 apuestas). min_edge es
   un piso de desvío, no un techo; el techo necesitaba su propio filtro y
   no existía en ninguna ronda anterior.
2. El booksum de tríos bajo 1.00 es estadística normal del máximo entre
   casas (25% de las filas) — una guardia simétrica estricta habría
   bloqueado un cuarto de los anclajes 1x2 legítimos.
3. El Shin 1x2 normalizaba filas rotas (booksum 0.139) sin quejarse:
   inventaba probabilidades exactas a partir de precios imposibles.
4. `model_state` todavía no existe en Neon — el primer weekly post-H2 no
   ha corrido (hoy es domingo; corre el lunes). H2 sigue sin verificación
   en vivo.
5. Las cuotas de shots siguen siendo fabricadas (1.80) aunque el mercado
   está disabled — deuda de limpieza para cuando ese mercado se reactive.

## 9. Correcciones aplicadas

Commit: **a26cda3**. Suite: 226 passed. R8: §0 sin regresiones.

| Arreglo | Archivos | Ocurrencias (R6) | Test | ¿Reintroduce? (R8) | ¿Parámetros re-derivados? (R9) |
|---|---|---|---|---|---|
| A1 piso+techo | prediction_pipeline.py (MIN_EDGE 0.05, MAX_MODEL_DEVIATION 0.30, tabla uniforme, filtro nuevo, `_model_deviation`) | tabla: 16 entradas, 16 tocadas | `test_min_edge_is_operational_floor_not_deviation_cap`, `test_no_threshold_above_floor` | No | **Sí — es el objeto de la ronda** |
| A2 sin cuota fabricada | prediction_pipeline.py (odds córners/tarjetas, bloque combinación) | fallbacks: 2 → 0; regla estructural: 1 sitio | `test_no_fabricated_odds_in_source`, `test_anchorable_without_anchor_is_not_bettable` | No | Sí (umbral default 0.05→piso) |
| A3 guardia trío/Shin | prediction_pipeline.py (`_devig_three_way`), market_odds.py (`market_probabilities`) | 2 sitios | `test_devig_three_way_guard`, `test_shin_rejects_broken_books` | No | Sí (rango medido [0.90,1.15]) |
| A4 escalera ejercida | prediction_pipeline.py (llamada con edge) | 1 sitio | `test_pipeline_passes_edge_to_calibration` | No | No requerido |
| A5 cuartos | prediction_pipeline.py (8 formatos) | `+.1f`: 8 → 0; `+.2f`: 8 | `test_ah_line_format_preserves_quarters`, `test_resolver_parses_two_decimal_lines` | No | No requerido |
| Marcado 21 bets fabricadas | Neon (decision_log `cuota_fabricada`) | 21 filas | — | — | — |

## 10. Lo que sigo sin poder afirmar

- La calibración del modelo actual a desvíos >36pt (fuera de la región
  medida; el techo 0.30 lo excluye por diseño, no porque lo haya medido).
- Que el gate de CLV basta como regulador por mercado: necesita n≥100 por
  mercado, y los mercados nuevos (AH desbloqueado) empiezan de cero.
- Que el MLE llegue a producción (primer weekly post-H2: mañana lunes).
- El ROI por tramo de desvío de la generación actual: Q-M con era anclada
  tiene celdas de n=3-5; no se concluye nada de ahí.

## 11. Veredicto y condición de salida de papel, actualizada

Papel, sin cambios — y ahora con la arquitectura de decisión coherente con
el modelo que la produce, que era la condición implícita de R9.

**Condición actualizada (re-baseada en la generación post-`a26cda3`):**
≥200 apuestas resueltas de esta generación (horizonte realista: 2-3 meses
al ritmo medido de la corrida en seco), cumpliendo las tres puertas de la
ronda 5: (1) brecha anclada ≤3pt, (2) CLV ≥0 con n≥100 en un mercado
principal, (3) ROI con IC95 que contenga 0. Adición de esta ronda: si al
cierre de la muestra el panel por mercado muestra concentración >70% en un
solo grupo (hoy: O/U+BTTS), el veredicto se limita a ese grupo en vez de
habilitar dinero real para todo el sistema.
