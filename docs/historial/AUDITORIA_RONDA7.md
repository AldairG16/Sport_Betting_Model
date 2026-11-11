# Auditoría Ronda 7

Corte de datos: **20-sep-2026** (domingo, noche). Commit de fixes: **f2c5ae1**.
Suite: **237 passed**. Slate vigente: 153 partidos.

## 0. Regresiones: re-verificación de rondas 4, 5 y 6 (R8)

| Check | Estado |
|---|---|
| D4/D5/D6/D9/D11/D13 (tests r4) | OK |
| N1/N2/N5 (tests r5) | OK |
| A1-A5 (tests r6): piso 0.05, techo 0.30, sin cuotas fabricadas, guardias, `+.2f`×8 | OK (`test_round6_thresholds_unchanged`) |
| Gate CLV: nueva firma estadística no rompe `load_clv_blocked_*` consumidos por el pipeline | OK (suite + import) |

## 1. Tabla resumen

| ID | Posición de A | Mi veredicto | ¿Quién tenía razón? | Evidencia | Acción |
|----|---------------|--------------|---------------------|-----------|--------|
| B1 | El gate de CLV (n≥100 fijo) no puede activarse para mercados <30-40% del flujo | **Confirmado y agravado**: con tasas reales, NINGÚN mercado llega a 100 en ventana (máximo: over25, 94). El gate jamás pudo dispararse | A | Q-Q, Q-S | Criterio estadístico (IC95 unilateral <0, n≥30) + bloqueo explícito de ah_*_fav (f2c5ae1) |
| B2 | La ventana [19.1, 30pt] excluye la ventaja pequeña; propongo shadow logging | **Confirmado el diagnóstico** (aritmética correcta, slip real medido por mercado); **propuesta implementada** con una salvedad de selección (§3.2) | A, con matiz mío | vig por mercado, corrida en vivo | `shadow_bets` + closing compartido + `shadow_clv_bands` semanal (f2c5ae1) |
| B3 | §2.2 r6 contradice §10 r6 (R5) | **Confirmado** — la frase del §2.2 era falsa tal como estaba escrita | A | errata aplicada | Erratas en r6 §2.1/§2.2 + este documento |

## 2. B1 — Dimensionamiento del gate de CLV

### 2.1 Q-Q: tasa real por mercado y cobertura de closing

```
AFIRMACIÓN:  Con las tasas REALES (no el rango 15-40/sem que A tomó de mi
             §2.4), el gate n≥100 es inalcanzable para TODOS los mercados.
COMANDO:     Q-Q: COUNT(*) por mercado, últimos 120 días
SALIDA:      over25: 94 (5.5/sem, 81 con closing)   ← el MÁXIMO
             home_win: 38 · btts: 22 · cards_under_4.5: 17 · dnb_away: 13
             total del período: ~225 bets = 13.1/semana
CONCLUSIÓN:  [MEDIDO] Peor que la tabla de A: ni siquiera el mercado
             más grande llega a 100. El gate de mercados JAMÁS pudo
             dispararse — era decorativo, exactamente lo que R10
             prohibe. La tabla de A daba "activable" a over25 con 129;
             la realidad es 94.
```

Y la cobertura de closing agrava el umbral efectivo (Q-S: 69-85% semanal;
por grupo 120d: ou25 81/95, 1x2 35/38, **btts 1/23, dc 0/14, dnb 5/13**).
El closing de BTTS y DC prácticamente no se captura — por eso el gate viejo
nunca vio esos mercados aunque hubiera tenido n. Causa probable: esas odds
llegan por enrichment tardío y la fila de `upcoming_matches` se limpia a los
7 días antes de que el hourly de cierre la encuentre. Queda como observación
con mecanismo de medición (shadow usará el mismo mapeo; su cobertura se
reporta semana a semana).

### 2.2 Tasa de activación declarada (R10)

Con el criterio nuevo (n≥30 con closing, ver 2.3) y las tasas Q-Q:

| Mercado | bets/sem con closing | semanas hasta n=30 | ¿antes (n=100)? |
|---|---|---|---|
| over25 | 4.7 | ~6.4 | 21 (y no llegaba: 94<100) |
| home_win | 2.0 | ~15 | nunca (38) |
| cards_under_4.5 | 1.25 | ~24 | nunca (17) |
| dnb_away | 0.4 | ~75 | nunca (13) |
| btts | 0.06 | ~490 → nunca | nunca (1 con closing) |
| dc_1x | 0 | **nunca** | nunca (14) |
| ah_* (cualquiera) | ~0.5 | nunca | nunca (9) |

Declaración R10: el gate estadístico es **real** para over25 y home_win,
lento para cards/dnb, y **sigue siendo inalcanzable** para btts, dc y AH —
para esos, la protección mientras tanto es la static: btts_no/ah_pk ya
tienen piso de liquidez; AH favoritos van bloqueados por decreto (2.3); y
el shadow logging mide CLV sin necesidad de apuesta (§3), que es la vía de
salida real para los mercados de flujo bajo.

### 2.3 Criterio nuevo + piso para ah_*_fav

**(a) adoptado con la fórmula exacta.** `scripts/clv_gate.py`:
`market_clv_blocked(n, mean, sd)` → bloquea cuando
`mean + 1.645 · sd/√n < 0` con `n ≥ 30` (IC95 unilateral de una normal;
con sd de CLV ~0.04, un efecto real de −1 a −2pt dispara con n=30-40).
El criterio viejo (n≥100 ∧ mean<−0.005) queda como campo legado en el
payload. Tests: dispara con efecto real y n=35, perdona ruido leve,
no dispara bajo n=30, y con n=200 exige efecto ≥ −0.8pt.

**(b) adoptado en versión mejorada — bloqueo, no piso.** A proponía un piso
estático más alto para `ah_*_fav`. Cualquier piso que proteja de verdad
(≥0.12) exige desvío ≥40pt bajo el ancla — por encima del techo 0.30 — y
los vuelve a matar por aritmética, el mismo estado que la ronda 6
denunciaba, disfrazado. Lo instalado: **bloqueo explícito en el filtro**
(`_ah_group(mkt) in ("ah_home_fav", "ah_away_fav")`, clubes; paper excluido)
con ruta de reactivación declarada: CLV de la banda shadow de favoritos AH
≥ 0 con n≥30. Limitación declarada de la ruta (R10 aplicada al propio
arreglo): el punto de bloqueo está antes del piso de edge y no tiene
captura shadow propia, así que su CLV de reactivación se aproxima con los
registros AH adyacentes (pk/dog, que sí capturan). Si el weekly muestra que
esa aproximación es pobre, se añade un punto `_shadow("blocked_ah_fav")` en
ese `continue` — una línea, pendiente de evidencia de que hace falta.

## 3. B2 — La ventana de apuesta

### 3.1 Slip real por mercado → ventana efectiva por mercado

Vig medido sobre el slate vigente (pares, promedio de booksum):

| Mercado | booksum | slip por pata | desvío mínimo (piso 0.05) | ventana |
|---|---|---|---|---|
| 1x2 | 1.0226 | ~0.8pt | ~16.6pt | [16.6, 30] |
| over/under | 1.0376 | ~1.9pt | ~19.7pt | [19.7, 30] |
| AH | 1.0450 | ~2.2pt | ~20.3pt | [20.3, 30] |
| btts | 1.0795 (n=3) | ~4.0pt | ~25.8pt | [25.8, 30] |

Diagnóstico de A confirmado con matiz: la ventana no es uniforme [19.1, 30]
— es [16.6-25.8, 30] según mercado. En BTTS la ventana es casi inexistente
(4.2pt de ancho); en 1x2 es la más amplia. La conclusión central de A se
sostiene: bajo el ancla 65/35, la ventaja pequeña (≤10pt de desvío) es
inelegible por construcción, y discrepar ~20pt del mercado es raro por
definición.

### 3.2 Shadow logging: implementado (f2c5ae1), con una salvedad de selección

Evaluación de la propuesta: correcta y barata. Implementación:

- **Captura**: 7 puntos de rechazo en el filtro de candidatos
  (`odds_band` ×3, `edge_floor`, `deviation_cap`, `no_reference_price`,
  `max_odds`) registran mercado, liga, p_final, precio de referencia,
  **desvío con signo**, cuota y edge (closure `_shadow` en el pipeline).
- **Persistencia**: `persist_shadow_bets()` en save_bets con
  `UNIQUE (match, market, match_date)` — las re-corridas del mismo slate no
  duplican. Validado en vivo: la corrida de hoy insertó 2 filas, justo los
  casos de frontera (under25 desvío 17.9pt por piso; btts_no 31.8pt por
  techo).
- **Closing**: refactor del mapeo a funciones compartidas
  (`_nearest_market_row`, `_closing_odds_for`) usadas por bets_history y
  shadow_bets por igual — cero duplicación de mapeo (R6). El weekly imprime
  `shadow_clv_bands()` (bandas [0-5), [5-10), [10-15), [15-19), [19-25),
  [25-30), [30+]).
- **Salvedad de selección (la que A pidió buscar y existe)**: las candidatas
  entran al shadow solo si tienen edge positivo (salen de `find_value_bets`),
  así que el shadow cubre desvíos ≥ ~5.5pt (donde edge>0), sesgado al lado
  donde el modelo está ENCIMA del precio. No medirá el lado "modelo debajo
  del precio" — para el propósito de B2 (¿hay skill en 5-15pt al alza?) es
  suficiente, pero cualquier extrapolación a "el modelo ve todo mejor" sería
  inválida. Documentado, no corregido: capturar el lado negativo requeriría
  tocar find_value_bets y duplicaría volumen sin responder la pregunta de la
  ventana.

### 3.3 La elección: (b), con (a) y (c) como salidas medibles

Elijo **(b)** — aceptar la ventana [piso→desvío, 30pt] y verificarla en
papel — como posición activa, porque:

1. La única medición de la generación vigente (Q-A/Q-H, n=8-32) vive dentro
   de esa ventana; mover el piso hacia abajo AHORA sería apostar en la
   región donde no tengo ni una medición del modelo actual.
2. El shadow logging convierte (a) y (c) de apuestas teóricas en decisiones
   condicionales: si `shadow_clv_bands` muestra CLV positivo significativo
   en [5-15) con n≥50, se baja el piso (→(c)); si lo muestra solo dentro de
   [19-30), (b) queda validada con datos propios. Y el weight-por-mercado
   de (a) requiere skill medido por mercado — el shadow lo produce gratis.
3. Cambiar el ancla por mercado hoy, sin esa medición, repetiría el patrón
   que las 6 rondas fueron cerrando: decidir con números elegidos.

## 4. B3 y el encuadre del techo de 30pt

- **B3**: errata aplicada en `docs/AUDITORIA_RONDA6.md` §2.2 (la frase
  "la decide el gate dinámico de CLV" era falsa y contradecía al §10; con
  [ERRATA, ronda 7] y el texto corregido in situ). R5 restaurada.
- **Encuadre del 30**: errata en §2.1 de r6 — **el 30pt es una elección
  prudencial dentro de una región observada con n=8-32, no una derivación**.
  Qué lo movería, con umbrales: CLV shadow positivo en banda [25-30] o
  [30+] con n≥50 → revisar subida del techo (en pasos de 5pt, re-midiendo);
  CLV negativo significativo (criterio del gate, §2.3) en [19-25) → bajar
  piso y techo juntos.

## 5. Q-S, Q-T (¿llegó el MLE a producción?)

```
COMANDO:    SELECT key, updated_at FROM model_state WHERE key='dc_params'
SALIDA:     ERROR: relation "model_state" does not exist
CONCLUSIÓN: [MEDIDO] El weekly post-H2 aún no ha corrido: el fix H2 se
            instaló el sábado 20-sep y el weekly dispara el LUNES 21-sep
            13:00 UTC. No es un fallo — es el calendario. Verificación
            programada: si el lunes por la tarde model_state sigue vacío
            o el decision_log del morning del martes sigue con
            mle_weight=0.0, H2 se reabre como incidente, no como hallazgo.
```

Q-S: cobertura de closing 69-85% semanal, con colapso en btts (1/23) y dc
(0/14) — §2.1. El shadow hereda ese límite de cobertura y lo hará visible
semana a semana (la función reporta n por banda).

## 6. Derecho de réplica

### R-1
```
OBJETO:     La tabla de A que daba "gate activable" a over25 (15/sem, 50%
            de cuota → 129 en ventana).
MI POSICIÓN: Con las tasas REALES el gate era inalcanzable incluso para
            over25: 94 bets en 120d, no 129.
COMANDO:    Q-Q (SELECT market, COUNT(*) ... INTERVAL '120 days')
SALIDA:     over25: 94 · home_win: 38 · btts: 22 · (total 225 ≈ 13.1/sem,
            no 15-40/sem como asumía la tabla)
POR QUÉ A SE EQUIVOCA: usó mi estimación de ritmo de la ronda 6 (§2.4,
            hecha con el slate de fin de semana) como si fuera tasa
            sostenida; el promedio real de 120 días es un tercio de la
            cota alta. Su conclusión (B1) era correcta; su magnitud,
            optimista.
ALCANCE:    refuerza B1 — y corrige mi propia proyección de acumulación de
            la ronda 6 (§2.4), que también usaba el slate de un solo día.
```

### R-2
```
OBJETO:     "Bajo un anclaje del 65%, un modelo bueno no puede ganar
            dinero" (B2).
MI POSICIÓN: Es cierta en el sentido operativo pero falsa en el absoluto:
            el ancla YA INCORPORA la señal del modelo en el precio final,
            así que la pregunta no es "¿puede ganar un modelo bueno?" sino
            "¿dónde está el óptimo de peso?". Un modelo bueno bajo ancla
            65% gana si el mercado está mal calibrado EN LOS PARTIDOS que
            la ventana selecciona — lo imposible no es ganar, es SABERLO
            con ventajas de 5pt, que el diseño hace invisibles.
COMANDO:    la aritmética de la propia ventana (edge = 0.35·desvío − slip)
SALIDA:     ventaja de 5pt → edge 0.0005: indistinguible de cero. Cierto.
POR QUÉ MATIZA: la frase de A suena a "el diseño anula el skill"; lo
            demostrable es "el diseño hace INVISIBLE el skill pequeño" —
            y por eso la respuesta es medirlo en la sombra (implementado),
            no concluir que el modelo no puede ganar.
ALCANCE:    ninguno operativo — la elección (b) y el shadow logging son
            exactamente los que A proponía; solo cambia el encuadre.
```

## 7. Hallazgos propios de esta ronda

1. **El gate de CLV nunca pudo dispararse para ningún mercado** — no era
   "lento", era decorativo desde su instalación (R10 tenía razón y la
   magnitud real la dio Q-Q, no la estimación de nadie).
2. **La cobertura de closing colapsa en btts (1/23) y dc (0/14)**: CLV y
   gate para esos mercados eran ficción doble — sin muestra Y sin precio de
   cierre. El shadow hereda el límite y ahora lo hace visible por banda.
3. **La ventana apostable no es una, son cuatro** (por vig de mercado:
   1x2 [16.6,30], O/U [19.7,30], AH [20.3,30], BTTS [25.8,30]) — el piso
   uniforme de edge produce techos y pisos de desvío distintos por mercado.
   El caso extremo: BTTS con ventana de 4pt de ancho.
4. La proyección de acumulación que hice en la ronda 6 (15-40/sem) estaba
   inflada ~3× por usar el slate de un solo día: el ritmo real de 120 días
   es 13.1/sem. La condición de salida de papel de §10 usa ahora ese ritmo.
5. El shadow logging, como pidió A que buscara, tiene un sesgo de selección
   estructural: solo captura el lado "modelo encima del precio" (los
   candidatos nacen de find_value_bets con edge>0). Suficiente para la
   pregunta de la ventana; insuficiente para declaraciones generales de
   skill.

## 8. Correcciones aplicadas

Commit: **f2c5ae1**. Suite: 237 passed. Erratas en r6 aplicadas en el mismo
push.

| Arreglo | Archivos | Ocurrencias (R6) | Test | ¿Reintroduce? (R8) | ¿Parámetros re-derivados? (R9) | ¿Tasa de activación declarada? (R10) |
|---|---|---|---|---|---|---|
| B1(a) gate estadístico | scripts/clv_gate.py (`market_clv_blocked`, criterio en run) | 1 sitio de decisión, 1 tocado | 4 tests (dispara/permone/mínimo/n grande) | No | Sí (n≥30 + IC95) | Sí — §2.2, tabla por mercado |
| B1(b) bloqueo ah_*_fav | prediction_pipeline.py (filtro) | 1 sitio | `test_ah_favorites_explicitly_blocked` | No | Sí (bloqueo + ruta de reactivación declarada) | Parcial — reactivación condicionada a shadow n≥30; sin captura shadow propia (§9) |
| B2 shadow logging | prediction_pipeline.py (closure `_shadow`, 7 puntos), save_bets.py (`persist_shadow_bets`, `_update_shadow_closing`, refactor `_nearest_market_row`/`_closing_odds_for`), orchestrator.py (bandas semanales) | 7 puntos de captura; mapeo closing: 2 consumidores, 0 duplicados | 6 tests (puntos, persistencia, dedupe, mapeo compartido, bandas) | No — el refactor de closing conserva el mapeo exacto | Sí (bandas = medición futura del piso/techo) | Sí — cobertura de closing reportada semanalmente |
| B3 erratas | docs/AUDITORIA_RONDA6.md | 2 secciones | — | — | — | — |

## 9. Lo que sigo sin poder afirmar

- Que el gate estadístico no produzca falsos positivos con sd anómalas
  (protección: solo bloquea, y desbloquea solo por rotación — revisable con
  datos del primer trimestre).
- El CLV de las bandas bajas [5-15): la tabla `shadow_bets` arranca HOY con
  2 filas; el primer corte con potencia estadística es ~4-6 semanas.
- La reactivación de favoritos AH: sin captura shadow propia del bloqueo
  (cae antes del piso de edge), su CLV de banda se aproxima con registros
  AH adyacentes. Si el weekly muestra que esa aproximación es pobre, se
  añade un punto de captura específico.
- El MLE en producción: verificación del weekly del lunes 21-sep (§5).

## 10. Veredicto y condición de salida de papel

Papel, sin cambios en las tres puertas (brecha anclada ≤3pt; CLV ≥0 con
n≥100 en un mercado principal; ROI con IC95 conteniendo 0). Corrección
honesta de la ronda 6: con el ritmo real de **13.1 apuestas/semana**, las
200-300 apuestas de la condición tardan **~4-6 meses**, no 2-3.

Añadido de esta ronda: la decisión B2 queda **nombrada y con mecanismo de
revisión** — elección (b) (ventana actual verificada en papel), con (c)
(bajar el piso) y (a) (ancla por mercado) como salidas activables por datos:
el primer `shadow_clv_bands` con n≥50 por banda dispara la revisión
programada, y cualquier CLV negativo significativo dentro de [19-25) dispara
una revisión de emergencia del piso y el techo.
