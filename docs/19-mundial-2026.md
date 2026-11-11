# 19 · Mundial 2026

El Mundial de 2026 se jugó del 11 de junio al 19 de julio. Sus partidos se analizaron en papel (no entraron a `bets_history`) por el kill-switch `WORLD_CUP_BETTING_ENABLED`. Si vuelve a haber un torneo, aquí está cómo se hizo.

<!-- detalle movido de OPERACION.md -->

## Mundial 2026

`_check_world_cup_activation()` añade `soccer_fifa_world_cup` a `SPORT_KEYS` **solo
entre `WORLD_CUP_START` y `WORLD_CUP_END`** (11-jun a 19-jul-2026). El torneo terminó,
así que hoy la función sale antes y la key no se añade.

La versión original solo tenía fecha de inicio: seguía metiendo la liga en `SPORT_KEYS`
indefinidamente, y `update_upcoming_matches` pide cuotas por liga. Si vuelve a haber un
torneo, replica el patrón con las dos fechas.

`WORLD_CUP_BETTING_ENABLED` sigue en `false` por defecto: los picks de torneo van a
`data/paper_trades.jsonl` con etiqueta `[PAPER]` y **no** entran a `bets_history`.

---

<!-- del README de la raíz (11-nov-26) -->

## Kill-switch — Mundial 2026 (Paper-Trading)

`soccer_fifa_world_cup` se activa automáticamente el **2026-06-11** desde
`scripts/orchestrator.py::_check_world_cup_activation()`. Para evitar que el
modelo apueste dinero real con datos de selecciones nacionales aún no validados,
está protegido por un **kill-switch**.

### Cómo funciona

| `WORLD_CUP_BETTING_ENABLED` | Comportamiento de bets del Mundial |
|---|---|
| `false` (default) | Predicciones se loguean a `data/paper_trades.jsonl` y aparecen en Telegram bajo la sección `📝 [PAPER]`. **NO entran a `bets_history`** → no afectan bankroll, ROI, ni CLV. |
| `true` | Se tratan como cualquier otra liga: insertadas en `bets_history`, descontadas del bankroll y rastreadas para CLV. |

El pre-kickoff analyst (`scripts/pre_kickoff_analyst.py`) **NO analiza** paper
bets porque solo lee de `bets_history`.

### Cómo activar (para apostar dinero real en el Mundial)

**Pre-requisitos antes de activar**:

1. **Walk-forward 2022 con ROI ≥ 0** — backtest contra el Mundial pasado.
2. **Paper-trading 1-2 semanas durante el torneo** — revisar `data/paper_trades.jsonl`
   contra resultados reales:
   ```sql
   -- Cuando el Mundial empiece, comparar paper picks vs bets_history.results
   -- (los partidos del Mundial sí se cargan a `matches` aunque no se apueste)
   ```
3. **Calibración Brier ≤ 0.25** específica del Mundial.
4. **Lineups data-loader** funcionando para selecciones nacionales.

**Activar**:

1. **GitHub Settings** → Secrets and variables → Actions → New repository secret
   - **Name**: `WORLD_CUP_BETTING_ENABLED`
   - **Value**: `true`
2. Re-ejecutar `morning.yml` desde Actions → «Run workflow» (no hay cron de GitHub para ese workflow; ver [04](04-corridas-y-horarios.md)).
3. Verificar en Telegram que las apuestas del Mundial ya **no** llevan tag `[PAPER]`.

**Desactivar de emergencia** (si el modelo empieza a perder feo en el Mundial):

1. Actions → Settings del workflow → editar el secret → cambiar a `false`
2. Re-ejecutar `morning.yml` (`daily.yml` ya no existe). Cualquier bet **ya colocada** sigue resolviéndose
   normal — solo se detienen las nuevas.

### Auditoría de paper-trades

```bash
# Ver cuántas predicciones paper hay del día de hoy
python -c "from scripts.notify_telegram import _read_paper_trades_for_date; \
           from datetime import date; \
           print(len(_read_paper_trades_for_date(date.today())))"

# Estadísticas globales
wc -l data/paper_trades.jsonl

# Después del Mundial: matchear paper trades contra resultados de `matches`
# para validar performance del modelo en selecciones nacionales antes de
# levantar el kill-switch en futuras ediciones.
```

### Walk-forward Mundial 2022

Antes de levantar el kill-switch hay que validar que la calibración funciona
contra un Mundial real. El script `scripts/validate_world_cup.py` corre un
walk-forward sobre los **64 partidos del Mundial 2022** (cargados via
`load_international_data.py`) y reporta Brier, win-rate predicho vs real,
ROI hipotético flat-stake (edge ≥ 5%) y factor de calibración sugerido.

```bash
# Validación completa (todos los mercados)
python scripts/validate_world_cup.py

# Solo un mercado
python scripts/validate_world_cup.py --market over25

# Otro año (cuando se carguen más mundiales históricos)
python scripts/validate_world_cup.py --year 2018
```

Ejemplo de output:

```
🌍 VALIDACIÓN MUNDIAL 2022
============================================================
Parámetros MLE: 534 equipos | fitted_at=2026-04-09
Partidos cargados: 64

Mercado      Brier   Pred%   Real%   Bias  ROI(flat)     WR    N
─────────────────────────────────────────────────────────────────
home_win    0.3065   36.1%   43.8%  -7.7pp     -10.0%  37.5%   24
draw        0.1944   28.4%   23.4%  +5.0pp     -52.9%  14.3%   14
away_win    0.2112   35.5%   32.8%  +2.7pp     +54.5%  48.3%   29
over25      0.3019   36.6%   46.9% -10.3pp     -44.3%  28.6%    7
```

**Criterio go/no-go**:

- ✅ **Activar kill-switch**: Brier ≤ 0.25 en los 4 mercados **y** ROI flat ≥ 0% **y** calibración ±15%.
- ⚠️ **No activar**: si algún mercado sale de rango → seguir en paper-trading hasta que se acumulen más datos previos al torneo o se ajuste la calibración por liga (`by_league` en `calibration_factors.json`).

El script es **read-only** sobre `matches` y no toca `bets_history`.
