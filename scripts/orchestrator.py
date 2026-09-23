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

# después del sys.path.append: al correr `python scripts/orchestrator.py`
# la raíz del repo (paquete src) no está en el path hasta aquí
from src.utils.log import get_logger

log = get_logger(__name__)

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
                log.warning(f"⚠️  Lock file stale ({age_sec/60:.0f} min > {LOCK_STALE_MINUTES} min), forzando...")
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
    Activa soccer_fifa_world_cup en SPORT_KEYS solo MIENTRAS dura el torneo.
    Modifica la lista en memoria — no cambia settings.py en disco.
    Envía notificación Telegram la primera vez que se activa.
    """
    from datetime import date, timedelta
    WORLD_CUP_START = date(2026, 6, 11)
    WORLD_CUP_END   = date(2026, 7, 19)   # final del Mundial 2026
    WORLD_CUP_KEY   = "soccer_fifa_world_cup"

    hoy = date.today()

    # Activar UN DÍA ANTES para que el pipeline evening del 10-jun
    # pueda fetchear odds y generar paper-picks del día 1 del Mundial,
    # que llegan al usuario en el preview nocturno de "picks de mañana".
    if hoy < WORLD_CUP_START - timedelta(days=1):
        return   # aún no es momento

    # Y DESACTIVAR al terminar. Esta guarda faltaba: la version original solo
    # tenia fecha de inicio, asi que desde el 10-jun-26 cada corrida seguia
    # metiendo la key en SPORT_KEYS. `update_upcoming_matches` itera
    # SPORT_KEYS y pide odds por liga (`log_credits` contabiliza una por
    # sport), de modo que tras la final del 19-jul se siguieron gastando
    # creditos de The Odds API pidiendo cuotas de un torneo terminado.
    # Margen de un dia para que el evening del dia de la final resuelva.
    if hoy > WORLD_CUP_END + timedelta(days=1):
        return   # el torneo termino

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
            # ── Migración aditiva: probability (0-100) y decision (APUESTA/NO APUESTA) ──
            # Se agregaron 09-may-26 para que el analista sea más específico y
            # para construir loop de aprendizaje (calibración real vs estimada).
            # Nullables → backward compatible con rows viejas.
            "ALTER TABLE pre_kickoff_analyses ADD COLUMN IF NOT EXISTS probability INT",
            "ALTER TABLE pre_kickoff_analyses ADD COLUMN IF NOT EXISTS decision VARCHAR(15)",
            # ── Tabla analyst_heartbeat (10-may-26) ─────────────────────────
            # Cada corrida del pre_kickoff_analyst.py escribe una row.
            # Sirve para detectar gaps en el cron (si pasaron >2h sin row,
            # GH Actions probablemente está throttling). Evening summary
            # consulta esta tabla y alerta al usuario si está stale.
            """
            CREATE TABLE IF NOT EXISTS analyst_heartbeat (
                id          SERIAL PRIMARY KEY,
                ran_at      TIMESTAMP DEFAULT NOW(),
                bets_found  INT,
                bets_analyzed INT,
                bets_skipped_rule INT,
                error_msg   TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_heartbeat_ran_at ON analyst_heartbeat (ran_at DESC)",
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
        self.failed_steps: list[str] = []

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
        logger.failed_steps.append(name)
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


def step_goalscorer_picks():
    """Anytime goalscorer (papel, 0 créditos): picks con fair odds para
    los partidos de las próximas 48h usando match_events."""
    try:
        from scripts.goalscorer_picks import generate_goalscorer_picks
        generate_goalscorer_picks(verbose=True)
    except Exception as e:
        # Papel/informativo — no debe romper el morning
        log.error(f"⚠️  Goalscorer picks falló: {e}")


def step_goalscorer_resolve():
    """Resuelve picks de goleadores de partidos ya jugados (0 créditos)."""
    try:
        from scripts.goalscorer_picks import resolve_goalscorer_picks
        resolve_goalscorer_picks(verbose=True)
    except Exception as e:
        log.error(f"⚠️  Goalscorer resolve falló: {e}")


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
    Captura de cierres antes del kickoff (corre cada ~30 min).
    1. Refresco normal por TTL (sin créditos si el caché está vigente).
    2. Recarga dirigida: solo ligas/eventos con kickoff en la próxima hora
       y bets o candidatas shadow (refresh_for_closing, con tope de créditos).
    3. update_closing_odds: cierre + hora de descarga para bets y shadow; el
       cierre se reemplaza si llega uno más cercano al kickoff.
    Luego revalidación y confirmaciones oficiales pre-kickoff.
    """
    from scripts.update_upcoming_matches import update_all, refresh_for_closing
    from scripts.update_closing_odds import update_closing_odds, select_closing_targets

    # Refresco normal por TTL: sin créditos si el caché está vigente.
    update_all(force=False)

    # Recarga DIRIGIDA (22-sep-26): solo las ligas con kickoff en la próxima
    # hora que tengan bets pendientes o candidatas shadow (+ re-enrichment de
    # los eventos con bets en mercados por evento), con tope de créditos por
    # corrida. Antes: refetch de las 19 ligas si alguna BET arrancaba en
    # <75 min, y nada en el resto — el shadow casi nunca tenía una cuota
    # posterior a la de apertura, y su "cierre" era esa misma cuota.
    refresh_error = None
    try:
        targets = select_closing_targets()
        refresh_for_closing(targets)
    except Exception as e:
        # No se corta el closing (revalidación y confirmaciones siguen), pero
        # el paso termina FALLIDO al final para que se vea (run en rojo).
        refresh_error = e
        log.error(f"   ❌ Recarga dirigida de cierre falló: {type(e).__name__}: {e}")

    update_closing_odds()

    # Fotografía de cuotas post-refetch (dataset de movimientos de línea)
    try:
        from scripts.odds_history import capture_snapshot
        capture_snapshot(verbose=False)
    except Exception as e:
        log.warning(f"   ⚠️  odds_history: {e}")

    # Revalidar bets del día contra las odds frescas recién descargadas:
    # si la línea se movió en contra y el edge murió, cancelar la bet ANTES
    # del kickoff en vez de apostar un número que ya no existe.
    try:
        from scripts.revalidate_pending_bets import revalidate_pending_bets
        result = revalidate_pending_bets(verbose=True)
        if result.get("cancelled"):
            from scripts.notify_telegram import send_message
            send_message(
                f"🔁 <b>REVALIDACIÓN PRE-KICKOFF</b>\n\n"
                f"• {result['cancelled']} bets canceladas (la línea absorbió el edge)\n"
                f"• {result.get('kept_better_odds', 0)} actualizadas a odd mejor\n"
                f"• {result.get('kept_edge_survives', 0)} mantenidas (el edge sobrevive)"
            )
    except Exception as e:
        # La revalidación es un filtro de protección — un fallo no debe
        # romper el closing, pero sí debe verse en el log.
        log.error(f"⚠️  Revalidación pre-kickoff falló: {e}")

    # ── CONFIRMACIONES PRE-KICKOFF (flujo de dos fases, 14-sep-26) ──────
    # Las apuestas OFICIALES llegan aquí: ya con cuota revalidada, lineup
    # guard pasado — el valor final que ya no cambia antes del kickoff.
    # El morning de las 6 AM es solo preview informativo.
    try:
        from scripts.revalidate_pending_bets import send_kickoff_confirmations
        send_kickoff_confirmations(verbose=True)
    except Exception as e:
        log.error(f"⚠️  Confirmaciones pre-kickoff falló: {e}")

    if refresh_error is not None:
        raise RuntimeError(f"recarga dirigida de cierre: {refresh_error}")


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
        log.warning(f"⚠️  fbdata backup skipped: {e}")


def step_results():
    from src.models.save_bets import update_bet_results
    update_bet_results()


def step_shadow_results():
    """Liquida las candidatas shadow ya jugadas con la MISMA función que las
    apuestas (stake 1, sin bankroll). Solo DB. Es el dato que juzga las
    reglas manuales contra resultados reales (ver step_rule_evidence)."""
    from src.models.save_bets import resolve_shadow_outcomes
    resolve_shadow_outcomes()


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
    # Fotografía de cuotas para el dataset de movimientos de línea
    try:
        from scripts.odds_history import capture_snapshot
        capture_snapshot(verbose=True)
    except Exception as e:
        log.error(f"⚠️  odds_history falló: {e}")

    predict_ok = run_step(logger, "Predictions", step_predict)
    # run_step(logger, "MLB Predictions",   step_mlb_predict)  # desactivado — sin creditos MLB

    # Si predict falló, NO mandamos "Sin value bets" — mandamos alerta.
    # Antes: el usuario veía "Sin value bets para hoy" sin saber que era un bug.
    if predict_ok:
        run_step(logger, "Goalscorer picks", step_goalscorer_picks)
        run_step(logger, "Telegram",      step_notify)
    else:
        logger.log("⚠️  Saltando step_notify — predictions falló. Enviando alerta.")
        _alert_step_failed("Predictions")


def step_resolve_pending():
    """Resolver bets pending con Claude API (corners/cards/HT/ligas sin
    cobertura) ANTES de mandar el resumen evening.

    Sin esto, el resumen a las 21:00 MX mostraba corners/cards pendientes
    porque football-data.co.uk lagueaba 24-36h y el cron de resolve_pending
    no corría hasta las 22:00 MX (1h después del resumen).

    Budget-safe: si ANTHROPIC_DAILY_BUDGET_USD ya está agotado, `can_call`
    en resolve_pending_bets levanta RuntimeError, el loop captura y
    continúa silenciosamente. hours_lag=3 y limit=10 acotan el costo a
    ~$0.12 max por corrida.
    """
    try:
        from scripts.resolve_pending_bets import main as resolve_main
        # silent_telegram=True: el resumen evening que va inmediatamente
        # después ya cubre estado de las bets — evitamos doble notificación.
        resolve_main(hours_lag=3, limit_matches=10, silent_telegram=True)
    except Exception as e:
        # Nunca tiramos el evening pipeline por esto — es housekeeping.
        log.error(f"⚠️  resolve_pending (chained) error non-fatal: {e}")


def step_notify_evening():
    from scripts.notify_telegram import send_evening_summary
    # Fecha objetivo DATA-DRIVEN: el día local más reciente con apuestas ya
    # resueltas, acotado a [ayer, hoy]. Inmune a los retrasos de GitHub (el
    # cron de 21:07 MX llegó a ejecutarse a las 02:13 MX del día siguiente,
    # y el anclaje por reloj resumía el día NUEVO — todo pendiente, inútil).
    from datetime import datetime, timedelta, timezone as _tz
    from zoneinfo import ZoneInfo as _ZI
    from config.settings import USER_TIMEZONE as _TZ
    import pandas as pd
    from sqlalchemy import text as _t
    from config.database import engine as _eng
    today_local = datetime.now(_ZI(_TZ)).date()
    target_day = today_local - timedelta(days=1)   # default: ayer
    try:
        df = pd.read_sql(_t("""
            SELECT (match_date AT TIME ZONE 'UTC'
                    AT TIME ZONE :tz)::date AS d
            FROM bets_history
            WHERE result IN ('win','loss','push','half_win','half_loss')
            ORDER BY match_date DESC LIMIT 1
        """), _eng, params={"tz": _TZ})
        if not df.empty:
            d = df.iloc[0]["d"]
            if d == today_local:
                target_day = today_local          # ya hay resueltas hoy
            elif d == today_local - timedelta(days=1):
                target_day = d                    # hoy aún no resuelve nada
            # d más viejo → default ayer
    except Exception as e:
        log.warning(f"   ⚠️  No se pudo anclar por datos ({e}) — usando ayer")
    print(f"   Resumen nocturno anclado al día local: {target_day}")
    send_evening_summary(target_date=target_day)


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
    run_step(logger, "Shadow results",    step_shadow_results)
    # Resolver bets pending (corners/cards/HT) ANTES del resumen, así el
    # usuario no ve "esperando data" en la noche para partidos ya jugados.
    run_step(logger, "Resolve pending (Claude)", step_resolve_pending)
    run_step(logger, "Goalscorer resolve", step_goalscorer_resolve)
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

    # El preview separado de mañana se ELIMINÓ (12-sep): duplicaba las
    # mismas apuestas que el morning enviaría a las 6 AM y era de los
    # mensajes más voluminosos del día. Ahora el resumen nocturno incluye
    # un avance compacto de mañana, y los detalles llegan con las odds
    # frescas del morning.
    if not predict_ok:
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


def step_soccerdata_refresh():
    """xG real + goleadores de clubes + Club Elo vía soccerdata (scraping
    semanal, corre en CI con Python 3.11). Guarda en Neon; el pipeline
    solo lee."""
    try:
        from src.features.soccerdata_feed import refresh_understat, refresh_club_elo
        refresh_understat(verbose=True)
        refresh_club_elo(verbose=True)
    except Exception as e:
        log.error(f"⚠️  Soccerdata refresh falló: {e}")


def step_fit_dc_mle():
    from src.models.dc_mle_fitter import fit_dc_parameters
    fit_dc_parameters(verbose=True)


def step_drift_detection():
    """MEJORA #15 — drift detection automático cada semana.

    Compara últimos 30d vs ventana referencia (60-120d). Si detecta drift
    (KS p<0.05 o ΔBrier>0.05 o ΔROI>10pp), manda alerta a Telegram.
    """
    try:
        from src.models.drift_detector import detect_drift, format_drift_report
        from scripts.notify_telegram import send_message
        report = detect_drift(by_league=True)
        msg = format_drift_report(report, max_lines=15)
        # Solo enviar si hay alerta — no inundar el canal con "sin drift"
        if report.get("alerts"):
            send_message(msg)
            print(f"🚨 Drift detectado — {len(report['alerts'])} alertas → Telegram")
        else:
            print("✅ Sin drift detectado")
    except Exception as e:
        # Drift es informativo — un error no debe bloquear el weekly
        log.error(f"⚠️  Drift detection falló: {e}")


def step_clv_gate():
    """CLV como métrica de decisión: bloquea mercados con CLV significativamente
    negativo (IC95, n>=30, 2 semanas seguidas) y ligas con CLV <= -5%. Guarda
    el estado en la DB (model_state), que el prediction_pipeline lee en cada
    corrida como kill-switch dinámico.
    """
    try:
        from scripts.clv_gate import run_clv_gate
        result = run_clv_gate(verbose=True)
        # B2 (ronda 7): CLV por banda de desvío de las candidatas shadow
        from scripts.clv_gate import shadow_clv_bands
        shadow_clv_bands(verbose=True)
        if result.get("blocked"):
            from scripts.notify_telegram import send_message
            send_message(
                f"🚦 <b>CLV GATE</b>\n\nMercados bloqueados por CLV negativo:\n"
                + "\n".join(f"• {m}" for m in result["blocked"])
                + "\n\nSe desbloquean solos si el CLV recupera."
            )
    except Exception as e:
        # Informativo — no debe bloquear el weekly
        log.error(f"⚠️  CLV gate falló: {e}")


def step_anchor_learning():
    """Aprende el peso del modelo frente al mercado por familia de mercado,
    con el CLV de las candidatas shadow (src/models/anchor_learner.py).
    El pipeline lo lee de la DB en la siguiente corrida."""
    from src.models.anchor_learner import run_anchor_learning, format_report, PRIOR_MODEL_WEIGHT
    state = run_anchor_learning(verbose=True)
    # Telegram solo si algún peso se movió del prior: silencio = sin novedad
    moved = [f for f, n in state.get("families", {}).items()
             if abs(n.get("weight", PRIOR_MODEL_WEIGHT) - PRIOR_MODEL_WEIGHT) > 1e-9]
    if moved:
        from scripts.notify_telegram import send_message
        send_message(format_report(state, html=True))


def step_rule_evidence():
    """Reglas manuales contra RESULTADOS reales: liquida las shadow
    pendientes, arma la evidencia por regla (rule_evidence) y aprende la
    escala de los ajustes manuales (shade_learner), que el pipeline lee de
    la DB en la siguiente corrida. Telegram solo si algún veredicto ya
    tiene datos suficientes o si la escala se movió: silencio = sin novedad."""
    from src.models.save_bets import resolve_shadow_outcomes
    from src.models.rule_evidence import (read_resolved_shadow, run_rule_evidence,
                                          conclusive, format_report as evidence_report)
    from src.models.shade_learner import (run_shade_learning, PRIOR_SCALE,
                                          format_report as scale_report)
    resolve_shadow_outcomes()
    df = read_resolved_shadow()
    evidence = run_rule_evidence(df, verbose=True)
    scales = run_shade_learning(df, verbose=True)
    moved = any(abs(n.get("scale", PRIOR_SCALE) - PRIOR_SCALE) > 1e-9
                for n in scales.get("families", {}).values())
    if conclusive(evidence) or moved:
        from scripts.notify_telegram import send_message
        send_message(evidence_report(evidence, html=True) + "\n\n"
                     + scale_report(scales, html=True))


def step_shadow_reactivation():
    """Mercados de bloqueo fijo (away_win, AH con el local favorito) que
    vuelven solo con evidencia shadow contra el cierre."""
    from scripts.clv_gate import run_shadow_reactivation
    result = run_shadow_reactivation(verbose=True)
    if result.get("changed"):
        from scripts.notify_telegram import send_message
        send_message(
            "🔁 <b>REACTIVACIÓN POR SHADOW</b>\n\n"
            + "\n".join(f"• {g}: {'REACTIVADO' if on else 'bloqueado de nuevo'}"
                        for g, on in result["changed"].items())
            + "\n\nCriterio: CLV de sus candidatas shadow ≥ 0 con n ≥ 30."
        )


def step_evaluate_holdout():
    """Evalúa el modelo sobre el holdout congelado (últimos 45d, que la
    calibración ya NO usa para ajustar). Mide si generaliza o hay overfit.
    Incluye el reporte de slippage (odds vistas vs colocadas).
    """
    try:
        from scripts.evaluate_holdout import evaluate_holdout
        evaluate_holdout()
    except Exception as e:
        log.error(f"⚠️  Holdout evaluation falló: {e}")
    try:
        from src.models.save_bets import slippage_report
        rep = slippage_report()
        if rep.get("status") == "ok":
            print(f"   Slippage: n={rep['n']}  medio={rep['avg_slippage_prob']:+.4f} "
                  f"(prob) / {rep['avg_slippage_odds_pct']:+.2f}% (odds)")
        else:
            print("   Slippage: sin bets con odds_placed registrado aún")
    except Exception as e:
        log.error(f"⚠️  Slippage report falló: {e}")


def step_sanity_audit():
    """Auditor de sanidad: duplicados, xG, calibración rodante, bets dobles,
    resolución estancada, integridad. Alerta Telegram solo si falla algo."""
    try:
        from scripts.weekly_sanity_audit import run_sanity_audit
        result = run_sanity_audit(verbose=True)
        if result.get("alerts"):
            logger = None  # el mensaje ya lo manda el auditor
    except Exception as e:
        log.error(f"⚠️  Sanity audit falló: {e}")


def step_market_regime():
    """Monitor de régimen del mercado: vig promedio, bookmakers por partido
    y volatilidad de líneas. Si el mercado cambia (API, panel de books),
    los edges se distorsionan silenciosamente — esto lo detecta.
    """
    try:
        from scripts.market_regime_monitor import run_market_regime_monitor
        result = run_market_regime_monitor(verbose=True)
        if result.get("alerts"):
            from scripts.notify_telegram import send_message
            send_message(
                "🧭 <b>CAMBIO DE RÉGIMEN DEL MERCADO</b>\n\n"
                + "\n".join(f"• {a}" for a in result["alerts"])
            )
    except Exception as e:
        # Informativo — no debe bloquear el weekly
        log.error(f"⚠️  Market regime monitor falló: {e}")


def step_refresh_clv_cache():
    """MEJORA #14 — refresca el caché de CLV (DB model_state/clv_cache, con
    espejo en data/clv_cache.json) para que kelly_stake use el CLV trailing
    de la cohorte al modular kelly_fraction. Llamado en weekly.
    """
    try:
        from src.models.betting_engine import refresh_clv_cache
        result = refresh_clv_cache()
        n_markets = len(result.get("by_market", {}))
        if "error" in result:
            log.error(f"⚠️  CLV refresh falló: {result['error']}")
        else:
            print(f"✅ CLV cache refrescado: {n_markets} mercados")
    except Exception as e:
        log.error(f"⚠️  CLV refresh falló: {e}")


def run_results_only(logger: Logger):
    # Incluye fbdata backup: el late_results.yml a las 06:30 MX existe
    # justamente para atrapar corners/cards de Europa que ya sincronizó
    # fbdata. Sin esta línea ese cron descargaba goles via The Odds API
    # pero nunca tocaba fbdata → corners/cards se quedaban unresolved.
    run_step(logger, "Fetch results",                  step_fetch_results)
    run_step(logger, "Fetch results (fbdata backup)",  step_fetch_results_backup)
    run_step(logger, "Results",                        step_results)
    run_step(logger, "Shadow results",                 step_shadow_results)
    run_step(logger, "CLV update",                     step_clv)
    run_step(logger, "Backtest",                       step_backtest)


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
        log.error("❌ Abortando: otra instancia del orchestrator está corriendo.")
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
            # 1) PRIMERO lo que aprende de datos ya recolectados (bets y
            #    shadow resueltos por evening/closing): solo DB, segundos en
            #    total, y no depende de las cargas de abajo. Ir antes las
            #    protege del timeout del job — las cargas tardan ~50 min
            #    (eventos 21', ligas extra 25') y un corte ahí dejaba al
            #    sistema una semana sin aprender.
            run_step(logger, "Calibration monitor",      step_calibration)
            run_step(logger, "CLV gate (kill-switch)",   step_clv_gate)
            run_step(logger, "Reactivación por shadow",  step_shadow_reactivation)
            run_step(logger, "Peso del modelo (ancla)",  step_anchor_learning)
            run_step(logger, "Evidencia de reglas",      step_rule_evidence)
            run_step(logger, "Refresh CLV cache",        step_refresh_clv_cache)   # Mejora #14
            run_step(logger, "Holdout evaluation",       step_evaluate_holdout)
            # 2) Cargas de datos (lentas) y el ajuste que depende de ellas
            run_step(logger, "Load historical data",     step_load_historical)
            run_step(logger, "Load international data",  step_load_international)
            run_step(logger, "Collect match events",     step_collect_events)
            run_step(logger, "Load extra leagues",       step_load_extra_leagues)
            # run_step(logger, "Load MLB data",            step_load_mlb)  # desactivado — sin creditos MLB
            run_step(logger, "Soccerdata refresh (xG real)", step_soccerdata_refresh)
            run_step(logger, "Fit DC-MLE parameters",    step_fit_dc_mle)
            # 3) Monitores y reportes
            run_step(logger, "Drift detection",          step_drift_detection)     # Mejora #15
            run_step(logger, "Market regime monitor",    step_market_regime)
            # La auditoría de sanidad NO corre aquí: la ejecuta su propio paso
            # en weekly.yml (if: always(), corre aunque el weekly falle).
            # Correrla en los dos sitios mandaba el mismo mensaje dos veces.
            run_step(logger, "Optimize thresholds",      step_optimize_thresholds)
            run_step(logger, "Walk-forward backtest",    step_walkforward)
            run_step(logger, "Weekly Telegram report",   step_weekly_report)
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

                # Health check SOLO si algo falló (12-sep): el "Pipeline OK"
                # diario era ruido — el usuario ya recibe los picks (morning)
                # y el resumen (evening) como confirmación de que corrió.
                if logger.steps_ok < logger.steps_total:
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
        elif logger.failed_steps:
            # Los modos sin health check (weekly, closing, results) fallaban
            # en silencio: el 21-sep "Load historical data" del weekly murió
            # y nadie se enteró. Aviso corto con los pasos caídos.
            try:
                from scripts.notify_telegram import send_message
                send_message(
                    f"🚨 <b>Pipeline {args.mode}: {len(logger.failed_steps)} "
                    f"paso(s) fallaron</b>\n\n"
                    + "\n".join(f"• {s}" for s in logger.failed_steps)
                    + "\n\nRevisa el log del run en GitHub Actions."
                )
            except Exception:
                pass

        logger.close()
        _release_lock()

    _report_tolerated_errors(args.mode)

    # Un paso fallido deja el run en ROJO en Actions. Antes salía verde
    # aunque fallara ("un fallo verde se ve igual que un día normal",
    # docs/OPERACION.md §2). Se evalúa al final: todos los pasos corren y
    # las notificaciones salen igual; solo cambia el código de salida.
    if logger.failed_steps:
        print(f"::error::{len(logger.failed_steps)} de {logger.steps_total} pasos "
              f"fallaron en modo {args.mode}: {', '.join(logger.failed_steps)}")
        sys.exit(1)


def _report_tolerated_errors(mode: str) -> None:
    """
    Errores TOLERADOS: los que el código atrapa, registra como ERROR y deja
    seguir (sin tumbar el paso). Antes solo quedaban en el log — el apagón
    de 82 días de The Odds API fue una línea "❌ API error 401" por liga
    dentro de pasos que terminaban OK. Ahora se reportan en cada corrida:
    anotación ::warning:: en Actions + un Telegram con los primeros.
    """
    from src.utils.log import RUN_ISSUES
    tolerated = [m.strip() for m in RUN_ISSUES.errors()]
    if not tolerated:
        return
    print(f"::warning::{len(tolerated)} errores tolerados en modo {mode}: "
          + " | ".join(m[:140] for m in tolerated[:5]))
    try:
        import html
        from scripts.notify_telegram import send_message
        send_message(
            f"⚠️ <b>{mode}: {len(tolerated)} errores tolerados</b>\n"
            f"(la corrida siguió, pero algo falló)\n\n"
            + "\n".join(f"• {html.escape(m[:180])}" for m in tolerated[:8])
            + ("\n…" if len(tolerated) > 8 else "")
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
