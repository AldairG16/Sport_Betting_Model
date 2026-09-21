# SALIDA_PAPEL — Documento operativo de gobierno

Sustituye al ciclo de auditorías (rondas 1-11). Creado en la ronda 12 con la
corrección G2 (simetría de puertas) y las secciones §0/§6 exigidas para que
el primer corte no mezcle cohortes. Reabre auditorías solo el §5.

**Propuesta de cierre de la ronda 12**: si Q-AH del lunes 28-sep confirma
|Δhome_adv| < 0.08 entre dos fits consecutivos de CI, el ciclo queda cerrado
y este documento gobierna.

---

## §0 — Estado de partida

| Campo | Valor |
|---|---|
| Generación de modelo vigente | `fit_fingerprint = 2026-09-21T11:03:20.359206\|g29.780495` |
| Parámetros | home_adv=0.2211 · rho=−0.0947 · n_matches=21,724 · 736 equipos |
| Desde | lunes 21-sep-2026 (primer morning con MLE y huella en decision_log) |
| Cohortes excluidas del cómputo | TODAS las bets anteriores al 21-sep 12:03 UTC (211 filas): sin `fit_fingerprint` ni banderas de cohorte; construidas con lambdas pre-fix (λ_total≈4), sin MLE y con BTTS en Poisson cruda |
| Próximo refit (CI) | lunes 28-sep — debe heredar warm-start desde Neon (fix G1); criterio de continuidad: \|Δhome_adv\| < 0.08 entre refits consecutivos (Q-AH) |

Ninguna métrica de este documento computa sobre cohortes excluidas.

## §1 — Qué se mide (por mercado, corte semanal automático)

1. Brecha de calibración de bets ancladas: `pred − real`, por mercado.
2. CLV medio con n: `1/closing − 1/odds` (definición fija desde 15-sep).
3. ROI: `Σprofit/Σstake`.
4. CLV shadow por banda de desvío: `shadow_clv_bands` (mercado × banda, con
   `n_banda` y `n_con_closing` separados).
5. Serie de refits: `model_state_history` (home_adv, rho, grad, n_matches).

Todo lleva `fit_fingerprint` como dimensión de cohortes.

## §2 — Con qué n es concluyente cada métrica

**Ventana declarada (H1, ronda 13): ACUMULADA desde el inicio de la
generación (21-sep-2026 12:03 UTC).** No es rodante de 120 días por dos
razones: mezclaría cohortes excluidas en §0 (R12) y no existe dato previo de
la generación vigente.

| Métrica | n mínimo | Mientras no lo alcance |
|---|---|---|
| Brecha anclada | 50 por mercado | se registra, no se decide |
| CLV (habilitar) | **30 por mercado con closing** + espejo estadístico (límite inferior IC95 > 0) | mercado sin habilitación |
| CLV (bloquear) | 30 (gate estadístico, ya activo) | — |
| ROI | 200 | no-descalificador (§3) |
| Shadow por banda | 50 por celda | se registra, no se decide |

**Por qué 30 y no 100 (H1, ronda 13):** el 100 era B1 otra vez — con las
tasas medidas, era inalcanzable en ventana rodante para TODOS los mercados
(over25 topaba en 94). El espejo con n=30 es MÁS estricto en efecto (el
error estándar crece): exige CLV medio ≥ 1.645·sd/√30 — **+1.11pp para
over25 y +2.25pp para home_win** — frente a los +0.61pp poco exigentes del
100. Viabilidad sin perder rigor.

**Tasa de activación por mercado (R16 — el umbral dividido por la tasa):**

| Mercado | /sem baseline (Q-Q, 120d) | /sem post-fix (últimos 8d) | n=30 acumulado en |
|---|---|---|---|
| over25 | 5.5 | **49.0** (56/56 con closing) | 1-5.5 semanas |
| home_win | 2.2 | 6.1 | 5-13.6 semanas |
| btts | 1.3 | 19.3 (verificar closing nuevo) | 1.6-23 semanas |
| dnb_away | 0.76 | 7.0 | 4.3-39 semanas |
| dc_1x | 0.53 | 3.5 | 8.6-57 semanas |

**Si ningún mercado habilita nunca:** el mercado permanece en papel
indefinidamente y NO se le aplican conclusiones ajenas; a las 8 semanas sin
n≥30, la tasa del mercado pasa a revisión trimestral (¿cobertura de closing,
calendario de ligas, filtros?) — el silencio nunca se interpreta como
inocencia (misma lógica que la histéresis del gate).

sd de referencia medido (120d): over25 0.037 · home_win 0.075 · away_win
0.10. La puerta usa el sd DEL mercado, nunca un global.

## §3 — Qué decisión toma cada resultado (umbrales numéricos)

| Resultado | Decisión |
|---|---|
| **Habilitar mercado** (puerta 1): límite INFERIOR del IC95 unilateral del CLV > 0 con n≥100 — el espejo exacto del criterio de bloqueo (G2). Con sd medido: exige CLV medio ≥ +0.6pp (over25) o +1.2pp (home_win) a n=100 | el mercado entra al universe apostable |
| Brecha anclada ≤3pt sostenida + al menos un mercado habilitado + ROI **no-descalificador** (IC95 que contenga 0 — significa "no demostrado que perdemos", NO evidencia a favor) | **piloto real**: 1% de bankroll por apuesta, tope 5u/día |
| CLV shadow positivo SIGNIFICATIVO (límite inferior > 0) en la banda baja de un mercado, n_con_closing ≥ 50 | bajar el piso de ESE mercado a 0.03 (abre exposición → exige prueba) |
| CLV shadow negativo con punto estimado < 0 y n_con_closing ≥ 30 en [19-25) de un mercado | subir el piso de ESE mercado, **acotado por `piso_max`** (H2, ronda 13, ver abajo) |
| **`piso_max(mercado) = 0.35 × (techo − 5pt) − slip(mercado)`** → 1x2 **0.068** · O/U **0.069** · AH **0.066** · BTTS **0.048** | ninguna subida de piso puede superar `piso_max`: dejaría menos de 5pt de ventana viva bajo el techo de 30pt y sería **un bloqueo disfrazado** (H2) — un bloqueo real usa la ruta del gate, con su reactivación por evidencia. Si el techo pasa a ser por mercado, la fórmula se parametriza con el techo de ese mercado |
| Gate de CLV (IC95 < 0, 2 ventanas) | bloqueo automático (ya operativo) |
| \|Δhome_adv\| > 0.08 entre refits consecutivos | cohort="inestable" esa semana; no cuenta para habilitaciones |
| rho en zona de histéresis [0.015, 0.03] | estado anterior manda (implementado ronda 11) |

La asimetría de las dos filas shadow es deliberada: **bajar el piso abre
exposición (exige prueba estadística); subirla la cierra (basta la
estimación)**. Criterio de G2 aplicado en ambas direcciones.

## §4 — Quién y cuándo

- **Hourly (CI)**: closing de bets y shadow; revalidación pre-kickoff.
- **Weekly (CI, lunes)**: refit (warm-start desde Neon — G1) + gate CLV +
  `shadow_clv_bands` + resumen Telegram + Q-AH de continuidad.
- **Humano (martes, 10 min)**: leer el resumen; decisiones de piso/techo
  solo con celda n≥50; registrarlas en la bitácora de §6.

## §5 — Qué reabre una auditoría

1. Brecha anclada > 8pt en ventana de 50 bets.
2. Cualquier mercado con CLV < −5pp y n≥30 SIN que el gate lo haya bloqueado
   (gate no dispara = bug de instrumento).
3. `mle_weight = 0.0` con `model_state` poblado (patrón H2).
4. Reconciliación bankroll descuadrada > 0.5u.
5. \|Δhome_adv\| > 0.08 dos semanas consecutivas (el warm-start dejó de
   heredar — patrón G1).
6. Cambio arquitectónico mayor (nueva fuente, cambio de ancla/piso global):
   cohorte nueva con bandera en decision_log y papel otras 200 apuestas.

## §6 — Bitácora de decisiones

| Fecha | Métrica | n | Valor | Decisión | Autor |
|---|---|---|---|---|---|
| 21-sep-2026 | — | — | — | Creación del documento; corrección G1 (warm-start desde Neon, verificado: hereda 0.2211) y G2 (puerta espejo) | ronda 12 |
| 21-sep-2026 | H1: tasa vs umbral n=100 | Q-Q + post-fix | inalcanzable en rodante; 4.2 meses en acumulada solo over25 | habilitación pasa a n=30 con espejo estadístico; ventana declarada ACUMULADA; tasa por mercado escrita al lado (R16) | ronda 13 |
| 21-sep-2026 | H2: piso 0.08 bajo techo 30pt | — | ventana restante 0.9-4.9pt = bloqueo disfrazado | adoptado `piso_max(mercado) = 0.35×(techo−5pt) − slip`; subir piso más allá = bloqueo con ruta del gate | ronda 13 |
| 21-sep-2026 | Auditoría final externa: I2 (motivación mal enrutada, 80% a goles) + I1 (1x2 vs AH-0.5 con matrices distintas, 1.6pp) | reproducción numérica propia | confirmados ambos | aplicados ANTES del inicio de la cohorte fingerprinted; bandera de cohorte `dc_rho_score` en decision_log separa pre/post | ronda 14 |
| 28-sep-2026 | Q-AH: Δhome_adv entre refits CI | — | pendiente | si <0.08 → ciclo auditor cerrado formalmente | pendiente |
| *(siguiente fila)* | *métrica* | *n* | *valor* | *acción tomada* | *quién* |

---

**CIERRE DEL CICLO (ronda 13).** Doce rondas llevaron el sistema de "el
modelo infla los goles un 60% con toda su capa adaptativa muerta" a λ
correctos a 1e-16 de la media de liga, estado persistido en Neon,
contabilidad atómica, gate estadístico con histéresis, shadow que mide el
rango correcto y agrega en la unidad correcta, cohortes por huella de fit,
266 tests, y este documento con criterios numéricos y tasas de activación
declaradas. **A partir de hoy el gobierno del sistema es
`docs/SALIDA_PAPEL.md`; los disparadores de su §5 son la única vía de
reapertura del ciclo de auditorías.**

---

### Nota de diseño G1 (herencia del warm-start)

Se eligió **continuar la cadena vía Neon** (cada refit parte del anterior) y
no el arranque limpio semanal: el arranque limpio costó fun 5500 vs 5198
(+45% en λ_home) y habría puesto ese salto EN producción cada lunes. La
cadena es auditable (`model_state_history` + `fit_fingerprint` por fila) y
el §5-5 la vigila: si la cadena produce dos saltos >0.08 seguidos, se
reabre.

### Declaración R15 — lecturas restantes de `DC_PARAMS_FILE`

Tras el fix G1 quedan tres: (1) `data/dc_params.json` como **escritura**
(caché local, informativa), (2) `_load_params` como **fallback** cuando Neon
no responde (correcto: el orden es base-primero), (3) scripts de validación
locales (tooling, no producción). Ninguna decide comportamiento en CI.
