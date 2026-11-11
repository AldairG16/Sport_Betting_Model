# Auditoría Ronda 11

Corte de datos: **lunes 21-sep-2026, 10:35 CST** (verificado con `date`).
Commits: fixes **(ver §8)**, doc al final. Suite: **261 passed**.

## 0. Regresiones: re-verificación de rondas 4-10 (R8)

261 tests (256 + 5 nuevos). Sweep, histéresis del gate, closing con
resueltas, bandas por (mercado × banda), banderas de cohorte — intactos.
Único cambio declarado: `test_tau_gate_threshold_unchanged` (r10) fija ahora
la histéresis de r11 (enciende >0.03, apaga <0.015).

## 1. Tabla resumen

| ID | Posición de A | Mi veredicto | ¿Quién tenía razón? | Evidencia | Acción |
|----|---------------|--------------|---------------------|-----------|--------|
| F1 | `mle_converged` es constante y no segmenta; falta la huella del fit (`fitted_at`) | **Confirmado**: `converged=False` en 100% de los fits → bandera-testigo sin poder discriminante | A | Q-AF (211 filas sin huella) | `fit_fingerprint` (fitted_at+grad) y `mle_final_grad` en decision_log; `mle_converged` se queda como testigo (decisión tomada) |
| F2 | La puerta de tau es binaria sobre un parámetro inestable; histéresis o anclar al prior | **Confirmado el riesgo, medido el alcance**: rho es ESTABLE entre arranques (−0.088/−0.095, pegado al prior −0.10) mientras home_adv se mueve 0.22→0.59 — el flap es hoy teórico. Histéresis instalada como seguro + serie historizada | A en el diseño; el riesgo real está en home_adv, no en rho (§5) | 2 fits en model_state_history | Histéresis (enciende >0.03, apaga <0.015, estado del histórico) + `model_state_history` |
| F3 | `sweep_total / total_matches` de la corrida del domingo — si ~1.75, hay 1x2 que no llega al sweep | **Cerrado**: el total_matches del domingo está en la traza de r9 — **4 partidos** → 70/4 = 17.5 pares con cuota por partido (≥3 ✓) | La premisa de A (~40 partidos) era errada; el dato ya estaba guardado | r9: "40 candidatas barridas en 4 partidos" | Sin acción |
| (E2 diferido) | Diagnóstico de la no-convergencia con el histograma | **Resuelto**: el gradiente está concentrado en UN parámetro — home_adv (703.7 vs 5.98 del siguiente) | A abrió la pregunta; el hallazgo concreto es nuevo | histograma (§5) | Producción restaurada al mejor fit; arreglo del prior documentado, NO aplicado (R13) |

## 2. F1 — Huella del fit en decision_log

`decision_log.model` lleva desde hoy:
- `fit_fingerprint`: `"<fitted_at>|g<final_grad_max>"` — agrupa filas por
  versión de modelo (lo que el corte del 29-sep necesita).
- `mle_final_grad`: continuo, discrimina qué tan lejos de estacionario.
- `mle_converged`: **se queda como testigo** (decisión F1): el día que el
  fit converja, la bandera cambia sola; mientras tanto es constante y así
  se documenta.

Q-AF: 211 filas anteriores a r10 sin banderas — separables hacia atrás solo
por fecha; hacia adelante, todo lleva huella.

## 3. F2 — Estabilidad de rho y la puerta de tau

Medido sobre dos fits con arranques opuestos (zeros vs warm-start en 0.228):

```
fit desde zeros:   home_adv=0.592   rho=-0.088
fit warm-start:    home_adv=0.221   rho=-0.095
```

**rho se queda pegado al prior (−0.10) pase lo que pase con el resto del
ajuste** — la inestabilidad de home_adv NO se transfiere al pricing de tau.
El riesgo de flap es hoy teórico. Aun así: histéresis instalada (enciende
|rho|>0.03, apaga <0.015, en la zona intermedia manda el estado anterior,
leído de `model_state_history`) y la serie historizada — si un refit futuro
mueve rho de verdad, la serie lo mostrará antes de que la puerta flapee.

## 4. F3 — sweep_total / total_matches

El dato estaba guardado: la corrida del domingo produjo
"40 candidatas barridas **en 4 partidos**" (traza de ronda 9). **70/4 =
17.5 pares con cuota por partido** — muy por encima de los 3 del 1x2 solo.
F3 cerrado: no hay mercados 1x2 perdidos; el slate del domingo noche era
pequeño (4 partidos cargados de mercados completos), no de ~40.

## 5. No-convergencia: el histograma resuelve el diagnóstico

Fit desde zeros (sin warm-start local), 1474 dimensiones:

```
grad_max      = 703.72   ← home_adv (¡el propio parámetro!)
siguiente     =   5.98   (def:arsenal)
grad_l2       = 706.22
masa top 1%   = 33.4% del gradiente total
fun final     = 5,500 (vs 5,198 del warm-start en 0.228)
home_adv final= 0.592   (factor exp = 1.81× — implausible)
```

**Diagnóstico: la no-convergencia es un parámetro.** home_adv no tiene
regularización ni prior (el L2 cubre equipos; reg_rho solo rho) — el
optimizador agota el presupuesto mientras home_adv sigue deslizando, y el
punto de corte depende del arranque:

- Arranque zeros → se corta en 0.592 (fun 5500, peor).
- Arranque warm 0.228 → se corta en 0.221 (fun 5198, mejor).

**Y el hallazgo estructural que explica la serie 0.198/0.301/0.228/0.223 de
la ronda 10:** el fitter tiene **warm-start desde el archivo local**
(`DC_PARAMS_FILE`) — las tres corridas del domingo no eran muestras
independientes, eran un paseo aleatorio encadenado desde puntos truncados.
Además el runner de CI, sin archivo, arranca de zeros → **el modelo semanal
de CI y mis fits locales parten de puntos distintos por estructura.** La
"instabilidad" de ronda 10 era esto, no ruido del optimizador en sentido
estrecho.

**Acción tomada**: producción restaurada al mejor fit disponible
(home_adv=0.221, fun=5197.7) vía warm-start desde el .bak.
**Acción NO tomada a propósito**: añadir prior a home_adv o subir
presupuesto — es un cambio de modelo en plena ventana de medición (R13);
con `fit_fingerprint` el efecto de cada variante queda separable después.
Candidato documentado: prior débil sobre home_adv hacia ln(1.30), o
maxfun×4 solo para home_adv (es UNA dimensión: ~1,475 evals extra, barato).

## 6. Q-AE / Q-AF / Q-AG

- **Q-AE**: `model_state` sobrescribía — creada `model_state_history`
  (INSERT idempotente por fitted_at); hoy 2 filas (0.592/−0.088 y
  0.221/−0.095). La serie ya existe.
- **Q-AF**: 211 filas pre-r10 sin banderas; las cohortes nuevas llevan
  `mle_converged`, `dc_rho_global`, `fit_fingerprint`, `mle_final_grad`.
- **Q-AG**: los kickoffs del lunes son por la tarde — sin closing todavía
  (celdas de 2 filas, 0 con closing). Primer CLV real por celda esta
  noche/tomorrow; corte con potencia el 29-sep como agendado.

## 7. Derecho de réplica

### R-1
```
OBJETO:    "la parte que le toca a A es peor de lo que reconoce el
           informe: A lleva desde la primera auditoría leyendo ese SQL.
           Bastaba pedir el COUNT de la ventana."
MI POSICIÓN: Acepto la extensión — y la elevo a regla operativa del propio
           pipeline: el número que se imprime ("Matches encontrados: N")
           es el único que cuenta; cualquier COUNT de la base es población
           distinta. El print de r11 lo dice explícito ("pares con cuota"
           junto a total_matches del slate real).
COMANDO:   SELECT COUNT(*) FILTER (WHERE match_date BETWEEN NOW()+1h AND
           NOW()+3d) → 4 (vs 236 filas futuras)
SALIDA:    slate_3d=4
POR QUÉ IMPORTA: la corrección de A convierte E1 en un caso de manual de
           R19-ante-scripción: el lector del informe (A) también valida las
           poblaciones que el informador da por buenas.
ALCANCE:   ninguno operativo — sin atrición real, sin palanca.
```

### R-2
```
OBJETO:    "R14 puede ser injusta con la ronda 8: el fix era defensivamente
           correcto y el hallazgo (df reasignado) real."
MI POSICIÓN: De acuerdo en el matiz y lo adopto: el bug (`df` reasignado
           entre el print y el sweep) EXISTÍA y el fix era correcto; el
           fallo fue declarar corregido el SÍNTOMA ("4 partidos" vs 153)
           sin verificar el número — que resultó estar bien por el bug
           mismo. R14 se aplica igual (el valor en disputa nunca cambió),
           pero el juicio de la ronda 8 sube de "arreglo accidental" a
           "arreglo correcto con verificación incompleta".
COMANDO:   git show (r8): total_matches = len(df) capturado antes de la
           reasignación de df
SALIDA:    el print hoy siempre refleja el slate del pipeline
ALCANCE:   R14 queda como regla; el histórico de ronda 8 queda matizado en
           este documento.
```

## 8. Hallazgos propios

1. **El warm-start encadena los fits locales**: tres corridas "independientes"
   eran un paseo aleatorio. La serie de home_adv de ronda 10 no medía ruido
   del optimizador; medía la historia de una caminata.
2. **CI y local parten de puntos distintos por estructura** (el runner no
   tiene archivo): sin corrección, el modelo semanal del CI salta respecto a
   cualquier fit local. Ahora `model_state_history` lo hace visible.
3. **home_adv es el único parámetro sin restricción de todo el fit** — ni
   L2 ni prior ni presupuesto propio. El histograma lo señala con un factor
   118× sobre el segundo (703.7 vs 5.98).
4. rho, en cambio, está anclado por su prior y no se mueve: la puerta de tau
   era segura por construcción accidental, y ahora por histéresis
   documentada.

## 9. Correcciones aplicadas

Commits: fix y doc de esta ronda (SHAs en el log; fix = el primero de los dos).

| Arreglo | Archivos | R6 | Test | R8 | R9-R13 | **R14: ¿cambió el valor en disputa?** |
|---|---|---|---|---|---|---|
| F1 huella del fit | prediction_pipeline.py | 1 sitio (decision_log) | `test_decision_log_carries_fit_fingerprint`, `test_fingerprint_uses_fitted_at_and_gradient`, `test_mle_converged_kept_as_witness` | No | Sí (R13 completada) | **Sí: antes las cohortes eran inseparables; después, cada fila lleva fitted_at+grad** |
| F2 histéresis de tau | prediction_pipeline.py (`_previous_fit_rho` + puerta) | 1 sitio | `test_tau_gate_has_hysteresis_with_state` | No | Sí | Sí: el pricing ya no puede cambiar de modelo por ruido en [0.015, 0.03] |
| F2/Q-AE historización | dc_mle_fitter.py (`model_state_history`) | 1 sitio | `test_refits_are_historized` | No | No | Sí: la serie existe (2 filas ya) |
| E2 histograma | dc_mle_fitter.py (grad_top/l2/share) | 1 sitio | — (verificado por corrida: 703.7/5.98/33%) | No | No | Sí: el diagnóstico pasó de "no converge" a "home_adv no estacionario" |

## 10. Lo que sigo sin poder afirmar

- El CLV por celda: los kickoffs del lunes son por la tarde; el primer corte
  real llega esta noche y el de potencia el 29-sep.
- Que el prior de home_adv (cuando se instale) calme la deriva sin sesgar el
  ajuste: la propuesta está documentada, no medida.
- La dirección del sesgo del shadow en los pares sin referencia (28/70):
  sigue sin caracterizar.

## 11. Veredicto y agenda

**Papel.** Las tres puertas y el horizonte de 4-6 meses siguen. Agenda:
hoy tarde primeros closings de shadow por celda; lunes 28-sep comparación
de dos fits del CI vía `model_state_history`; ~29-sep corte [5-15) con
potencia y cohortes separables por huella; ~20-oct revisión B2 completa.

## 12. Propuesta de cierre del ciclo

Once rondas: de "el modelo calcula mal los goles" a "falta una huella para
agrupar cohortes". Propongo **sustituir el ciclo de auditorías por este
documento operativo** (`docs/SALIDA_PAPEL.md`, a crear con este contenido)
y reabrir auditorías solo bajo los disparadores del punto 5.

**1. Qué se mide (por mercado, corte semanal automático):**
brecha de calibración de bets ancladas (`pred − real`), CLV medio con n,
ROI, y CLV shadow por banda de desvío (`shadow_clv_bands`, ya por mercado ×
banda). Todo con `fit_fingerprint` como dimensión de cohortes.

**2. Umbrales de conclusión (n mínimo por métrica):**
brecha: n≥50 por mercado (antes no se concluye — se sigue en papel);
CLV: n≥100 (el gate automático opera desde n≥30 solo para BLOQUEAR, nunca
para habilitar); ROI: n≥200 con IC95; shadow banda: n≥50 por celda.

**3. Decisión por resultado (numérica):**
brecha ≤3pt sostenida + CLV ≥0 + ROI con IC95 conteniendo 0 → piloto real
(1% bankroll, tope 5u/día). Brecha >6pt o CLV <−2pp con n≥100 → gate bloquea
el mercado (automático). CLV shadow positivo en [5-15) con n≥50 → bajar el
piso de ESE mercado a 0.03. CLV shadow negativo en [19-25) → subir el piso
de ESE mercado a 0.08. home_adv entre refits consecutivos variando >0.08 →
los bets de esa semana se marcan cohort="inestable" y no cuentan para la
puerta 1.

**4. Quién y cuándo:** weekly (lunes): fit + gate + shadow_bands + resumen
Telegram. Hourly: closing (bets + shadow) y revalidación. Humano: revisión
de 10 minutos los martes con el resumen; decisión de piso/techo solo con
celda n≥50.

**5. Qué reabre una auditoría:** brecha anclada >8pt en ventana de 50;
cualquier mercado con CLV <−5pp con n≥30 (gate no disparado = bug);
mle_weight=0.0 con model_state poblado (H2-style); reconciliación bankroll
descuadrada >0.5u; o un cambio arquitectónico mayor (nueva fuente de datos,
cambio de ancla) — en cuyo caso: cohorte nueva con bandera y papel otras
200 apuestas.

Con esto, el sistema se gobierna solo en operación y el ciclo auditor se
convierte en lo que debió ser desde el inicio: un respondedor de incidentes
con criterios explícitos de reapertura.
```
