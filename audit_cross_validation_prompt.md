# AUDITORÍA CRUZADA: Validación de hallazgos en un sistema de apuestas deportivas

Eres un ingeniero cuantitativo senior especializado en modelos de apuestas deportivas.
Otro modelo de IA ha auditado un sistema de apuestas de fútbol y ha llegado a conclusiones.
Tu trabajo es VALIDAR o REFUTAR esas conclusiones basándote en la evidencia presentada.
No tienes acceso al código — evalúa la calidad del razonamiento y las conclusiones.

---

## CONTEXTO DEL SISTEMA

Sistema de apuestas de fútbol con:
- Modelo Dixon-Coles + ensemble (forma Kalman + xG proxy + H2H)
- PostgreSQL (Neon) con ~93,000 partidos, ~1,038 apuestas resueltas
- The Odds API para cuotas (plan 20,000 créditos/mes)
- 19 ligas (5 grandes + Europa menor + América + Asia)
- Arquitectura: mercado-anclada al 65% + 35% señal del modelo
- Kelly fraccional al 25%, máximo 2% del bankroll por apuesta
- Pipeline: GitHub Actions (morning/evening/closing hourly/weekly Monday)
- 197 tests unitarios + property-based (hypothesis)

### Arquitectura del pipeline de una apuesta:
```
Cuotas API → upcoming_matches → λ (Kalman forma + xG real/proxy + H2H + congestion)
→ Poisson/Dixon-Coles → probabilidades → anclaje 65% mercado → calibración
→ filtros edge (5-9%) → sizing Kelly 25% × confianza → bets_history
→ resolución (evening/late_results) → CLV tracking → calibración semanal
```

### Constantes clave:
- HOME_ADVANTAGE = 1.214 (EPL, derivado de 58k partidos)
- TEMPO = 1.129 (EPL)
- KALMAN_Q = 0.10, KALMAN_R = 1.00, BASELINE = 1.35 goles
- GLOBAL_CALIBRATION = 0.85
- MIN_EDGE = 5% base, 9% home_win
- xG proxy (viejo): SOT × 0.30 + (shots - SOT) × 0.08
- xG proxy (recalibrado): SOT × 0.28 + (shots - SOT) × 0.03

---

## HIPÓTESIS Y VEREDICTOS DEL PRIMER AUDITOR

### H1 — Error dimensional en lambdas
**VEREDICTO: PARCIAL**

Evidencia: Con attack=1.0, defense=1.0 (equipos promedio), la fórmula
`lambda = attack × defense × HOME_ADVANTAGE × TEMPO` da:
- home = 1.0 × 1.0 × 1.214 × 1.129 = 1.371
- away = 1.0 × 1.0 × 1.129 = 1.129
- total = 2.500

La liga real EPL promedia 2.65-2.85 goles. El modelo SUBESTIMA ~5%.
El operador `??` (nullish) fue eliminado del código por compatibilidad.
El multiplicador TEMPO aplica a AMBOS lambdas — no hay doble conteo porque
attack_rating está en escala de goles/partido, no de ratio.

Pregunta al validador: ¿la subestimación del 5% es material? ¿Debería
corregirse agregando un factor de normalización o es aceptable dado que la
calibración semanal compensa?

### H2 — Estado semanal no persiste
**VEREDICTO: CONFIRMADA**

Los archivos que el weekly genera (calibration_factors.json, clv_cache.json,
dc_params.json, thresholds.json) viven en el filesystem efímero del runner de
GitHub Actions. Cada checkout limpio los pierde. `is_params_fresh()` devuelve
False y el pipeline se degrada EN SILENCIO (sin DC-MLE, con calibración vieja,
sin CLV cache).

Pregunta al validador: ¿es correcto que estos archivos se guarden en PostgreSQL
(la DB ya es la fuente de verdad de todo lo demás)? ¿O hay una razón para
mantenerlos en filesystem?

### H3 — CLV gate por nombre incorrecto
**VEREDICTO: CONFIRMADA Y CORREGIDA**

El pipeline importaba `load_clv_blocked_markets` pero llamaba `load_clv_blocked_leagues`
(un nombre que no existía). El `except` envolvía ambas cargas en el mismo bloque,
lo que significa que si fallaba una, se perdían ambas. Ya corregido: las cargas
están separadas y el except loguea qué falló.

Pregunta al validador: ¿el criterio de auto-bloqueo (n≥20, CLV ≤ -5%) es
razonable, o es demasiado agresivo/conservador?

### H4 — Método Shin devig
**VEREDICTO: FUNCIONA CORRECTAMENTE**

La implementación del método Shin produce resultados correctos (suman 1.0)
y las diferencias con la normalización proporcional son mínimas a niveles
de overround de 3-7% (esperado: Shin corrige más en cuotas largas, pero
con overround <7% la corrección es pequeña).

Pregunta al validador: ¿el método Shin es materialmente diferente de la
proporcional en cuotas de fútbol con overround típico de 5%? ¿Justifica la
complejidad adicional?

### H5 — Handicaps asiáticos de cuarto
**VEREDICTO: NO VERIFICADO EN PRODUCCIÓN**

El formateo de líneas (±0.25, ±0.75) y el parseo del resolver fueron revisados
teóricamente, pero no hay evidencia de apuestas reales con estos mercados que
hayan sido resueltas correctamente.

Pregunta al validador: ¿es crítico verificar esto antes de operar con dinero,
o puede validarse con las primeras apuestas en papel?

### H6 — Anclaje al mercado vs umbrales de edge
**VEREDICTO: CONFIRMADA (LA MÁS IMPORTANTE)**

Con anclaje al 65%, para que una apuesta genere edge ≥5%, el modelo debe
discrepar del mercado por más de ~14 puntos porcentuales. Esto significa que
el filtro selecciona los partidos donde el modelo está MÁS EQUIVOCADO (outliers),
no donde tiene ventaja — a menos que el modelo tenga un edge real que el mercado
no ha capturado.

Datos reales del skill score (calibration_factors.json):
- home_win (n=125): Brier 0.2441, skill +0.016 (marginalmente mejor que azar)
- over25 (n=166): Brier 0.2545, skill -0.022 (marginalmente peor)
- away_win (n=53): Brier 0.2165, skill -0.067 (peor)
- La brecha de calibración es +18-25% sistemáticamente

Preguntas al validador:
1. ¿Es razonable esperar que un modelo con anclaje 65/35 genere edge si el
   skill base es ~0?
2. ¿El anclaje debería ser más agresivo (80/20) para capturar solo las
   desviaciones más significativas?
3. ¿Es correcta la interpretación de que el anclaje selecciona outliers?

### H7 — Skill score del modelo
**VEREDICTO: CONFIRMADA (EL HALLAZGO MÁS REVELADOR)**

Análisis de skill score por mercado (Brier del modelo vs Brier de predecir
siempre la tasa base):

```
Mercado         N    Pred%  Real%  Brier   Base   Skill   Veredicto
home_win       125   50.5%  45.6%  0.2441  0.2481  +0.016  MARGINAL +
over25         166   62.9%  53.0%  0.2545  0.2491  -0.022  MARGINAL −
away_win        53   38.2%  28.3%  0.2165  0.2029  -0.067  PEOR
dc_1x           18   63.4%  61.1%  0.2383  0.2377  -0.003  NEUTRO
dnb_away        13   57.2%  53.8%  0.2321  0.2485  +0.066  MEJOR (débil)
draw            15   29.4%  40.0%  0.2253  0.2400  +0.061  MEJOR (débil)
```

Conclusión del auditor: El modelo tiene skill MARGINAL positivo solo en
home_win (+0.016) con n=125. En over25 (el mercado de mayor volumen), el
skill es NEGATIVO (−0.022). La mayoría de los mercados tienen muestras <30
que no permiten conclusiones.

Preguntas al validador:
1. ¿Estás de acuerdo con que el skill es marginal e insuficiente?
2. ¿Qué mercado priorizarías para mejorar el skill?
3. ¿El Brier de 0.2441 en home_win con n=125 es estadísticamente
   significativo vs el baseline de 0.2481?

### H8 — Independencia del ensemble
**VEREDICTO: CONFIRMADA**

Las tres señales del ensemble (forma Kalman, xG proxy, H2H) provienen de
los mismos attack_rating/defense_rating que se derivan de los mismos
partidos. No son independientes — el "agreement" entre ellas está inflado
artificialmente, lo que aumenta el confidence_boost que alimenta el sizing.

Pregunta al validador: ¿debería eliminarse el confidence_boost del ensemble,
o es aceptable como medida de consistencia aunque las señales no sean
independientes?

---

## HALLAZGOS ADICIONALES (FASE 3)

### A1 — xG proxy sistemáticamente inflado (+35%)
El proxy viejo (SOT × 0.30 + off × 0.08) daba xG ~1.88 al equipo promedio
cuando el real es ~1.40. Esto inflaba TODOS los lambdas y TODAS las
probabilidades. Recalibrado a 0.28/0.03 → promedio ~1.41 ✓

Pregunta: ¿la recalibración es suficiente, o el enfoque del proxy es
fundamentalmente limitado y debería reemplazarse por xG real de FBref/Understat?

### A2 — Duplicados por variantes de nombre
427 partidos duplicados por variantes de nombre de equipo ("Nott'm Forest" vs
"nottm forest") que contaminaban todas las ventanas de forma. Corregido con
normalización + auto-reparación.

Pregunta: ¿qué otros problemas de calidad de datos deberían auditarse?

### A3 — Brecha de calibración creciente
La brecha predicción-vs-realidad creció de +12% a +24% en las últimas semanas.
El modelo predice 57-58% y acierta 33-38%. Esto empeoró DESPUÉS de las
correcciones de bugs, lo que sugiere que los bugs viejos enmascaraban la
debilidad real del modelo.

Pregunta al validador: ¿es esperado que las correcciones REVELEN una
debilidad que estaba enmascarada? ¿O indica que las correcciones introdujeron
un nuevo problema?

---

## VEREDICTO GENERAL DEL PRIMER AUDITOR

El sistema tiene:
- Infraestructura sólida (pipelines, tests, auto-refresco, versionado)
- Medición honesta (CLV, Brier, calibración, sanity audit, holdout)
- CERO JavaScript obligatorio (100% server-side)
- Skill MARGINAL positivo solo en home_win (+0.016, n=125)
- Brecha de calibración +18-25% sistemática
- ROI -8% acumulado en 1038 apuestas
- La arquitectura mercado-anclada es correcta en teoría pero NUEVA (1 día)
- Las señales profesionales (tabla miente, FLB, empates) son NUEVAS (sin validar)

El auditor NO declara el sistema rentable. Declara que:
1. La infraestructura es de calidad profesional
2. El skill del modelo es INSUFICIENTE para superar el vig
3. La arquitectura anclada es la correcta pero necesita validación
4. El período de recolección (100+ apuestas ancladas) es la condición
   necesaria para el veredicto

---

## INSTRUCCIONES PARA TI (VALIDADOR)

1. Lee cada hipótesis y su evidencia
2. Evalúa si el razonamiento es correcto
3. Identifica cualquier conclusión que consideres errónea o prematura
4. Responde: ¿estás de acuerdo con cada veredicto? ¿Qué añadirías?
5. Da tu propio veredicto general: ¿el sistema tiene potencial? ¿Qué
   cambiarías con prioridad?
6. Sé específico: cita los números y los argumentos que respaldan tu posición
