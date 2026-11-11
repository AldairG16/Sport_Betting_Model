# Auditoría Ronda 10

Corte de datos: **lunes 21-sep-2026, 09:52 CST** (verificado con `date`, como
exige la corrección procedimental de A — y esta vez la fecha del prompt era
correcta). Commit de fixes: **ad35f6d**. Suite: **256 passed**.

## 0. Regresiones: re-verificación de rondas 4-9 (R8)

256 tests (251 + 5 nuevos). Sweep, histéresis del gate, closing arreglado,
bandas por (mercado × banda), contadores de población — todos intactos.

## 1. Tabla resumen

| ID | Posición de A | Mi veredicto | ¿Quién tenía razón? | Evidencia | Acción |
|----|---------------|--------------|---------------------|-----------|--------|
| E1 | 85-93% del slate se descarta antes del modelo (skipped_no_odds / fallback_used); si es normalización, la palanca 7-15× | **Refutado con los dos contadores**: 0 y 0 en la corrida. El 153 no era el slate — era MI etiqueta errada: conté filas de la base hasta el 19-oct; el slate real (ventana 3d) hoy es 4 partidos | Ni A ni yo: el error de población era mío (prohibición 19 aplicada hacia adentro) | corrida + SQL del slate (§2) | Sin arreglo de código; corrección de reporte y trazabilidad |
| E2 | El ruido del optimizador entra en `_signed_deviation`; mide gradiente antes de arreglar | **Confirmado y medido**: gradiente ∞ en el punto de parada = 36.3 (lejos de estacionario); y parte del drift es drift de DATOS (n_matches 22,296 → 21,724) | A, matizado por su propia réplica (las corridas no eran comparables) | instrumentación nueva | `final_fun`/`final_grad_max` persistidos en dc_params (ad35f6d) |
| E3 | Dos cambios simultáneos mañana (blend MLE + tau BTTS) sin bandera de cohorte | **Confirmado en el log del runner**: `DC rho=-0.067 (BTTS con tau-correction activa)` corrió HOY junto al primer blend MLE | A | log 35597034851 | Banderas `mle_converged` + `dc_rho_global` en decision_log (ad35f6d) |

**Bonus de la ronda — Q-AB resuelto con giro**: mi primera query daba
mle_weight=0.0 en 86 bets y disparaba el incidente H2 definido. Era un
artefacto mío: filtré por `match_date` (48h) en vez de `created_at` — esas
bets se crearon a las 03:08 UTC, ANTES del fit (04:26 UTC). La bet creada por
el morning de las 12:03 UTC tiene **mle_weight = 0.25** y el log del runner
muestra `Parámetros DC-MLE cargados (ajuste reciente)`. **H2 cerrado en
producción** — nueve rondas después.

## 2. E1 — Atrición del slate

### 2.1 Los dos contadores

```
COMANDO:    corrida instrumentada (slate del lunes 09:52)
SALIDA:     📊 Matches encontrados: 4
            Sin odds:            0
            Fallback usados:     0
CONCLUSIÓN: [MEDIDO] Cero atrición en el slate real. La hipótesis de la
            atrición del 85-93% se refuta — pero con una confesión mía:
```

**El "153 partidos" nunca fue el slate.** Era el resultado de mi propio
`SELECT COUNT(*) FROM upcoming_matches WHERE match_date >= NOW()` — filas de
la base hasta el **19 de octubre** (29 días), porque el cleanup solo borra
pasado. El slate que el pipeline procesa es `NOW()+1h .. NOW()+3d`:
**4 partidos hoy** (lunes), ~40 el fin de semana. La aritmética de inversión
de A (70/153 → 7-15% de partidos) se construyó sobre mi etiqueta errada, y
la prohibición 19 ("no cites una población sin decir de cuántas unidades
superiores proviene") se me aplicó a mí primero.

El corolario bueno: el shadow y las bandas de B2 **sí se miden sobre el slate
real de candidatas** — no hay un 85-93% oculto sesgando la muestra. La
variabilidad del slate (4 entre semana, decenas el fin de semana) sí explica
el ritmo de 13.1 apuestas/semana sin hipótesis adicionales.

### 2.2-2.3 Q-AA y normalización

Sin `fallback_used` dominante, la cruzada contra `matches` queda sin objeto
esta ronda. Queda como vigilancia barata: los contadores ya se imprimen en
cada corrida; si algún día suben, el diagnóstico de nombres (Q-AA) está
escrito y listo.

### 2.4 Impacto en la agenda

El horizonte de 4-6 meses NO se comprime (no había palanca oculta), pero
tampoco se agrava. La corrección de tasa de la ronda 9 (13.1/sem) queda
vigente y ahora bien explicada: ventana de 3 días + calendario de ligas
cubiertas.

## 3. E2 — Estabilidad del MLE

```
COMANDO:    fit instrumentado + re-fit (35s)
SALIDA:     converged=False · n_matches=21,724 (run 1: 22,296)
            final_fun=5,198.13 · final_grad_max=36.32
            home_adv=0.2227 (series: 0.198 / 0.301 / 0.228 / 0.223)
CONCLUSIÓN: [MEDIDO] Dos causas mezcladas, ya separables:
            (1) DRIFT DE DATOS — n_matches cambió entre corridas, así que
            las cuatro corridas no eran comparables (la réplica de A era
            correcta); (2) NO-ESTACIONARIEDAD REAL — un gradiente infinito
            de 36.3 en el punto de parada significa que el presupuesto se
            agotó lejos de un óptimo; no es la tolerancia ftol.
            Diagnóstico de la causa profunda (regularización vs
            parametrización): NO hecho — la instrucción de A era medir
            primero, y eso quedó instalado para la siguiente ronda.
```

Impacto en bandas shadow: el ruido de P(local) (~2pt estimado por A) queda
ahora **segmentable** porque `converged` y `dc_rho_global` viajan en el
decision_log junto a cada bet y cada fila shadow hereda el slate del día.

## 4. E3 — Separación de cohortes (R13)

Elección: **banderas, no escalonado.** El escalonado (activar tau una
semana después) retrasa la señal de BTTS sin necesidad — con las banderas,
las cohortes son separables DESPUÉS y ambas señales corren desde hoy:

- `decision_log.model.mle_converged` (bool) — separa bets con fit
  convergido / no convergido / sin fit.
- `decision_log.model.dc_rho_global` (float|null) — separa BTTS con tau
  activa de BTTS Poisson cruda.

El umbral de tau (|rho|<0.02 → Poisson cruda) no se tocó: el pricing de hoy
queda documentado por fila. Test: `test_decision_log_carries_cohort_flags`.

## 5. Q-AB / Q-AC / Q-AD

```
Q-AB: mle_weight por created_at (no match_date):
      03:08 UTC (pre-fit): 0.0 ×3   ← correcto, no había params
      12:03 UTC (morning): 0.25 ×1  ← MLE OPERANDO EN PRODUCCIÓN
      Verificación en runner log 35597034851:
      "🔧 Parámetros DC-MLE cargados (ajuste reciente)"
      "🔧 DC rho=-0.067 (BTTS con tau-correction activa)"

Q-AC: shadow_bets por (mercado × banda): 81 filas de un slate; el closing
      aún no existe (kickoffs del lunes por la tarde) — primera medición
      real de CLV por celda el fin de semana.

Q-AD: closing por día (post-fix): 21-sep 3/3 · 22-sep 1/2 · 20-sep 37/58.
      Los días nuevos salen ~100% — el arreglo de D1 funciona en producción
      (el 64% del 20-sep incluye bets cuyo partido ya rotó de upcoming).
```

## 6. Derecho de réplica

### R-1
```
OBJETO:    "Entre 10 y 23 de 153 partidos llegan a puntuarse... tu R-1
           explica los 70 diciendo que la mayoría trae solo 1x2+O/U — eso
           daría 765, no 70. Lo que falta no son mercados: son partidos."
MI POSICIÓN: La aritmética de A es impecable y MI premisa era la errada:
           el 153 no era el slate del pipeline sino filas de la base con
           horizonte de 29 días. El slate real del sweep es la ventana de
           3 días (4 hoy, decenas el fin de semana), y en él la atrición
           medida es CERO.
COMANDO:   corrida + SELECT COUNT(*) FILTER (WHERE match_date BETWEEN
           NOW()+1h AND NOW()+3d) → 4 vs 236 filas futuras
SALIDA:    Sin odds: 0 · Fallback: 0 · slate_3d=4 · filas_futuras=236
POR QUÉ NADIE GANA DEL TODO: A razonó bien sobre un dato mío mal
           etiquetado; yo publiqué ese dato sin aplicarme la prohibición
           19. El crédito del hallazgo es de A — el error de población,
           mío.
ALCANCE:   cae la "palanca 7-15×" (no existe) y cae mi declaración de ronda
           9 de que el shadow representa "el 57% de los pares con cuota del
           slate": ahora está bien dicho — del slate real, sin atrición
           oculta.
```

### R-2
```
OBJETO:    "¿el problema es el presupuesto o la parametrización? ... No lo
           arregles a ciegas: mide primero el gradiente final".
MI POSICIÓN: Medido — y el dato separa las dos hipótesis de A: hay AMBAS.
COMANDO:   fit instrumentado (final_fun / final_grad_max en dc_params)
SALIDA:    final_fun=5,198.13 · final_grad_max=36.32 · n_matches=21,724
POR QUÉ MATIZA A: su candidato "REG casi nula frente a la LL" sigue vivo
           (gradiente 36 = dirección de descenso empinada disponible), pero
           la comparabilidad entre corridas también estaba rota por datos
           (22,296 → 21,724 partidos) — su propia lista de réplicas lo
           anticipó para E2.
ALCANCE:   la estabilización del fit queda como tarea con instrumento de
           medición ya instalado; sin ese instrumento, cualquier ajuste de
           REG/maxiter habría sido a ciegas.
```

## 7. Hallazgos propios

1. **Mi "153 partidos" era filas de la base con 29 días de horizonte** — el
   error de población que la prohibición 19 intenta prevenir, cometido en el
   informe de la ronda 9 y heredado por el hallazgo E1 de A. La corrección
   está en §2.
2. **Mi Q-AB inicial disparó un incidente H2 falso** por filtrar
   `match_date` en vez de `created_at`: las bets "de las últimas 48h" se
   habían creado antes del fit. El incidente real se resolvió al medir bien:
   mle_weight=0.25 en el morning.
3. La tau-correction de BTTS llevaba apagada desde siempre (rho ausente) y
   se activó sola el lunes — nadie la había agendado; pasó desapercibida
   hasta que A leyó la puerta del rho.
4. El fitter ahora publica `final_fun` y `final_grad_max`: la próxima ronda
   puede atacar la no-convergencia con datos, no con intuiciones.

## 8. Correcciones aplicadas

Commit: **ad35f6d**. Suite: 256 passed.

| Arreglo | Archivos | R6 | Test | R8 | R9 | R10 | R11 | R12 | R13 cohortes separables |
|---|---|---|---|---|---|---|---|---|---|
| E3 banderas de cohorte | prediction_pipeline.py | 1 sitio | `test_decision_log_carries_cohort_flags`, `test_converged_defaults_false_when_params_absent` | No | No | No | — | — | **Sí — por fila** |
| E2 diagnóstico fit | dc_mle_fitter.py | 1 sitio | `test_fit_records_optimizer_diagnostics` | No | No | medición antes que arreglo | — | — | — |
| E1 trazabilidad | prediction_pipeline.py (print con slate real) | 1 sitio | `test_slate_window_print_uses_true_slate_count` | No | No | — | slate real declarado | — | — |

## 9. Lo que sigo sin poder afirmar

- La causa profunda de la no-convergencia (REG vs parametrización vs
  escala): con el gradiente medido, es la primera pregunta de la siguiente
  ronda si el CLV de BTTS se mueve de forma rara.
- Cuánto del home_adv entre corridas es datos vs presupuesto: ahora
  distinguible retroactivamente (n_matches + final_fun quedan en el payload).
- El CLV de las bandas: primer kickoff medible esta tarde; corte con
  potencia ~29-sep como agendado.

## 10. Veredicto y agenda, con fechas verificadas

**Papel.** Sin cambios en las tres puertas ni en el horizonte (13.1/sem real,
con la explicación ahora correcta: ventana 3 días + calendario de ligas).

Agenda:

| Fecha (verificada) | Evento | Criterio |
|---|---|---|
| Hoy tarde | primeros kickoffs con shadow + closing guardado | n_con_closing empieza a llenar por celda |
| Lun 28-sep | segundo morning con MLE (segunda corrida del fit en CI) | comparar home_adv/rho entre corridas con n_matches y gradiente — separar drift de datos de ruido |
| ~Mar 29-sep | corte shadow [5-15) con n≥25-50 en O/U y 1x2 | CLV por celda (mercado × banda), cohortes separadas por mle_converged/dc_rho_global |
| ~20-oct | revisión B2 completa | decisión de piso por mercado con cohortes limpias |
```
