"""
scripts/orchestrator.py
=========================
Runner principal automatizado del Sports Betting Model.

Modos de ejecucion:
  python scripts/orchestrator.py --mode morning   → fetch odds + analisis + notificacion
  python scripts/orchestrator.py --mode evening   → closing odds + resultados
  python scripts/orchestrator.py --mode full      → todo el ciclo completo
  python scripts/orchestrator.py --mode results   → solo actualizar resultados
  python scripts/orchestrator.py --mode closing   → closing odds pre-kickoff (30-60 min antes)
  python scripts/orchestrator.py --mode weekly    → recarga historica + calibracion + reporte

Schedule recomendado (Windows Task Scheduler):
  06:00 AM  → --mode morning
  12:00 PM  → --mode closing      (captura odds de cierre antes de los partidos)
  23:00 PM  → --mode evening
  Lunes 7AM → --mode weekly

Diseñado para ser llamado por Windows Task Scheduler sin intervencion manual.
"""

import sys
import os
import argparse
import traceback
import atexit
from pathlib import Path
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.append(str(Path(__file__).parent.parent))

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_RETENTION_DAYS = 30


def _rotate_logs():
    """Elimina archivos de log con más de LOG_RETENTION_DAYS días de antigüedad."""
    cutoff = datetime.now().timestamp() - (LOG_RETENTION_DAYS * 86400)
    deleted = 0
    for f in LOG_DIR.glob("run_*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            pass
    # También limpiar api_credits.log si supera 1MB
    credits_log = LOG_DIR / "api_credits.log"
    try:
        if credits_log.exists() and credits_log.stat().st_size > 1_000_000:
            # Mantener solo las últimas 500 líneas
            lines = credits_log.read_text(encoding="utf-8").splitlines()
            credits_log.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")
    except OSError:
        pass
    if deleted > 0:
        print(f"🗑️  Log rotation: {deleted} archivos eliminados (>{LOG_RETENTION_DAYS} días)")


LOCK_FILE = LOG_DIR / "orchestrator.lock"
LOCK_STALE_MINUTES = 120  # si el lock tiene más de 2h, considerarlo abandonado


def _acquire_lock(mode: str) -> bool:
    """
    Crea un lock file para prevenir ejecuciones concurrentes.
    Retorna True si se adquirió el lock, False si ya hay otro proceso corriendo.
    """
    if LOCK_FILE.exists():
        try:
            age_sec = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
            if age_sec < LOCK_STALE_MINUTES * 60:
                content = LOCK_FILE.read_text(encoding="utf-8").strip()
                print(f"⛔ Otro proceso ya está corriendo: {content}")
                print(f"   Lock file: {LOCK_FILE} (age: {age_sec/60:.0f} min)")
                return False
            else:
                print(f"⚠️  Lock file stale ({age_sec/60:.0f} min > {LOCK_STALE_MINUTES} min), forzando...")
                LOCK_FILE.unlink()
        except OSError:
            pass

    # Crear lock
    LOCK_FILE.write_text(
        f"mode={mode} pid={os.getpid()} started={datetime.now().isoformat()}",
        encoding="utf-8"
    )
    return True


def _release_lock():
    """Elimina el lock file."""
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except OSError:
        pass


def _check_world_cup_activation():
    """
    Auto-activa soccer_fifa_world_cup en SPORT_KEYS si la fecha >= 2026-06-11.
    Modifica la lista en memoria — no cambia settings.py en disco.
    Envía notificación Telegram la primera vez que se activa.
    """
    from datetime import date
    WORLD_CUP_START = date(2026, 6, 11)
    WORLD_CUP_KEY   = "soccer_fifa_world_cup"

    if date.today() < WORLD_CUP_START:
        return   # aún no es momento

    import config.settings as _settings
    if WORLD_CUP_KEY not in _settings.SPORT_KEYS:
        _settings.SPORT_KEYS.append(WORLD_CUP_KEY)
        print(f"🌍 MUNDIAL 2026: '{WORLD_CUP_KEY}' activado automáticamente")
        try:
            from scripts.notify_telegram import send_message
            send_message(
                "🌍 <b>MUNDIAL FIFA 2026 ACTIVADO</b>\n\n"
                "El modelo ahora incluye partidos del Mundial.\n"
                "Liga: <code>soccer_fifa_world_cup</code>\n\n"
                "¡Buena suerte! ⚽🏆"
            )
        except Exception:
            pass


def _ensure_db_indexes():
    """Crea índices y tablas auxiliares en la DB si no existen."""
    try:
        from config.database import engine as _engine
        from sqlalchemy import text as _text
        # Tabla del Pre-Kickoff Analyst — log de dictámenes 45 min antes del kickoff.
        # NO se toca bets_history. Es solo un canal informativo paralelo.
        ddl = [
            # ── Tabla pre_kickoff_analyses ─────────────────────────────────
            """
            CREATE TABLE IF NOT EXISTS pre_kickoff_analyses (
                id            SERIAL PRIMARY KEY,
                match         TEXT        NOT NULL,
                match_date    TIMESTAMP   NOT NULL,
                market        VARCHAR(50) NOT NULL,
                verdict       VARCHAR(20) NOT NULL,
                confidence    INT         NOT NULL,
                reasoning     TEXT,
                lineups       TEXT,
                sources       JSONB,
                analyzed_at   TIMESTAMP   DEFAULT NOW(),
                UNIQUE(match, market, match_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_prekickoff_match_date ON pre_kickoff_analyses (match_date)",
        ]
        indexes = [
            # matches: búsquedas por equipo + fecha
            "CREATE INDEX IF NOT EXISTS idx_matches_home_date ON matches (home_team, date)",
            "CREATE INDEX IF NOT EXISTS idx_matches_away_date ON matches (away_team, date)",
            "CREATE INDEX IF NOT EXISTS idx_matches_league    ON matches (league)",
            # bets_history: queries frecuentes
            "CREATE INDEX IF NOT EXISTS idx_bets_result       ON bets_history (result)",
            "CREATE INDEX IF NOT EXISTS idx_bets_match_date   ON bets_history (match_date)",
            "CREATE INDEX IF NOT EXISTS idx_bets_league       ON bets_history (league)",
            # upcoming_matches: pipeline usa sport_key + match_date
            "CREATE INDEX IF NOT EXISTS idx_upcoming_sport    ON upcoming_matches (sport_key)",
            "CREATE INDEX IF NOT EXISTS idx_upcoming_date     ON upcoming_matches (match_date)",
        ]
        with _engine.begin() as conn:
            for stmt in ddl + indexes:
                try:
                    conn.execute(_text(stmt))
                except Exception:
                    pass  # índice/tabla ya existe o tabla relacionada no existe aún
    except Exception:
        pass  # no bloquear el pipeline por un error de índices


# ============================================================
# LOGGER
# ============================================================
class Logger:
    def __init__(self, mode: str):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = LOG_DIR / f"run_{mode}_{ts}.log"
        self._fh = open(self.log_file, "w", encoding="utf-8")
        self.steps_ok    = 0
        self.steps_total = 0

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self):
        self._fh.close()


def run_step(logger: Logger, name: str, func, *args, **kwargs) -> bool:
    """Ejecuta un paso y captura errores sin detener el pipeline."""
    logger.log(f"\n{'='*50}")
    logger.log(f"PASO: {name}")
    logger.log(f"{'='*50}")
    logger.steps_total += 1
    try:
        func(*args, **kwargs)
        logger.log(f"✅ {name} — OK")
        logger.steps_ok += 1
        return True
    except Exception as e:
        logger.log(f"❌ {name} — ERROR: {e}")
        logger.log(traceback.format_exc())
        return False


# ============================================================
# PASOS DEL PIPELINE
# ============================================================

def step_fetch_odds(force: bool = False):
    from scripts.update_upcoming_matches import update_all
    update_all(force=force)


def step_load_historical():
    from scripts.load_historical_data import load_historical_data
    load_historical_data()


def step_enrich():
    from scripts.enrich_matches import enrich_matches
    enrich_matches()


def step_predict():
    from src.pipeline.prediction_pipeline import run_prediction_pipeline
    run_prediction_pipeline()


def step_notify():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from config.settings import USER_TIMEZONE
    now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    if now_local.hour >= 12:
        # Después del mediodía → mostrar picks de MAÑANA
        from scripts.notify_telegram import send_tomorrow_preview
        send_tomorrow_preview()
    else:
        # Mañana temprano → mostrar picks de HOY
        from scripts.notify_telegram import notify_best_bets
        notify_best_bets()


def step_closing_odds():
    from scripts.update_closing_odds import update_closing_odds
    update_closing_odds()


def step_pre_kickoff_closing():
    """
    Mejora 6: Fetch closing odds justo antes del kickoff.
    Actualiza upcoming_matches con odds frescas (que seran las closing odds reales)
    y luego ejecuta update_closing_odds para asociarlas a bets_history.
    Diseñado para correr 30-60 min antes de los primeros partidos del dia.

    IMPORTANTE: respeta el cache TTL. No usa force=True porque los picks
    del morning (~6h antes) ya quedaron registrados en bets_history con
    sus odds; re-fetchear con force tira ~600 créditos extra por día y
    mueve poco la aguja de CLV vs dejar que el cache TTL expire natural.
    """
    from scripts.update_upcoming_matches import update_all
    update_all(force=False)
    from scripts.update_closing_odds import update_closing_odds
    update_closing_odds()


def step_fetch_results():
    """Descarga resultados recientes desde The Odds API e inserta en matches.

    days_from=3 — MÁXIMO permitido por la API. Valores >3 devuelven 422
    Unprocessable Entity para todas las ligas → 0 resultados → todas las
    bets se quedan 'pending'. (Bug introducido en b657178 con days_from=5.)
    """
    from scripts.fetch_results import fetch_all_results
    fetch_all_results(days_from=3)


def step_fetch_results_backup():
    """Fallback: football-data.co.uk rellena córners/tarjetas/tiros que The
    Odds API /scores no entrega. Sólo cubre ligas europeas mapeadas.
    Nunca falla el pipeline: si la descarga rompe, se registra y continúa."""
    try:
        from scripts.fetch_results_backup_fbdata import fetch_fbdata_backup
        fetch_fbdata_backup(days=10, verbose=True)
    except Exception as e:
        print(f"⚠️  fbdata backup skipped: {e}")


def step_results():
    from src.models.save_bets import update_bet_results
    update_bet_results()


def step_mlb_predict():
    from src.pipeline.mlb_pipeline import run_mlb_pipeline
    run_mlb_pipeline()


def step_load_mlb():
    from scripts.load_mlb_data import load_mlb_data
    load_mlb_data()


def step_load_extra_leagues():
    from scripts.load_extra_leagues import load_extra_leagues
    load_extra_leagues()


# ============================================================
# MODOS
# ============================================================

def _alert_step_failed(step_name: str):
    """Manda un mensaje a Telegram cuando un paso crítico falla.

    Sin esto, si step_predict truena, step_notify corre igual y manda
    "Sin value bets para hoy" — mensaje engañoso que oculta el bug
    durante días. Esto le avisa al usuario en cuanto pasa.
    """
    try:
        from scripts.notify_telegram import send_message
        send_message(
            f"🚨 <b>Pipeline ERROR</b>\n\n"
            f"Step fallido: <b>{step_name}</b>\n\n"
            f"⚠️ Las predicciones / notificaciones de hoy NO son confiables.\n"
            f"Revisa el log del run en GitHub Actions."
        )
    except Exception:
        pass


def run_morning(logger: Logger, force_fetch: bool = False):
    """
    Ciclo matutino (recomendado: 06:00 AM)
    1. Fetch odds (respeta TTL cache)
    2. Enriquecer partidos con stats historicos
    3. Calcular predicciones + value bets (soccer + MLB)
    4. Notificar por Telegram
    """
    logger.log("MODO: MORNING (fetch + predict + notify)")

    run_step(logger, "Fetch odds",        step_fetch_odds, force_fetch)
    run_step(logger, "Enrich data",       step_enrich)
    predict_ok = run_step(logger, "Predictions", step_predict)
    # run_step(logger, "MLB Predictions",   step_mlb_predict)  # desactivado — sin creditos MLB

    # Si predict falló, NO mandamos "Sin value bets" — mandamos alerta.
    # Antes: el usuario veía "Sin value bets para hoy" sin saber que era un bug.
    if predict_ok:
        run_step(logger, "Telegram",      step_notify)
    else:
        logger.log("⚠️  Saltando step_notify — predictions falló. Enviando alerta.")
        _alert_step_failed("Predictions")


def step_notify_evening():
    from scripts.notify_telegram import send_evening_summary
    send_evening_summary()


def step_notify_tomorrow():
    from scripts.notify_telegram import send_tomorrow_preview
    send_tomorrow_preview()


def run_evening(logger: Logger):
    """
    Ciclo nocturno (recomendado: 23:00 PM)
    1. Actualizar closing odds (sin llamada API)
    2. Actualizar resultados de partidos terminados
    3. Calcular CLV de bets resueltas
    4. Correr backtest
    5. Enviar resumen del dia a Telegram
    6. Fetch odds frescos para mañana + predicciones + preview
    """
    logger.log("MODO: EVENING (closing odds + results + CLV + notify + preview mañana)")

    run_step(logger, "Closing odds",      step_closing_odds)
    run_step(logger, "Fetch results",     step_fetch_results)
    run_step(logger, "Fetch results (fbdata backup)", step_fetch_results_backup)
    run_step(logger, "Results",           step_results)
    run_step(logger, "CLV update",        step_clv)
    run_step(logger, "Backtest",          step_backtest)
    run_step(logger, "Telegram evening",  step_notify_evening)

    # ── Preview de mañana ────────────────────────────────────────────────
    # Fetchea odds frescos, genera predicciones para los partidos de mañana
    # y envía el resumen por Telegram. Así el usuario puede preparar sus
    # apuestas antes de dormir aunque la laptop esté apagada por la mañana.
    run_step(logger, "Fetch odds (mañana)",       step_fetch_odds)
    predict_ok = run_step(logger, "Predictions (mañana)", step_predict)
    # run_step(logger, "MLB Predictions (mañana)",  step_mlb_predict)  # desactivado — sin creditos MLB

    # Mismo guard que run_morning: si predict truena, no enviamos un preview
    # vacío que confunda al usuario — enviamos alerta.
    if predict_ok:
        run_step(logger, "Preview mañana",        step_notify_tomorrow)
    else:
        logger.log("⚠️  Saltando preview mañana — predictions falló. Enviando alerta.")
        _alert_step_failed("Predictions (mañana)")


def run_full(logger: Logger, force_fetch: bool = False):
    """Ciclo completo: morning + evening en una sola ejecucion."""
    logger.log("MODO: FULL")

    run_step(logger, "Fetch odds",   step_fetch_odds, force_fetch)
    run_step(logger, "Enrich data",  step_enrich)
    run_step(logger, "Predictions",  step_predict)
    run_step(logger, "Notify",       step_notify)
    run_step(logger, "Closing odds",  step_closing_odds)
    run_step(logger, "Fetch results", step_fetch_results)
    run_step(logger, "Results",       step_results)


def step_backtest():
    from src.models.backtest_engine import run_backtest
    run_backtest()


def step_clv():
    from src.models.clv_tracker import update_clv
    update_clv()


def step_optimize_thresholds():
    from src.models.threshold_optimizer import optimize_thresholds
    optimize_thresholds(verbose=False)


def step_walkforward():
    from src.models.walkforward_backtest import run_walkforward
    run_walkforward(verbose=True)


def step_weekly_report():
    from scripts.notify_telegram import send_weekly_report
    send_weekly_report()


def step_calibration():
    from src.models.calibration_monitor import compute_calibration, check_calibration_alert
    from scripts.notify_telegram import send_message
    factors = compute_calibration(verbose=True)
    alert = check_calibration_alert(factors)
    if alert:
        send_message(alert)


def step_load_international():
    from scripts.load_international_data import load_international_data
    load_international_data(verbose=True)


def step_collect_events():
    from scripts.collect_match_events import collect_match_events
    collect_match_events(verbose=True)


def step_fit_dc_mle():
    from src.models.dc_mle_fitter import fit_dc_parameters
    fit_dc_parameters(verbose=True)


def run_results_only(logger: Logger):
    run_step(logger, "Fetch results", step_fetch_results)
    run_step(logger, "Results",       step_results)
    run_step(logger, "CLV update",    step_clv)
    run_step(logger, "Backtest",      step_backtest)


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Orchestrator del Sports Betting Model")
    parser.add_argument(
        "--mode",
        choices=["morning", "evening", "full", "results", "weekly", "closing"],
        default="morning",
        help="Modo de ejecucion"
    )
    parser.add_argument(
        "--force-fetch",
        action="store_true",
        help="Forzar fetch de odds aunque el cache sea valido"
    )
    args = parser.parse_args()

    _rotate_logs()              # limpiar logs antiguos antes de cada ejecución
    _check_world_cup_activation()  # activar Mundial 2026 si llegó la fecha
    _ensure_db_indexes()        # crear índices si no existen

    # ── Task locking: prevenir ejecuciones concurrentes ──────────────────
    if not _acquire_lock(args.mode):
        print("❌ Abortando: otra instancia del orchestrator está corriendo.")
        sys.exit(1)
    atexit.register(_release_lock)  # liberar lock al terminar (incluso en crash)

    logger = Logger(args.mode)
    start = datetime.now()
    logger.log(f"INICIO: {start.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.log(f"Modo: {args.mode}  |  Force fetch: {args.force_fetch}")

    try:
        if args.mode == "morning":
            run_morning(logger, args.force_fetch)
        elif args.mode == "evening":
            run_evening(logger)
        elif args.mode == "full":
            run_full(logger, args.force_fetch)
        elif args.mode == "results":
            run_results_only(logger)
        elif args.mode == "closing":
            # Mejora 6: Fetch closing odds pre-kickoff
            # Ejecutar 30-60 min antes de los primeros partidos del dia
            # Esto captura las odds de cierre reales para calcular CLV
            run_step(logger, "Pre-kickoff closing odds", step_pre_kickoff_closing)
        elif args.mode == "weekly":
            run_step(logger, "Load historical data",     step_load_historical)
            run_step(logger, "Load international data",  step_load_international)
            run_step(logger, "Collect match events",     step_collect_events)
            run_step(logger, "Load extra leagues",       step_load_extra_leagues)
            # run_step(logger, "Load MLB data",            step_load_mlb)  # desactivado — sin creditos MLB
            run_step(logger, "Fit DC-MLE parameters",    step_fit_dc_mle)
            run_step(logger, "Calibration monitor",    step_calibration)
            run_step(logger, "Optimize thresholds",    step_optimize_thresholds)
            run_step(logger, "Walk-forward backtest",  step_walkforward)
            run_step(logger, "Weekly Telegram report", step_weekly_report)
    except Exception as fatal:
        # ── Crash report a Telegram ──────────────────────────────────────
        logger.log(f"💀 ERROR FATAL: {fatal}")
        logger.log(traceback.format_exc())
        try:
            from scripts.notify_telegram import send_message
            tb_short = traceback.format_exc()[-500:]  # últimos 500 chars del traceback
            send_message(
                f"💀 <b>CRASH — orchestrator.py</b>\n\n"
                f"Modo: <code>{args.mode}</code>\n"
                f"Error: <code>{str(fatal)[:200]}</code>\n\n"
                f"<pre>{tb_short}</pre>\n\n"
                f"Revisa el log: {logger.log_file.name}"
            )
        except Exception:
            pass
        raise  # re-lanzar para que el exit code sea != 0
    finally:
        end = datetime.now()
        elapsed = (end - start).total_seconds()
        logger.log(f"\nFIN: {end.strftime('%Y-%m-%d %H:%M:%S')}  ({elapsed:.1f}s)")
        logger.log(f"Log guardado: {logger.log_file}")

        # ── Health check: resumen de salud del ciclo ──────────────────────
        # Solo para modos que generan apuestas (morning, evening, full).
        # IMPORTANTE: el mensaje muestra DOS números:
        #   • "Picks del día" → cuántos picks confiables se enviaron al chat
        #     (lo que el usuario realmente verá). Viene del contador module-level
        #     `_LAST_PICKS_SHOWN` en notify_telegram, fijado por notify_best_bets
        #     o send_tomorrow_preview al terminar de mandar el mensaje.
        #   • "Pending totales en DB" → total de pending futuras (incluye días
        #     siguientes). Sirve de diagnóstico, no es lo que el usuario apuesta.
        # Antes solo aparecía "Bets generadas: 10" (= pending totales) y el
        # mensaje de picks mostraba 1 → falsa sensación de desincronización.
        if args.mode in ("morning", "evening", "full"):
            try:
                from scripts.notify_telegram import send_health_check, get_last_picks_shown
                from config.database import engine as _engine
                from sqlalchemy import text as _text
                _df = __import__("pandas").read_sql(_text("""
                    SELECT COUNT(*) as n FROM bets_history
                    WHERE result = 'pending'
                      AND match_date > NOW()
                """), _engine)
                pending_total = int(_df.iloc[0]["n"])
                picks_today   = get_last_picks_shown()

                # Etiqueta consistente con el día que muestra step_notify:
                # antes de mediodía local → picks de HOY; después → picks de MAÑANA.
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo as _ZI
                from config.settings import USER_TIMEZONE as _TZ
                _now_local = _dt.now(_ZI(_TZ))
                if args.mode == "evening" or _now_local.hour >= 12:
                    picks_label = "Picks de mañana"
                else:
                    picks_label = "Picks de hoy"

                send_health_check(
                    mode=args.mode,
                    picks_today=picks_today,
                    pending_total=pending_total,
                    elapsed_seconds=elapsed,
                    steps_ok=logger.steps_ok,
                    steps_total=logger.steps_total,
                    picks_label=picks_label,
                )
            except Exception:
                pass  # nunca bloquear el finally por el health check

        logger.close()
        _release_lock()


if __name__ == "__main__":
    main()
