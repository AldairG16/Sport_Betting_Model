# AUDITORÍA TÉCNICA — Sport Betting Model
## Estado del sistema al 20-sep-2026 · Análisis de producción con datos reales

---

## 1. CONTEXTO

Sistema de apuestas de fútbol: Dixon-Coles + ensemble, PostgreSQL (Neon),
The Odds API (20,000 créditos/mes), GitHub Actions.
19 ligas, 197 tests, pipeline autónomo (morning/closing horario/evening/weekly).
1,038 apuestas resueltas acumuladas. ROI histórico: −8.0%.

---

## 2. CAUSA RAÍZ DE LAS PÉRDIDAS — CONFIRMADA CON DATOS DE PRODUCCIÓN

### El lambda está inflado 49%

```
λ_total promedio en producción:  4.022
Goles reales promedio:           2.550-2.819 (según liga)
INFLACIÓN:                       +40% a +49%
```

**Por qué:** `attack_rating` y `defense_rating` del filtro de Kalman están en
escala de **goles absolutos** (baseline = 1.35 goles/partido para promedio).
El producto `attack × defense` da **goles²**:

```
Equipo promedio: 1.35 × 1.35 = 1.82 goles²
× HOME_ADV × TEMPO = 1.82 × 1.371 = 2.50 λ_home (para equipos promedio)
Equipo fuerte:    2.0 × 1.6 = 3.20 goles²
× 1.371 = 4.39 λ_home → CAP a 2.5 en el 65.8% de los casos
```

**El error:** el producto debería dividirse por el baseline para volver a
goles: `(attack × defense / 1.35) × ajustes`. Sin esta división, cualquier
equipo con attack > 1.35 produce lambdas absurdos.

**Estado del fix:** ya aplicado (commit baada73). Los λ nuevos son menores,
pero los ratings del Kalman **arrastran la inflación histórica** del xG proxy
que estuvo mal por semanas. El filtro produce lo que observó — y lo que
observó estaba inflado.

---

## 3. SEGUNDA CAUSA — xG proxy sistemáticamente inflado (+35%)

El proxy viejo (SOT × 0.30 + off × 0.08) daba **xG 1.88 al equipo promedio**
cuando el real es ~1.40. Esto inflaba ataque Y defensa de TODOS los equipos
un +35% vía el blend al 40%. Recalibrado a 0.28/0.03 → promedio ~1.41 ✓.

**Por qué importa:** el xG blend mezcla `form_attack` (escala goles) con
`xg_for` (escala xG). Si el xG estaba inflado, el blend produce un attack
inflado que alimenta un lambda inflado. La recalibración corrige el proxy
pero los ratings del Kalman siguen arrastrando historia inflada.

---

## 4. TERCERA CAUSA — MLE nunca corrió en producción

```
mle_weight en 187/187 bets: 0.0
```

El ajuste Dixon-Coles MLE (457 líneas) **nunca influyó en una apuesta real**.
La razón: el estado del weekly no persiste entre corridas de GitHub Actions.
Esto confirma que el sistema ha operado con la **señal base sin corrección
MLE** durante toda su historia.

---

## 5. SEÑAL POSITIVA — El anclaje al mercado funciona

| Métrica | Pre-anclaje (981) | Post-anclaje (82) | Mejora |
|---------|-------------------|-------------------|--------|
| Predicho | 57.4% | 58.5% | — |
| Real | 44.6% | 51.2% | +6.7pt |
| ROI | −8.5% | −1.4% | +7.1pt |
| Brecha | 12.8pt | 7.3pt | −43% |

El anclaje al mercado reduce la sobreconfianza y mejora el ROI en 7 puntos.
La brecha aún es +7.3pt pero la dirección es correcta.

---

## 6. SKILL SCORE POR MERCADO (n ≥ 10)

| Mercado | N | WR | Brier | Base | Skill |
|---------|---|-----|-------|------|-------|
| home_win | 125 | 45.6% | 0.2441 | 0.2481 | **+0.016** |
| over25 | 166 | 53.0% | 0.2545 | 0.2491 | **−0.022** |
| away_win | 53 | 28.3% | 0.2165 | 0.2029 | **−0.067** |
| dnb_away | 13 | 53.8% | 0.2321 | 0.2485 | +0.066 |
| draw | 15 | 40.0% | 0.2253 | 0.2400 | +0.061 |

**Conclusión:** el modelo tiene skill marginal positivo solo en home_win.
En over25 (mayor volumen) es marginalmente negativo. La mayoría de los
mercados no tienen muestra suficiente.

---

## 7. ANÁLISIS DE CAUSA RAÍZ — por qué el modelo no bate al mercado

### Cadena de causalidad
```
xG proxy inflado +35%
    → attack_rating inflado (blend 40% con xG)
        → lambda inflado +49%
            → over25 sobrevalorado
            → home_win sobrevalorado para equipos atacantes
                → calibración sobreconfiada +24%
                    → ROI −8% a −20%
```

### Por qué las correcciones no resuelven todo inmediatamente
El Kalman produce ratings basados en lo que observó. Si observó partidos
donde los lambdas estaban inflados, los ratings reflejan esa inflación.
La corrección del xG proxy evita que la inflación CONTINÚE, pero los
ratings históricos tardan 15-20 partidos en converger a valores realistas.

### La limitación fundamental
El fútbol es inherentemente impredecible a nivel de partido individual.
Los sindicatos profesionales con infraestructura de millones solo logran
53-55% de acierto. La ventaja no viene de predecir perfecto — viene de
encontrar los partidos donde el mercado está mal precioado y apostar
suficiente volumen para que el edge se componga.

---

## 8. LO QUE EL SISTEMA SÍ HACE BIEN

1. **Infraestructura autónoma**: 5 pipelines, cron-job puntual, watchdog
2. **Resolución correcta**: 14/14 verificadas, auto-resolución HT/córners
3. **Medición honesta**: CLV, Brier, calibración rodante, sanity audit
4. **Kill-switches en capas**: mercado, liga, CLV gate, circuit breaker
5. **Anclaje al mercado**: reduce la sobreconfianza (demostrado: −8.5% → −1.4%)
6. **Las 5 señales**: tabla miente, FLB asimétrico, empates, confianza, etc.
7. **197 tests** + smoke test 12/12 en cada push
8. **Dashboard server-side** (cero JS obligatorio)

---

## 9. EL VEREDICTO HONESTO

**¿El sistema es rentable?** NO — aún no. ROI −8% acumulado.

**¿Puede serlo?** POSIBLEMENTE, pero no hay garantía. La evidencia:
- Post-anclaje: ROI −1.4% (mucho mejor que −8.5%)
- CLV ≈ 0 (precios justos, sin desventaja)
- Brecha de calibración mejorando (12.8 → 7.3)

**Lo que haría falta para rentabilidad:**
1. 100-300 apuestas más del modelo anclado (3-4 semanas)
2. Que la brecha de calibración baje de 7.3 a <5 puntos
3. Que el CLV se mantenga ≥ 0
4. Que los over25 (el mercado de mayor volumen) converjan

**Si en 100 apuestas el CLV es negativo y la brecha no baja:**
el modelo necesita un rediseño de señal, no más parches. La
infraestructura está lista para ese veredicto.

---

## 10. MÉTRICAS DE PRODUCCIÓN (datos crudos)

### Lambdas por día
| Día | N | λ_h | λ_a | Total | Capped |
|-----|---|------|------|-------|--------|
| 10-sep | 1 | 2.500 | 2.500 | 5.000 | 1 |
| 12-sep | 32 | 2.293 | 1.772 | 4.065 | 23 |
| 19-sep | 48 | 2.344 | 1.874 | 4.218 | 33 |
| 20-sep | 58 | 2.161 | 1.848 | 4.009 | 31 |

### Goles reales por liga (365d)
| Liga | N | Total | Ratio H/A |
|------|---|-------|-----------|
| EPL | 525 | 2.819 | 1.256 |
| La Liga | 466 | 2.727 | 1.394 |
| Bundesliga | 399 | 3.203 | 1.319 |
| Serie A | 452 | 2.555 | 1.063 |
| Brasileirão | 343 | 2.685 | 1.523 |

### Pre vs Post anclaje
| Era | N | Pred | Real | ROI |
|-----|---|------|------|-----|
| Pre | 981 | 57.4% | 44.6% | −8.5% |
| Post | 82 | 58.5% | 51.2% | −1.4% |
