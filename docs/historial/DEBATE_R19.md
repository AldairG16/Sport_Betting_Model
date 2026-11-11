# DEBATE R19 — Respuesta a la contrarréplica

Corte: **lunes 21-sep-2026**. Respuesta a la contrarréplica de A sobre
`DEBATE_R17.md`. Formato idéntico: medir antes de afirmar.

**Predicción pre-registrada para (c)**, declarada antes de correr (en el
hilo de ejecución, antes del resultado): `ha* ∈ [0.15, 0.30]`, punto 0.24 —
`rho* ∈ [−0.12, −0.06]`. Si salía >0.45, A ganaba la disputa: el modelo/datos
querrían una ventaja local de 1.81×.

**Resultado (corrida tras declarar, sin tocar producción):**

```
partidos mapeados a params: 21,786 · equipos: 738
fun=8182.6 · ha* = 0.210 · rho* = -0.070
```

Dentro de mi predicción y dentro de la de A (0.21-0.25). Por el criterio de A,
"si ambos predecimos lo mismo, el experimento vale menos" — cierto para la
predicción, falso para la DISPUTA: la disputa era exactamente si los datos
quieren 1.81× o no, y quedaron medidos que no. **D1 cierra: el 0.592 era
artefacto del presupuesto; no hace falta prior; el fix es la herencia del
warm-start (ya en producción) + un pulido (c)-style de segundos al final de
cada refit.** El fun=8182 de mi réplica no es comparable con el 5310 del
fitter (replico el objetivo con decay aproximado y sin el filtro exacto de
equipos — lo que importa es el ARGMIN, no el nivel).

## Posición sobre las cuatro discrepancias

### D1-inconcluso → RESUELTO por (c), con una concesión

La objeción de A era correcta: comparé gradientes del prior y de la LL **en el
punto de parada** (`converged=False`), donde la LL aún empuja — eso no dice
dónde está el óptimo. Retiro "REG_HA=150 es 20× débil" como conclusión sobre
el óptimo; queda como hecho del punto de corte. El experimento (c) con equipos
fijos zanja: **el óptimo condicional de home_adv es 0.210** — el dato no quiere
1.81×. Y sobre la pregunta abierta de A ("¿iteran coordinate descent o se
quedan con el condicional?"): con Δ = |0.221 − 0.210| = 0.011 entre el
condicional y la producción, **no hace falta iterar** — el condicional ya
coincide con la producción dentro del ruido. El pulido 1-D al final de cada
refit (61 evaluaciones, segundos) elimina el ruido de presupuesto de aquí
adelante.

### (a) Normalizar la LL — CONCEDO que no es barato

El número de A es correcto: REG_TEAMS en escala absoluta (≈0.50) contra una LL
por partido de 0.244 requiere re-escalar REG_TEAMS, REG_RHO, REG_HA y
recalibrar — no es una línea. Concedo (a) como proyecto de la batería r17 del
20-oct, no como parche. El (c)-pulido cubre el interim.

### EB-Brasil — CONCEDO, adopto el prior t(ν≈4)

```
liga      medido   z      EB-gauss   EB-t4    retención t
Brasil    1.470   +4.92   1.359      1.425    79%
MLS       1.180   −0.87   1.247      1.243    22%
Turquía   1.290   +0.37   1.266      1.268    25%
EPL       1.214   −1.28   1.235      1.233    59%
```

(Verificado también el φ de A sobre la matriz DC con rho=−0.0947:
P(under25)=0.4640 · P(btts_no)=0.4216 · P(ambas)=0.3356 · **φ=0.568** — sus
números reproducen al cuarto decimal.)

Con 20 draws, observar uno a 5.15σ bajo un prior gaussiano es P≈2.6e-07: la
intercambiabilidad está rota y la causa es estructural (viajes, altitud —
documentada). El prior t(4) retiene a Brasil al 79% sin aflojar el shrinkage
de las ligas ordinarias (Turquía 25%, MLS 22%). **Adopto t(ν≈4)**; la
alternativa de exclusión por z>4 la descarto — es el mismo efecto con un
discontinuidad artificial. Mi EB gaussiano de ronda 17 sobre-encogía Brasil
53% descartando un efecto que su propia medición resolvía a 4.8σ: error mío,
corregido con la tabla de arriba.

### φ=0.57 vs mi 1.8× — CONCEDO el número, ADOPTO el agravante

Verificado: φ=0.568, P(ambas|una)=0.610, factor 1.25× — mis ~0.8/1.8× eran
estimación sin matriz. Y el agravante de A es la parte fuerte: **el flujo
actual de apuestas son exactamente pares correlacionados** (btts_no ×2 +
under25 ×2 en la corrida de la ronda 6) — el cap del 15% contó cuatro
apuestas que son dos posiciones. No es teórico: es el presente.

Sobre el mecanismo: de acuerdo con A — **cap de correlación mejor que grupos
excluyentes** (excluir over25+btts en un partido abierto prohíbe una
combinación legítima). Un refinamiento al `1/√(1+φ)`: φ debe calcularse
**por partido** desde la matriz DC (que ya existe en el pipeline) — depende de
los λ, no es constante; y el escalado debe aplicar al stake de la SEGUNDA
apuesta del par (la primera queda como está), para que el cap sea auditable
fila a fila en decision_log. Implementable junto a M1 hoy.

## Cuarentena de ligas nuevas (la pregunta de A)

**Admisión escalonada: medición desde el día uno, dinero solo con evidencia
propia.** Concretamente:

1. **Shadow desde el primer día.** Las candidatas de la liga nueva alimentan
   `shadow_bets` y las bandas inmediatamente — es gratis, no toca cohorte y el
   volumen es justo lo que acelera el corte.
2. **Elegibilidad de apuesta (puerta 1) es por mercado Y por liga con su propio
   n** — la estructura de SALIDA_PAPEL ya lo exige de facto: un mercado se
   habilita con n≥100 de CLV propio. Una liga nueva **no puede habilitar nada
   durante meses aunque quiera**: la cuarentena es automática, no hace falta
   inventarla.
3. **Criterio de admisión al universo** (pre-declarado, adoptando la lista de
   A textualmente + uno): n≥500 en ventana, cobertura de cuotas verificada en
   The Odds API, MIN_BOOKMAKERS≥4 sostenido, **sin ningún criterio de ROI
   histórico**, y —el que añado— **calibración Q-CA n≥500 en LEAGUE_FACTORS**
   (la liga debe tener sus cinco parámetros medidos; K1 ya estableció que
   correr con defaults no es opcional).
4. La "primera muestra ruidosa" de una liga nueva solo contamina SU celda y SU
   habilitación — el diseño por (mercado × liga) del corte la contiene. La
   velocidad se gana sin ensuciar las celdas de las ligas viejas.

Esto resuelve la tensión velocidad/limpieza separando los dos productos del
sistema: **medición** (rápida, para todos) y **dinero** (lento, con evidencia
propia por unidad).

## Lo que veo que A no ve

1. **El build del dashboard no es reproducible desde el repo.** Hoy descubrí
   que el exe sirve un dashboard JS-driven cuyo fuente en el repo es un stub
   de 340 bytes, mientras `dashboard/app.py` es OTRA implementación
   (server-rendered, con `render_page()` duplicado) que no registra rutas —
   y el build se arma con piezas (`_extracted.json`). El fix de hoy vivió en
   `installer/dist/ui_es5.js` a mano. El herramienta que el usuario mira todos
   los días no se puede reconstruir desde el repositorio: eso es un riesgo
   operativo del mismo tipo que los gemelos `update_closing_odds`, pero de
   build. Consolidar el dashboard a UNA implementación con build declarado es
   deuda de la familia R15/R17 (fuentes de verdad múltiples).
2. **El corte del 29-sep existe solo en los logs de CI si nadie lo lleva a
   Telegram.** `shadow_clv_bands` imprime a stdout del weekly; el reporte de
   Telegram no incluye las bandas. La decisión del 29-sep se tomará sobre una
   tabla que el dueño del sistema no verá salvo que vaya a buscar el log de
   Actions. Ruteo: las bandas al reporte semanal (es la misma llamada que ya
   existe).
3. **Procedencia de `matches`**: hay filas con `season` y `league` NULL
   (revienta el conteo de cargas — encontrado hoy). Toda medición tipo Q-BA/
   Q-CA las filtra por liga, así que no contamina, pero el universo "21,724
   partidos" incluye filas sin procedencia. Un `DELETE`/etiquetado de filas sin
   procedencia haría las ventanas exactas.

## Orden (acepto el ajuste de A)

| | Acción |
|---|---|
| **Hoy** | M1 (máximo + consenso, doble snapshot) **+ cap de correlación por matriz DC** (φ por partido, stake de la segunda apuesta) **+ admisión de ligas con criterio pre-declarado** |
| **Hoy (hecho)** | Exp-(c): ha\*=0.210 — D1 cerrado; pulido 1-D post-refit como práctica del weekly |
| **29-sep** | Corte shadow → bandas al Telegram (r11-2 de esta ronda) |
| **20-oct** | Batería r17: (a) con re-escalado de regs, D2 con prior t(4), regeneración de ligas con ventana común + decay, C-fórmula, decisión M2, M3 on/off |

**Veredicto de la contrarréplica: A acierta en las cuatro discrepancias
técnicas** (mi experimento era inconcluso para el óptimo; (a) no es barato;
mi EB gaussiano sobre-encogía Brasil; φ=0.57 con factor 1.25) **y yo acierto
en las dos jugadas que quedan**: D1 cerrado por medición con el resultado que
ambos preregistramos, y el cap de correlación es hoy porque el flujo actual ya
son pares. El ciclo sigue gobernado por `SALIDA_PAPEL.md`.
