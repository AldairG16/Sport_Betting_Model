## Objetivo

Convertir el flujo de apuestas en dos fases: la mañana trae un **preview informativo** de candidatos, y las **apuestas oficiales** llegan por Telegram pre-kickoff (agrupadas por ventana horaria), ya revalidadas con cuota final, lineup guard pasado — es decir, cuando ya no pueden cambiar de valor.

## Cambios

### 1. `scripts/notify_telegram.py` — el mensaje de las 6 AM se vuelve preview
- En `notify_best_bets()`: cambiar el header de "⚽ BETTING PICKS — fecha" a **"📋 CANDIDATOS DEL DÍA — fecha"** y añadir nota fija: *"ℹ️ Informativo — las apuestas OFICIALES se confirman por aquí ~1h antes de cada partido (cuota final verificada)"*.
- El resto del formato (ligas, mercados, stakes estimados) se queda igual.
- Revisar `tests/test_notification_flow.py` por si asume el texto viejo del header y actualizarlo.

### 2. `scripts/revalidate_pending_bets.py` — nueva función `send_kickoff_confirmations()`
- **Query**: bets `pending` con `match_date` entre `NOW()` y `NOW() + 90 minutos` que NO tengan la marca `kickoff_notified` en `decision_log` (usando `NOT COALESCE(decision_log, '{}'::jsonb) ? 'kickoff_notified'` — el COALESCE evita excluir filas con decision_log NULL).
- **Formato**: un mensaje agrupado — "🎯 APUESTAS CONFIRMADAS — patean en los próximos ~90 min" con por apuesta: hora local (America/Mexico_City), partido, mercado con etiqueta formal (reutilizar `dashboard/display.py`), **cuota final ya revalidada**, stake y edge.
- **Envío**: `send_message()` de notify_telegram (import local). Solo si devuelve True se marca la bet como notificada — así un fallo de Telegram reintenta en la siguiente corrida horaria.
- **Marca**: `decision_log || jsonb_build_object('kickoff_notified', CAST(:note AS jsonb))` con `{"at": iso, "odds": cuota_final}` — mismo patrón CAST que ya usa el archivo (evita el bug de bind que arreglamos antes).
- Helper puro `_format_confirmations(rows)` separado del query para poder testearlo sin DB.
- Envuelto en try/except — jamás rompe el closing.

### 3. `scripts/orchestrator.py` — `step_pre_kickoff_closing()`
- Al final de la función (después de `update_all` → `update_closing_odds` → `revalidate_pending_bets` → lineup guard), llamar `send_kickoff_confirmations()` en try/except. El orden garantiza que lo confirmado ya pasó por revalidación de cuota y guardia de alineaciones.

### 4. Tests
- Nuevo test para `_format_confirmations` (caso con bets y caso vacío).
- Ejecutar suite completa (186 tests) + compileall + push.

## No cambia
- Lógica de revalidación y lineup guard (la notificación se añade DESPUÉS de ellas).
- Evening summary (ya es compacto; su preview de mañana queda informativo).
- Stakes (se calculan en el morning; la revalidación solo actualiza cuotas).
- El mensaje de "REVALIDACIÓN PRE-KICKOFF" (cancelaciones) se queda — informa cuando una candidata muere antes de confirmarse.

## Riesgos cubiertos
- JSONB `?` operator con filas NULL: resuelto con COALESCE (caveat confirmado en exploración).
- Doble notificación si una corrida se retrasa y encimera: la marca `kickoff_notified` lo impide.
- Ventana de 90 min con corridas horarias: cada partido recibe exactamente una confirmación ~30-90 min antes de su kickoff; si GitHub retrasa una corrida, la siguiente aún confirma a tiempo mientras el partido no haya iniciado.