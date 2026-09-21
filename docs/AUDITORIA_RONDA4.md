# Auditoría Ronda 4

Corte de datos: **20-sep-2026** (post-morning, 1,162 bets en `bets_history`).
Estado global medido hoy: **ROI −8.2%** en 1,135 resueltas, WR 45.1%.

## 0. Método — el cambio de procedimiento

Tres rondas declarando H3 corregido sin verificación es un fallo de proceso, no
de conocimiento. El cambio concreto, verificable en esta ronda:

1. **Cada arreglo lleva un test con el ID del defecto en el nombre**
   (`tests/test_round4_audit.py`: `test_clv_gate_binds_both_blocked_sets` para
   H3, `test_resolver_*` para D10, etc.). El test falla si el bug regresa —
   incluida la forma exacta del NameError de H3 (reload del módulo con
   loaders parcheados, no una comprobación que ambas ramas cumplen).
2. **Ningún cierre sin `grep -rn` contado en el documento** (sección 7).
   El conteo es lo que detectó que el docstring de `league_calibration.py`
   seguía enseñando la fórmula bug (R6, hallazgo propio #1).
3. **Ninguna métrica sin fecha de corte y etiqueta PRE/POST** (secciones 2-3).

## 1. Tabla resumen

| ID | Posición de A | Mi veredicto | ¿Quién tenía razón? | Acción |
|----|---------------|--------------|---------------------|--------|
| H3 | NameError en el gate CLV deja ambos kill-switches vacíos | **Confirmado con el código** | A | Fix 074eeac + test de regresión |
| PARTE-1(a) | La mejora post-anclaje no es distinguible de ruido | Confirmado (n=17/65, SE ~12pt) | A | Reportado con intervalos; sin claim de mejora |
| PARTE-1(b) | La brecha bajó por dilución, no por mejora | **Parcialmente refutado por Q-A**, pero no decidible con este n | Misto, ver §2 | — |
| PARTE-2 | λ_total post-fix = 4.05, no 2.80; el ancla no mueve λ | **Confirmado por Q-B** — y peor de lo que A dijo: el fix dimensional fue un no-op en producción | A | Ver §3 |
| D4 | HOME_ADVANTAGE es reparto, no multiplicador (+22.7% residual) | **Confirmado** (aritmética + Q-B + simulación) | A | Fix 074eeac (`compute_lambdas`) |
| D5 | Fix de λ en 1 de 2 lugares; señal 3 ignora la liga | **Confirmado** (`ensemble_model.py:79`) | A | Fix 074eeac |
| D6 | `btts_yes` nunca se ancla | **Confirmado** (código + Q-C: 0 ocurrencias en producción) | A | Fix 074eeac |
| D7 | Calibración de mayo mide un modelo que ya no existe | Confirmado como riesgo; **sin refit posible todavía** | A | Procedimiento en §4-D7 |
| D8 | El cap 2.5 existía para tapar el error dimensional | Confirmado: post-fix recortaba aún el 58% de λ_home (Q-B) | A | Cap 3.5 + over25 0.80, justificados |
| D9 | Reparto 1T/2T suma 105% | Confirmado; medido: 1T real = 44% (4,559 partidos) | A | Fix 074eeac (44/56) |
| D10 | Resolver falla hacia pérdida en mercados sin rama | Latente confirmado en código; **sin víctimas en producción** (Q-D) | A (riesgo), sin daños | Fix 074eeac (unresolved) |
| D11 | `_BASELINE` duplicado a mano | Confirmado | A | Fix 074eeac (import KALMAN_BASELINE) |
| D12 | Los shades deshacen parte del anclaje | Correcto el dato, matizada la lectura — decisión: tope ±5pt | Misto | Fix 074eeac |
| D13 | Los tests no detectan nada de esto | Confirmado (197 pasaban con λ movidos 25%) | A | 9 tests nuevos |

## 2. La brecha de calibración: dilución o mejora (Q-A)

```
AFIRMACIÓN:  Con corte desde el 14-sep (era anclada), la brecha de los
             mercados anclados es 1.7pt, no ≈0.35×8.7=3.0pt como predice
             la dilución pura; y la no-anclada sigue en 8.7pt.
COMANDO:     SELECT CASE WHEN (decision_log->'model'->'anchored') ? market
                  THEN 'anclada' ELSE 'no_anclada' END AS tipo, COUNT(*) n,
                  ROUND(AVG(probability)::numeric,4) pred,
                  ROUND(AVG((result IN ('win','half_win'))::int)::numeric,4) real,
                  ROUND((AVG(probability)-AVG((result IN ('win','half_win'))::int))::numeric,4) brecha
                FROM bets_history WHERE result IN ('win','loss','half_win','half_loss')
                  AND decision_log IS NOT NULL AND match_date >= '2026-09-14' GROUP BY 1;
SALIDA:        tipo      n   pred    real   brecha
             anclada    17   0.6055  0.5882  0.0173
             no_anclada 65   0.5795  0.4923  0.0872
CONCLUSIÓN:  La dilución pura predice brecha(anclada) ≈ 0.35 × 8.72 = 3.05pt;
             se observa 1.73pt — menos de lo que la dilución explica, en la
             dirección contraria a A. PERO: n=17 con SE de la brecha ~12pt
             (CI95 ≈ ±23pt) → este test NO decide nada con este tamaño.
             [MEDIDO, no decidido]
```

**Por qué la pred media subió a 58.5% tras anclar (pregunta de A).** Es un
efecto de selección, no una paradoja. Con `p_final = 0.65·mercado + 0.35·modelo`
y MIN_EDGE 9%: sobrevive solo `0.35·(modelo − mercado) > 9pt`, es decir
`modelo > mercado + 25.7pt`. El filtro selecciona la cola extrema de los
desacuerdos modelo-mercado, así que la media de la prob final se queda arriba
(~58-61%): el ancla comprime el desacuerdo ×0.35 y el umbral de edge lo
vuelve a estirar hasta ~9pt. Antes del ancla el umbral equivalente era 5pt de
desacuerdo crudo → la muestra pre era menos extrema. Esto es consistente con
lo que A señala ("el filtro sigue seleccionando los partidos donde el modelo
más se equivoca") y es exactamente la razón para no leer la mejora del ROI
post como señal.

## 3. Mediciones con fecha de corte (Q-B a Q-F)

### Q-B — λ pre/post fix dimensional (corte: commit baada73, 19-sep)

```
      era        n  lam_h  lam_a  lam_total  capped
 1_pre_fix     113  2.240  1.763      4.003      80
 2_post_fix     74  2.200  1.850      4.051      43   (58% en el cap)
```

**Concesión total a A, y agravada:** mi tabla de la ronda 3 atribuía
`λ_total 4.02 → 2.80` a dos cambios; el ancla no puede mover un λ (correcto)
y el fix dimensional tampoco lo movió en producción: 4.00 → 4.05. El número
2.80 no era una medición de nada — era una proyección ilegítima sobre una
media truncada (exactamente el punto de A: 80/113 filas estaban pegadas al
cap 2.5, la media pre no proyecta nada). El cap absorbía la corrección:
por eso el fix de esta ronda es el **reparto** (D4), no otra división.

### Q-C — composición real del anclaje (desde 14-sep, n=124)

```
["away_win","btts_no","draw","home_win","over25","under25"]  73
[]                                                           50
["away_win","draw","home_win","over25","under25"]             1
```

`btts` (yes) no aparece ni una vez → **D6 confirmado en producción**. Dato
nuevo que ninguno de los dos había reportado: **el 40% de las bets de la era
anclada no tiene ningún mercado anclado** (50/124 con `[]`) — son AH, HT,
córners y tarjetas, mercados sin devig confiable. La arquitectura de anclaje
cubre ~60% de la producción, no el 100% que mi narrativa de la ronda 3
implicaba.

### Q-D — ¿algún mercado cae al default del resolver?

55 nombres de mercado distintos en `bets_history`; todos tienen rama en el
resolver (incluidos los paramétricos `ah_*`, `corners_*`, `cards_*`,
`shots_*` vía `startswith`). **Sin víctimas** — D10 era un riesgo latente,
no un daño activo. El fix se aplica igual: el default ahora marca
`unresolved` sin tocar bankroll.

### Q-E — CLV por mercado, unidades declaradas

La columna `clv` se define como `1/closing_odds − 1/odds` (**unidades de
probabilidad**, no %). Corte 120 días:

```
market    n   avg_clv_prob
over25   69     −0.00201   (−0.20pp)
home_win 29     −0.00011   (−0.011pp)
```

La ambigüedad que A señala se resuelve así: el "−0.011%" de mi ronda 3 era
este número (−0.011 **pp**) con la etiqueta de unidad mal puesta; el +4.2pp
de `clv_cache.json` (n=62) es otra ventana y otra definición previa al cambio
de fórmula del 15-sep. Con la definición actual: **CLV ≈ 0 en ambos
mercados, n insuficiente**. El gate de CLV sigue siendo el árbitro cuando
haya n≥100 por mercado.

### Q-F — MLE en producción

```
mle_w   n
 0.0    187     (corte 20-sep; antes 187/187, hoy igual)
```

Confirmada H2 de forma definitiva. **Causa raíz encontrada esta ronda:**
`data/dc_params.json` está en `.gitignore` y el fit corre en el runner
efímero del weekly — el morning corre en OTRO runner que jamás ve el
archivo, así que `is_params_fresh()` es siempre False. No era un problema
del fitter ni del blend: era logística de despliegue. Fix 074eeac: el fit
persiste en Neon (`model_state.dc_params`) y el loader lee Neon primero,
archivo como fallback. **De acuerdo con A en guardar el estado en
PostgreSQL** — ya es la fuente de verdad de todo lo demás; el filesystem de
CI no es estado.

## 4. Defectos D4-D13

**D4 (confirmado, corregido).** `HOME_ADVANTAGE` está documentado y usado
como reparto (`avg_home_goals / avg_away_goals`); aplicado como
multiplicador solo a λ_home daba total `1.35·T·(1+HA)`. La aritmética de A
reproduce `LEAGUE_FACTORS` al decimal (EPL: 1.851/1.524/3.375 contra mi
lectura del mismo código). Fórmula nueva (074eeac, `compute_lambdas`):
`μ=2.5·T`, `μ_h=μ·HA/(1+HA)`, `μ_a=μ/(1+HA)`, λ = (a/B)(d/B)·μ. En
equipos promedio: total = 2.5·T y razón = HA exactos (test). **En las colas,
como A pidió que dijera:** el producto de ratios independientes sobre-dispara
(ratings 2.4 vs 1.0 → λ≈4.9 en EPL antes del cap) — es inherente a la forma
multiplicativa de Dixon-Coles, no a esta parametrización; el guardia es
`LAMBDA_CAP=3.5`. Nota honesta: la *media* poblacional del producto lleva un
sesgo +cv² (~+7% con sd de ratings 0.26 goles); la mediana es exacta.

**D5 (confirmado, corregido).** La forma bug quedó en `_form_probs`
(`ensemble_model.py:79`, R6: 1 llamada, corregida). La señal 3 además usaba
defaults 1.10/1.20 ignorando la liga: `ensemble_predict` ahora recibe
`home_advantage/tempo` del pipeline (074eeac). Verificación de A sobre
córners/tiros: **correcta** — `corners_stats.py` produce ratings en escala
ratio (`corners_for / CORNERS_BASELINE`), así que `a·d·HA·BASELINE` en
córners y tiros es dimensionalmente correcta y no se tocó.

**D6 (confirmado en código y producción, corregido).** `ANCHOR_MAP` ahora usa
`"btts": "btts"`. Los dos lados del binario se anclan y `btts_no` se deriva
del capped `btts_yes` — vuelven a sumar 1.

**D7 (confirmado como riesgo, ABIERTO — procedimiento).** La calibración de
mayo (GLOBAL_CALIBRATION, isotónicas, MIN_EDGE, whitelist) fue ajustada contra
el modelo inflado; el modelo cambió tres veces en 6 días (ancla 14-sep,
dimensional 19-sep, reparto 20-sep). Procedimiento decidido:
1. Los mercados anclados ya saltan el shrink global (correcto: heredan la
   calibración implícita del mercado).
2. **Refit de isotónicas y factores solo cuando la generación post-074eeac
   acumule ≥300 resueltas** (n mínimo para isotonic estable por mercado con
   n≥30 por bucket). Antes: no refit, porque sería calibrar ruido.
3. MIN_EDGE y whitelist no se tocan hasta ese refit (dirección conservadora).
El estado "modelo nuevo + correcciones viejas" es real; la salida no es
afinar hoy sino **congelar el modelo y acumular la generación nueva**.

**D8 (decidido con simulación).** Con la fórmula de reparto, el cap 2.5
recortaba aún ~9.6% de λ_home legítimos en EPL (simulación, n=200k, ratings
lognormal sd 0.26); el cap 3.5 recorta ~1.3% — puro guardia de cola para
errores de datos (motivación, congestión, H2H). `over25` en 0.75 truncaba
~13% de la masa Poisson legítima (λ_total≈4 → 0.76); sube a 0.80 (~7%).
`btts` se queda en 0.75: exigiría ambos λ≥2.5, cola genuina. La simulación
de A (normal 1.35/0.35) daba 20%: la diferencia con mi 9.6% es la
distribución de ratings, no la conclusión — el cap estaba haciendo el
trabajo del fix en ambos números.

**D9 (confirmado y medido, corregido).** Fracción real del 1T sobre 4,559
partidos con descanso publicado (12 ligas): media **0.444**, rango
0.426-0.471. `HT_FIRST_HALF_FRACTION = 0.44`; 1T+2T = 100%. El 0.55 restante
en el pipeline (línea de stake) es otro dominio, no se tocó.

**D10 (riesgo latente confirmado, corregido, sin víctimas).** El default
`loss` ahora solo aplica a mercados con rama (`is_resolvable_market`); un
nombre desconocido marca `unresolved` con profit 0 y log visible.

**D11 (confirmado, corregido).** `compute_lambdas` usa `KALMAN_BASELINE`
importado de `team_form`; ya no existe copia manual en el pipeline (R6:
`grep "= 1.35$"` → 0 en pipeline; queda `OVER15_ODDS = 1.35`, que es una
cuota, otro dominio).

**D12 (decisión, no bug).** A tiene el dato correcto: los shades corren
después del ancla. Mi posición: son deliberadamente la señal que el mercado
no ve, y aplicarlos ANTES del ancla los diluiría ×0.35 — matarlos sería
renunciar al único edge idiosincrático del sistema. Pero el argumento del
tope es correcto: sin límite, el "35% declarado" es falso. Compromiso
aplicado: **desviación total post-ancla acotada a ±5pt** del valor anclado
(`_ANCHOR_DEV_CAP`). Una señal que necesite más de 5pt de movimiento no
debería pasar el filtro de edge de todos modos.

**D13 (confirmado, corregido).** Nueve tests en `tests/test_round4_audit.py`
cubren las cuatro aserciones pedidas + regresiones H3/D6. El reload del
módulo en el test de H3 verifica el binding real de los loaders, no una
propiedad que ambas ramas cumplen (R3).

## 5. Derecho de réplica

### R-1
```
OBJETO:      "Si es cierta, las apuestas ancladas deben mostrar
             aproximadamente el 35% de la brecha de las no ancladas,
             en el mismo período" (test Q-A de A).
MI POSICIÓN: Ese test, por construcción, NO puede decidir la hipótesis de
             dilución, porque los dos grupos no son el mismo modelo sobre
             el mismo mercado: el grupo no-anclado está compuesto por los
             mercados SIN devig (AH, HT, DC, córners, tarjetas).
COMANDO:     SELECT COALESCE(decision_log->'model'->>'anchored','[]') anclados, COUNT(*) n
             FROM bets_history WHERE decision_log IS NOT NULL
               AND match_date >= '2026-09-14' GROUP BY 1 ORDER BY 2 DESC;
SALIDA:      ["away_win","btts_no","draw","home_win","over25","under25"]  73
             []                                                          50
             ["away_win","draw","home_win","over25","under25"]            1
POR QUÉ A SE EQUIVOCA: La brecha del grupo no-anclado (8.72pt) mide la
             calibración del modelo crudo en AH/HT/córners — mercados donde
             ni siquiera existe prob de mercado para anclar. "Lo que hubiera
             pasado sin anclar en los mercados anclados" no es observable en
             estos datos: el anclaje es determinístico por tipo de mercado.
             La comparación correcta (mismo mercado, con y sin ancla) no
             existe en producción y no puede existir retrospectivamente.
ALCANCE:     La lectura "dilución confirmada" de A cae; la lectura "el
             anclaje corrige" TAMBIÉN cae (mi §2 lo dice con los intervalos).
             Q-A con este diseño solo acota: brecha(anclada)=1.7pt ± 23pt.
```

### R-2
```
OBJETO:      La descomposición de A en PARTE 1(b): "brecha_observada =
             0.35 × error_del_modelo" (asume mercado calibrado).
MI POSICIÓN: El supuesto es comprobable parcialmente y NO lo verificó ni lo
             etiquetó como supuesto — pero mi objeción es de método, no de
             número: no dispongo de medición del error del mercado en esta
             submuestra (requeriría devig del trío 1x2 por partido y su
             cruce con resultados; no está en bets_history).
COMANDO:     — (no ejecutado; declarado como no medible con el esquema actual)
SALIDA:      —
POR QUÉ IMPORTA: Si el mercado no está calibrado en esta submuestra de
             17-82 bets (y con ese n, cualquier mercado está "mal calibrado"
             por ruido), el 20.9pt de error implícito que A deriva es un
             número construido sobre dos capas de incertidumbre sin
             intervalo. No lo uso como conclusión en ningún sentido.
ALCANCE:     Solo la cuantificación de A (20.9pt); su objeción de fondo (no
             confundir dilución con mejora) sigue en pie y la acepto.
```

## 6. Hallazgos propios de esta ronda

1. **El fix baada73 fue un no-op en producción** (Q-B): λ_total 4.00→4.05,
   cap al 58%. Corregir la dimensión sin corregir el reparto cambió el
   número interno, no el modelo que apostaba. Es el hallazgo más incómodo
   para mí: lo presenté como "λ_total 4.02→2.80" hace 24 horas.
2. **El docstring de `league_calibration.py:212` enseñaba la fórmula bug**
   como "Usage" oficial — el patrón sobrevivió al fix en la documentación
   (detectado por el grep R6 de esta ronda; corregido).
3. **El 40% de la producción de la era anclada no tiene ningún mercado
   anclado** (Q-C) — la narrativa "todo anclado al 65%" era falsa para AH,
   HT, córners y tarjetas.
4. La señal 3 del ensemble y la señal 1 vivían en escalas de λ
   incommensurables → el "agreement" y los pesos adaptativos comparaban
   distribuciones mal construidas (D5).
5. Unidades de `clv` fijadas por escrito: `1/closing − 1/odds` (Q-E).

## 7. Correcciones aplicadas

Commit de fixes: **074eeac** (+ docstring en el commit de este documento).

| Arreglo | SHA | Archivos | Ocurrencias del patrón (R6) | Test que lo cubre |
|---------|-----|----------|------------------------------|-------------------|
| H3 gate CLV | 074eeac | prediction_pipeline.py | `_load_clv`: 1 sitio, 1 tocado | `test_clv_gate_binds_both_blocked_sets` |
| D4 reparto λ | 074eeac | prediction_pipeline.py (`compute_lambdas`), league_calibration.py (docstring) | `attack * away_defense`: 3 → 1 corregida en código, 1 docstring corregida, 1 legítima (córners, escala ratio) | `test_lambda_average_team_reproduces_league_mean`, `test_lambda_extreme_matchup_stays_sane` |
| D5 señal 3 | 074eeac | ensemble_model.py + call site | `_form_probs`: 1 llamada, 1 tocada | `test_ensemble_form_signal_matches_dc_scale` |
| D6 ancla btts | 074eeac | prediction_pipeline.py (`ANCHOR_MAP`) | `"btts"` clave de ancla: 1 sitio | `test_anchor_map_keys_exist_in_model_markets`, `test_anchor_covers_binary_pairs` |
| D8 caps | 074eeac | prediction_pipeline.py | `min(lambda`: 1 sitio | `test_lambda_extreme_matchup_stays_sane` |
| D9 HT 44/56 | 074eeac | prediction_pipeline.py | `0.55` post-fix: 1 restante (stake, otro dominio) | `test_ht_fractions_sum_to_one` |
| D10 resolver | 074eeac | save_bets.py | `outcome = "loss"`: 2 → default corregido, 1 legítima (AH quarter, con rama) | `test_resolver_covers_every_pipeline_market`, `test_resolver_rejects_unknown_market` |
| D11 baseline | 074eeac | prediction_pipeline.py | copias de 1.35 en pipeline: 0 tras fix | (implícito en tests D4) |
| D12 tope shades | 074eeac | prediction_pipeline.py | — | decisión documentada (§4-D12) |
| H2 MLE en Neon | 074eeac | dc_mle_fitter.py | — | verificación pendiente del primer weekly (tabla `model_state`) |
| D13 tests | 074eeac | tests/test_round4_audit.py | — | 9 tests, suite 206 en verde |

## 8. Lo que sigo sin poder afirmar

- Que el anclaje mejore la calibración más allá de la dilución (Q-A con
  n=17 no decide; y mi R-1 muestra que el diseño de Q-A tampoco podía).
- Que el modelo post-074eeac tenga edge real: **cero apuestas resueltas de
  esta generación** al corte.
- Que el mercado esté calibrado en esta submuestra (R-2).
- Que el MLE funcione en producción hasta que el weekly escriba
  `model_state` y un morning lo lea con `mle_weight > 0` en el decision log.

## 9. Veredicto: ¿papel o dinero real?

**Papel.** Sin ambigüedad esta vez.

Condición concreta para revisitar: **≥300 apuestas resueltas de la
generación post-074eeac** (modelo congelado: ningún cambio de modelo, solo
bugfixes demostrados) que cumplan las tres puertas a la vez:
1. ROI con IC95 que contenga el breakeven o mejor,
2. brecha de calibración de los mercados anclados ≤ 3pt,
3. CLV ≥ 0 sostenido con n≥100 en al menos los dos mercados principales
   (el gate de CLV es el árbitro automático).

Mientras tanto el sistema sigue corriendo en papel con su infraestructura
autónoma intacta. La justificación cuantitativa: ROI −8.2% en 1,135
resueltas (SE 3.1%, z=−2.65, p≈0.004) más tres generaciones de modelo en
seis días — no hay base estadística para arriesgar dinero en una
generación con cero observaciones.
