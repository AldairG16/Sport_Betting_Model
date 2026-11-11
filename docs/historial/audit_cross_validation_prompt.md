# VALIDACIÓN CRUZADA — Sistema de Apuestas Deportivas (Sport Betting Model)

Eres un ingeniero cuantitativo senior especializado en modelos de apuestas deportivas
y sistemas de predicción estadística. Otro modelo de IA auditó y modificó este
sistema. Tu trabajo es revisar las modificaciones, validar las conclusiones, e
identificar cualquier error, sesgo o mejora que el primer auditor haya pasado por alto.

---

## 1. ARQUITECTURA DEL SISTEMA

Pipeline: GitHub Actions (morning 06:05 / closing hourly / evening 21:05 / weekly Mon 13:00 UTC-6)
Base de datos: PostgreSQL (Neon) — 93,000 partidos, 1,195 bets resueltas + 90 pendientes
Lenguaje: Python 3.11 (CI) / 3.14 (local)
Fuentes: The Odds API (cuotas), Understat (xG real), football-data.co.uk (resultados)

Flujo de una apuesta:
- Cuotas API → upcoming_matches → lambda (Kalman forma + xG real/proxy + H2H)
- → Dixon-Coles → probabilidades → anclaje 65% mercado → calibración
- → filtros edge (5-9%) → sizing Kelly 25% → bets_history
- → pre-kickoff: revalidación cuota + lineup guard
- → resolución (evening 21:05 + late_results) → CLV tracking → calibración semanal

Constantes clave:
- HOME_ADVANTAGE = 1.214 (EPL, de league_calibration.py, 58k partidos)
- TEMPO = 1.129 (EPL)
- KALMAN_Q = 0.10, KALMAN_R = 1.00, KALMAN_BASELINE = 1.35
- xG proxy: SOT x 0.28 + off x 0.03 (recalibrado desde 0.30/0.08)
- GLOBAL_CALIBRATION = 0.85
- MIN_EDGE = 5% base, 9% home_win
- ANCHOR_WEIGHT = 0.65 mercado / 0.35 modelo
- Kelly fraccional = 0.25, max_bet_pct = 0.02
- lambda cap = min(lambda, 2.5)
- lambda dimensional fix: dividir por _BASELINE = 1.35

---

## 2. MODIFICACIONES REALIZADAS (cronológico)

| Fecha | Cambio | Impacto |
|-------|--------|---------|
| 10-sep | Re-habilitar EPL + Champions | +2 ligas, +volumen |
| 10-sep | xG proxy recalibrado 0.30/0.08 a 0.28/0.03 | Detiene inflación +35% |
| 11-sep | Dashboard v1 (Flask, JS) | Visualización |
| 12-sep | Gate HT (h1/h2 solo ligas con fbdata) | Evita HT sin datos |
| 13-sep | Resolución Levante (lluvia, push) | 3 bets contabilizadas |
| 14-sep | soccerdata integración (Understat + ClubElo) | xG real + goleadores clubes |
| 14-sep | Market-anchored 65/35 | lambda_total 4.02 a 2.80 |
| 14-sep | 5 senales profesionales en pipeline | Tabla miente, FLB, etc. |
| 15-sep | CLV contra odd original | Métrica honesta |
| 16-sep | Duplicados eliminados (427) | Limpieza de matches |
| 17-sep | get_real_xg defensivo (tabla puede no existir) | Evita crash |
| 17-sep | HT gate (solo ligas con fbdata) | Evita h1/h2 sin datos |
| 18-sep | Dashboard 100% server-side (cero JS) | Compatibilidad |
| 19-sep | Shin equation fix (p2/total vs (p/total)2) | Devig correcto |
| 19-sep | Lambda dimensional fix (dividir por 1.35) | lambda_total 4.02 a 2.80 |
| 19-sep | Stale recovery (36 bets resueltas con fuentes web) | Limpieza histórica |
| 20-sep | Dashboard estático HTML puro (sin servidor, sin JS) | Alternativa al exe |

---

## 3. DATOS DE PRODUCCIÓN (medidos, no inferidos)

### Lambdas reales (decision_log, últimos 120 días)
```
n=187 | lambda_home=2.224 | lambda_away=1.798 | lambda_total=4.022
capped (>=2.5) = 123 (65.8%)
```
El 65.8% de las bets tocan el cap de lambda 2.5. El error dimensional fue
parcialmente corregido (dividir por 1.35) pero el lambda sigue alto.

### Goles reales por liga (365 días)
```
EPL: 2.819 | La Liga: 2.727 | Bundesliga: 3.203 | Serie A: 2.555
Champions: 2.965 | Brasileirao: 2.685 | Eredivisie: 3.294
```
Promedio multi-liga: ~2.75 goles totales por partido.

### Pre vs Post anclaje
```
Pre-anclaje (981 bets): pred 57.4%, real 44.6%, ROI -8.5%
Post-anclaje (82 bets): pred 58.5%, real 51.2%, ROI -1.4%
```
El anclaje reduce la brecha de calibración de 12.8pt a 7.3pt.

### CLV por mercado (n>=10, últimos 120 días)
```
home_win (n=29): CLV = -0.011%
over25  (n=69): CLV = -0.201%
```
CLV ~ 0: precios justos, sin ventaja ni desventaja demostrada.

### Skill score (calibration_factors.json)
```
home_win   (n=125): skill +0.016 (marginal +)
over25     (n=166): skill -0.022 (marginal -)
away_win   (n=53):  skill -0.067 (marginal -)
```

### MLE weight (Dixon-Coles)
```
mle_weight = 0.0 en 187/187 bets (100%)
```
El ajuste Dixon-Coles MLE NUNCA influyó en una apuesta real — el estado
del weekly no persiste entre corridas de GitHub Actions.

---

## 4. LAS 8 HIPÓTESIS ORIGINALES — VEREDICTOS

### H1 — Error dimensional en lambdas: CONFIRMADA
El producto attack x defense sin dividir por baseline da goles².
Production data: lambda_total = 4.022 vs 2.75 real = +46% de inflación.
Parcialmente corregido (dividir por 1.35) pero el lambda sigue alto.

### H2 — Estado semanal no persiste: CONFIRMADA
Los archivos que el weekly genera se pierden en cada checkout.
Solo el cron-job.org del dueño dispara workflows que persisten estado local.

### H3 — CLV gate: CORREGIDA
El import tenía el nombre equivocado. Ya corregido y verificado.

### H4 — Método Shin: NUNCA SE EJECUTABA
La ecuación tenía (p/total)2 donde Shin pide p2/total. Sin cambio de
signo, brentq lanza ValueError, y el except devuelve proporcional.
Corregido a CAST(p AS numeric)2 / total. Verificado.

### H5 — Handicaps asiáticos de cuarto: PARCIALMENTE VERIFICADO
Las líneas de cuarto (0.25, 0.75) se formatean y parsean, pero el modelado
de push en líneas enteras no considera la probabilidad de empate en líneas
enteras. El push reduce el stake efectivo pero el modelo no lo descuenta.

### H6 — Anclaje al mercado: CONFIRMADA Y OPERANDO
73 bets ancladas post-deployment. La brecha de calibración bajó de 12.8 a 7.3pt.

### H7 — Skill score: CONFIRMADO
home_win: skill +0.016. over25: skill -0.022. El modelo NO tiene skill
demostrable sobre el mercado en la mayoría de mercados.

### H8 — Ensemble no independiente: CONFIRMADA
Las 3 señales provienen de los mismos attack/defense ratings. No son
independientes — el agreement está inflado artificialmente.

---

## 5. HALLAZGOS ADICIONALES

### A1 — xG proxy inflado +35% (CAUSA RAÍZ)
El proxy viejo daba xG 1.88 al promedio (real: 1.40). Recalibrado.

### A2 — 427 partidos duplicados
Variantes de nombre crearon duplicados. Corregido con fusión + normalización.

### A3 — Brecha de calibración +24% → +7.3%
Mejoró con el anclaje pero sigue positiva.

### A4 — ROI post-anclaje -1.4% vs pre -8.5%
Mejora significativa pero el ROI sigue negativo. Con 82 bets, el error
estándar es ~11%, así que el IC95 incluye el breakeven.

---

## 6. PREGUNTAS PARA EL VALIDADOR

1. ¿El anclaje al 65% mercado es la proporción correcta?

2. El skill score en over25 es -0.022. ¿Debería bloquearse over25?

3. El lambda cap min(lambda, 2.5) se activa en 65.8% de los casos.
   ¿Esto indica que el fix es insuficiente?

4. La brecha de calibración post-anclaje es +7.3pt. ¿Es aceptable?

5. ¿Vale la pena expandir el scorer model a más ligas con FBref?

6. El flujo de dos fases (preview + confirmación) añade complejidad.
   ¿Simplificarías?

7. Dixon-Coles MLE nunca corrió en producción. ¿Priorizar?

---

## 7. LO QUE EL SISTEMA HACE BIEN

- Infraestructura autónoma de 5 pipelines
- Resolución verificada 14/14
- CLV tracking 80%+ cobertura
- Kill-switches en capas
- Decision log completo
- Sanity audit auto-reparador (8 chequeos)
- 197 tests + smoke test 12/12
- Stale recovery: 36 bets históricas resueltas
- Validación cruzada entre auditorías
