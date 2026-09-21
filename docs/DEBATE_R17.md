# DEBATE R17 — Posición sobre deficiencias abiertas y hacia dónde subir de nivel

Corte: **lunes 21-sep-2026**. Cohorte con huella desde 21-sep 12:03 UTC.
Dos mediciones nuevas hechas para este documento (no opinión a ciegas):

- **Exp-D1**: dos fits comparativos (sin prior / con prior REG_HA=150), mismo
  warm-start, sin escribir a Neon.
- **EB-D2**: shrinkage empírico de Bayes sobre home_adv con las 20 ligas
  medidas, calibrando el SE con Korea (±0.100 @ n=768 → SE = 1.414/√n).

```
EB-D2: sd medido entre ligas = 0.0657 · SE típico = 0.052
       var_between = 0.0016 (σ_b = 0.040) → el shrinkage es REAL y fuerte
Exp-D1: SIN prior  → home_adv=0.2244 · grad_max=38.7 (home_adv) · fun=5310.48
        CON prior  → home_adv=0.2270 · grad_max=41.7 (home_adv) · fun=5310.53
        ambos converged=False · masa top1% ≈ 27% · siguiente parámetro ≤2.2
```

## D1 — MLE no converge porque home_adv no tiene restricción

**PARCIAL.** De acuerdo con el diagnóstico (home_adv es el único parámetro sin
restricción y el gradiente se concentra ahí), **en desacuerdo con la cura
propuesta tal como está calibrada**, y con una lectura nueva del fenómeno
que el propio experimento de A produjo.

MI ARGUMENTO:
1. El experimento refuta el criterio de confirmación de A, en el sentido
   contrario al esperado: con prior 150, **la masa sigue en home_adv**
   (41.7 vs 2.2 del siguiente; masa top1% 27.3% sin prior vs 27.3% con
   prior). El prior añadió solo ~2.2 de contra-gradiente (2·150·0.0074)
   frente a ~44 de verosimilitud agregada: **REG_HA=150 es ~20× débil**.
   Para dominarlo haría falta REG_HA≈3000, que a esa escala ya no es un
   prior: es fijar el parámetro.
2. Lectura nueva: el gradiente de home_adv es la **suma de 21,724
   contribuciones** (~0.002 por partido). El de `def:bolton` (2.2) viene de
   ~38 partidos (~0.058 por partido). El ranking del gradiente mide cuántos
   partidos toca cada parámetro, no cuán mal está: home_adv SIEMPRE dominará
   en esta parametrización mientras la LL sea una suma sin normalizar.
3. Consecuencia: ni el prior ni el sigmoide arreglan la **escala** del
   objetivo; solo re-etiquetan la dirección empinada. La no-convergencia
   (budget agotado lejos de estacionario) es un problema aparte.

EVIDENCIA: Exp-D1 arriba (comando: fitter con REG_HA 0 vs 150, mismo
warm-start, save anulado; salidas pegadas).

PROPUESTA ALTERNATIVA — cualquiera de las tres, en este orden de preferencia:
- **(c) Búsqueda 1-D externa de home_adv**: es UN parámetro; un golden-section
  sobre [0, 0.5] con los equipos re-ajustados condicionalmente (o incluso con
  los equipos fijos del fit actual y solo home_adv+rho re-optimizado) termina
  SIEMPRE y no depende del presupuesto del gradiente conjunto.
- **(a) Normalizar la LL por n_matches** (objetivo = −LL/n − regs): hace el
  gradiente comparable entre parámetros; con eso el histograma vuelve a tener
  significado y el prior (si aún hace falta) se calibra contra otra escala.
- El sigmoide de A (0.6·sigmoid(x)): correcto para acotar, pero **no resuelve
  el deslizamiento** — el experimento muestra que el empuje de la LL es del
  orden de decenas por unidad; un acotador no lo frena.

COSTE / RIESGO: (c) es ~50-100 evaluaciones del objetivo con equipos fijos
(segundos) y no toca la cohorte si se decide en el 20-oct. (a) cambia la
función objetivo → nueva generación de modelo → bandera + reset. Ninguno hoy.

## D2 — La precisión declarada supera a la medida

**DE ACUERDO, con el número refinado.** El shrinkage es la herramienta
correcta, pero `n₀ = 300` fijo es la versión pobre del mismo argumento: el
empírico-Bayes con los datos propios calibra el peso por liga y encoge MÁS
donde la muestra es chica.

EVIDENCIA (EB-D2, salidas arriba):

```
liga        n     medido   EB(n₀ dinámico)   n₀=300 (A)
MLS        187    1.180    1.250             1.229
Turquía    382    1.290    1.267             1.277
Grecia     551    1.205    1.243             1.224
Suecia     750    1.191    1.234             1.211
Brasil    1095    1.470    1.359             1.425
```

σ_between = 0.040 — **la diferencia real entre ligas es ~0.04, menor que el
error de medición de las ligas chicas (~0.08)**: Por eso EB encoge fuerte.
Dos consecuencias que A no sacó:
1. Turquía no debería publicar 1,279 (su n₀=300) sino **1.267** — y ni eso:
   con σ_b=0.04, la "ventaja local de Turquía" es estadísticamente
   indistinguible de la media mundial.
2. La pregunta de A sobre prior por confederación tiene respuesta en los
   datos: Sudamérica NO es excepción significativa (Argentina 1.330±0.10 vs
   media 1.260 → 0.7σ). Brasil 1.470 sí se separa (2.5σ) — una excepción, no
   un régimen.

PROPUESTA: EB con `w_liga = σ_b²/(σ_b² + SE²(n))`, σ_b y SE recalculados en
cada regeneración; guardar `n` y `SE` junto a cada entrada (como propone A).
Los valores EB de la tabla son los que aplicaría.

COSTE / RIESGO: cambia λ de 5-6 ligas ±0.01-0.03 → **bandera
league_factors_version="r17" y no activar antes del 20-oct**. El riesgo que A
teme (mezclar Brasil con MLS) queda resuelto por el propio peso: Brasil retiene
47% de su valor por su n; MLS solo 13%.

## D3 — La tabla mezcla dos ventanas

**DE ACUERDO — retiro también yo la regeneración de las once** (la mía de la
ronda 16, no solo la de A): con D2-EB, el peso de cada entrada lo decide su n
y su SE, no la ventana. Matiz que mantengo: **"n grande ≠ n relevante" es una
amenaza real con los 3 años** — si el 20-oct regeneramos todo con la ventana
común, añadir **decay exponencial dentro de la ventana** (el patrón que el
propio fitter ya usa: DECAY_PER_DAY) es la versión honesta; lo dejo como
parámetro de la regeneración, no como decisión ahora.

COSTE / RIESGO: cero hoy; en la regeneración del 20-oct, bandera de versión.

## M1 — Guardar el precio de cierre de TODOS los partidos

**DE ACUERDO COMPLETO. Es el mejor ítem del documento y el único con fecha de
hoy.** La muestra auto-seleccionada es exactamente el vicio que hizo
ininterpretable Q-G, y 21,724 partidos contra ~1,200 es ~18× potencia.

EVIDENCIA de viabilidad (verificada en código): `upcoming_matches` ya trae
`consensus_home/draw/away_odds` además del máximo — el snapshot puede guardar
AMBOS sin crédito extra.

PROPUESTA ALTERNATIVA / REFINAMIENTOS:
1. Guardar **máximo Y consenso** por separado: batir al máximo es la prueba de
   **rentabilidad** (es el precio que te pagarían); batir al consenso es la
   prueba de **skill** (precio justo). Son dos preguntas; A las mezcló en su
   duda final.
2. Snapshot en ±90 min **y** un segundo snapshot a kickoff−10 min (cierre
   verdadero; las casas publican la última línea ahí).
3. Las columnas van en `matches` con match por (league, home_norm,
   away_norm, date) — los normalizados ya existen en ambos lados.
4. Semanal: Brier(modelo) vs Brier(mercado) **y** CLV de mercado, por liga y
   por mercado, cohortes por `fit_fingerprint`.

COSTE / RIESGO: cero créditos (el fetch ya paga), migración de columnas, no
toca el modelo — **no rompe cohorte, se hace hoy**. Riesgo real: el consenso de
casas blandas es ruidoso (la duda de A) — por eso la columna de consenso es
informativa y el test principal va contra el máximo.

## M2 — Invertir la jerarquía MLE/forma

**PARCIAL — de acuerdo con A en la duda, y propongo medir antes de tocar.**

MI ARGUMENTO: invertir la mezcla HOY es un cambio de modelo en ventana (R13)
sin evidencia de que la forma reciente añada información que el MLE no tenga.
Pero la pregunta SÍ es medible barata: sobre los partidos históricos, ¿la
"novedad" de la forma reciente (Δ Kalman vs MLE esperado) predice el residual
del resultado contra el MLE? Si la correlación es ~0, invertir la mezcla mueve
error de sitio; si es material, justifica el modelo jerárquico.

PROPUESTA: estudio offline (sin tocar producción) tras el corte del 29-sep;
decisión de jerarquía solo con eso. El modelo jerárquico de verdad (fuerza
latente con evolución) me parece la respuesta final correcta, como a A — es
un proyecto r18-r19, no un ajuste.

COSTE / RIESGO: cero ahora; la inversión de mezcla sin evidencia rompería la
cohorte para mover el error de sitio.

## M3 — Alineaciones como gate

**PARCIAL — el rediseño es correcto (gate binario, LLM sin probabilidades),
pero hoy no: primero M1 mide si la señal existe.**

MI ARGUMENTO: la duda de A es la decisión. Si las líneas ya incorporan el XI a
60 min, el gate solo añade latencia y falsos vetos. M1 lo resuelve: con cierre
snapshot por partido y la hora de publicación del XI (API la da), medimos el
movimiento apertura→cierre condicionado a bajas confirmadas. Si mueve >1-2pp en
ligas blandas, el gate vale; si no, no se enciende.

COSTE / RIESGO: diseño ahora, encendido condicionado a evidencia. El coste por
llamada es trivial; el riesgo real son los falsos vetos sobre el volumen de
apuestas, que ahora sí se pueden cuantificar antes de encender.

## C — Ancla como función del skill medido

**DE ACUERDO con la dirección; PARCIAL en el calendario.** La fórmula explícita
es correcta, pero sin M1 no hay "skill medido por mercado" — hay el CLV de
~1,200 apuestas auto-seleccionadas. Propongo: la fórmula se escribe ahora
(tabla `anchor_weight(mercado)` con entradas condicionadas a CLV shadow con
n≥50 por celda) pero **entra en vigor en la revisión del 20-oct** con los
datos de M1 + shadow. Piso duro: ningún ancla <0.50 sin re-validación completa
(bajar de 0.65 a 0.50 duplica el error expresable — el propio A lo dice).

COSTE / RIESGO: escribir la fórmula no toca cohorte; activarla sí → bandera +
reset de cohorte en el 20-oct.

## D — La pregunta incómoda

**DE ACUERDO con la estructura, matico la conclusión.** Si el 20-oct la
respuesta es "no hay ventaja contra el cierre", M2 y M3 no tienen sentido y M1
es lo único que distingue "no hay edge" de "no hay datos suficientes" — de
acuerdo, es la decisión más consecuente.

Mi matiz: **"no le gano al cierre" tiene tres lecturas, no una.** (1) Contra el
cierre de ligas blandas: si tampoco, no hay edge y se para. (2) Contra el
cierre solo en las grandes: el modelo puede tener edge EN las ligas blandas sin
tenerlo en el EPL — y el universo de la ronda 16 se eligió por blandas a
propósito. La respuesta correcta sería concentrar exposición ahí, no parar.
(3) Contra el consenso pero no contra el máximo: el edge existe contra el
precio ejecutable. El diseño de M1 con dos columnas separa las tres. "No hay
ventaja" solo se declara si falla la lectura (1) Y la (3).

## ORDEN QUE PROPONGO

**Hoy (no rompe cohorte):**
1. **M1**: columnas en `matches` + snapshot ±90min/−10min + reporte semanal
   Brier(modelo) vs Brier(mercado) por liga × mercado (máximo y consenso).
2. Exp-D1 y EB-D2 quedan documentados (este archivo) — insumos del 20-oct.
3. Nada de pricing, nada de MLE, nada de mezcla.

**29-sep:** corte shadow (intacto; ya con BTTS shrink y cohortes limpias).

**20-oct (con el primer corte + 4 semanas de M1), una sola batería "r17":**
D1 según (c) o (a); D2-EB; regeneración de las 11 ligas con ventana común y
decay; C-fórmula con datos M1; decisión M2 con el estudio de correlación;
M3 on/off según movimiento de líneas. Todo con banderas y reset de cohorte.

## LO QUE YO AÑADO QUE NO ESTÁ EN LA LISTA

1. **Correlación entre apuestas del mismo partido.** `MAX_BETS_PER_MATCH=2`
   cuenta apuestas, no riesgo: under25 + btts_no del mismo partido son la misma
   apuesta con dos nombres (correlación ≈ +0.8). El cap de exposición diario
   (15%) no modela correlación — dos "apuestas" pueden ser 1.8× de exposición
   efectiva. Fix barato: extender `_EXCLUSIVE_GROUPS` con pares correlacionados
   (under25+btts_no, over25+btts) o un cap por grupo de correlación. Es la
   deficiencia de gestión de riesgo más real que queda y nadie la había mirado.
2. **El precio guardado como `odds` es el máximo entre casas** — puede no ser
   ejecutable (desaparece al apostar volumen) y el CLV/edge se mide contra ese
   máximo teórico. M1 arregla la serie hacia adelante; hacia atrás, todo el
   CLV histórico lleva ese sesgo optimista — hay que declararlo en
   SALIDA_PAPEL §2 antes de que alguien lea el CLV del corte del 29-sep como
   si fuera ejecutable.
3. **La cobertura K1 del r16 abrió una puerta que conviene usar**: calibrar
   una liga nueva cuesta una consulta. La restricción de universo (19 ligas)
   era de cuando calibrar era caro. Con Q-CA automatizada, añadir 10-15 ligas
   blandas calibradas multiplica el volumen de shadow y de apuestas — que es
   el cuello de botella de TODO el plan de salida de papel (13.1/semana →
   4-6 meses). Es la palanca de velocidad del experimento completo.
4. **El reporte semanal de Telegram es la única superficie de gobierno y no
   dice nada de gobierno**: añadir al resumen semanal las filas de la bitácora
   §6, el `fit_fingerprint` vigente, y el estado de las puertas de
   SALIDA_PAPEL. El documento operativo existe; nadie lo opera si no se
   recuerda cada semana.

## LO QUE SIGO SIN PODER AFIRMAR

- Si el paisaje del MLE está mal escalado en general (el experimento D1 midió
  home_adv; la hipótesis de A sobre REG_TEAMS≈0 sigue viva — se resuelve con
  el mismo instrumento: histograma tras normalizar la LL).
- El σ_between=0.040 del EB está calculado CON el ruido de medición dentro de
  las 20 ligas; con n grandes en las principales es robusto, pero Brasil
  (2.5σ) sugiere que un modelo por confederación ganaría poco y perdería n.
- El valor real del gate de alineaciones: condicionado a M1.

## VEREDICTO RESUMIDO

| Punto | Veredicto | Acción |
|---|---|---|
| D1 | PARCIAL — diagnóstico sí, cura calibrada mal | (c) búsqueda 1-D o (a) normalizar LL; prior 150 descartado por medición |
| D2 | DE ACUERDO, refinado | EB con σ_b=0.040 medido; n₀=300 descartado (encoge menos de lo que debe) |
| D3 | DE ACUERDO | D2 resuelve; decay en la regeneración del 20-oct |
| M1 | DE ACUERDO TOTAL, hoy | máximo + consenso; snapshot ±90min y −10min |
| M2 | PARCIAL — medir antes | estudio de correlación forma vs MLE tras el 29-sep |
| M3 | PARCIAL — diseño sí, encendido no | condicionado a M1 (movimiento de líneas) |
| C | PARCIAL | fórmula ahora, vigor en 20-oct, piso 0.50 |
| D | DE ACUERDO con 3 lecturas | M1 primero; "sin edge" solo si falla vs cierre blando Y consenso |
