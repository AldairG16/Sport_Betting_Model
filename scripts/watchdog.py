"""
scripts/watchdog.py
===================
Monitoreo de ausencia del pipeline. Corre cada 6 horas vía watchdog.yml.

Detecta silenciosamente y alerta SOLO si algo está mal:
  1. DB inaccesible (causa raíz del silencio apr-jun 2026: contraseña expirada)
  2. upcoming_matches sin actualizar en >26h (morning/evening no corrió)
  3. Muchas bets bloqueadas en 'pending' viejo sin resolver (resolución rota)
  4. Analyst heartbeat con gap largo durante ventana activa

Si todo está bien → no envía nada. Silencio = salud.
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _send_alert(msg: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print(f"[WATCHDOG] Sin credenciales Telegram — alert no enviada:\n{msg}")
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[WATCHDOG] No se pudo enviar Telegram: {e}")


def _hours_ago(dt) -> float:
    """Devuelve horas desde dt hasta ahora (UTC)."""
    if dt is None:
        return float("inf")
    import pandas as pd
    ts = pd.to_datetime(dt)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


# Dias sin una sola apuesta nueva antes de alertar. Holgado a proposito: el
# modelo es selectivo y puede pasar un dia sin encontrar valor; una semana
# significa que algo corriente arriba dejo de producir.
DIAS_SIN_BETS_ALERTA = 7


def run_watchdog():
    now_utc = datetime.now(timezone.utc)
    print(f"[WATCHDOG] {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")

    issues: list[str] = []

    # ── CHECK 1: DB accesible ────────────────────────────────────────────────
    try:
        from config.database import engine
        from sqlalchemy import text
        import pandas as pd
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("  ✅ DB accesible")
    except Exception as e:
        short = str(e)[:300].replace("<", "&lt;").replace(">", "&gt;")
        issues.append(
            "🔴 <b>DB INACCESIBLE</b>\n"
            f"<code>{short}</code>\n"
            "→ Verifica el secret DB_URL en GitHub Actions Settings."
        )
        # Sin DB no podemos hacer más checks — alertar y salir
        _send_alert(
            "🚨 <b>WATCHDOG — DB CAÍDA</b>\n\n"
            + issues[0]
            + "\n\n<i>El pipeline está completamente detenido.</i>"
        )
        print(f"  ❌ DB inaccesible: {e}")
        return

    # ── CHECK 2: upcoming_matches actualizado en las últimas 26 horas ────────
    # update_upcoming_matches corre en morning (06:00 MX) y evening (07:30 MX).
    # 26h da margen para días con retrasos o runs lentos.
    try:
        r = pd.read_sql(
            "SELECT MAX(updated_at) AS last_update FROM upcoming_matches",
            engine,
        )
        last_update = r.iloc[0]["last_update"] if not r.empty else None
        age_h = _hours_ago(last_update)
        print(f"  upcoming_matches última actualización: {age_h:.1f}h atrás")

        if age_h > 26:
            issues.append(
                f"🔴 <b>PIPELINE SIN CORRER</b>\n"
                f"upcoming_matches no se actualiza hace {age_h:.0f}h "
                f"(umbral: 26h).\n"
                f"→ Verifica que morning.yml y evening.yml corran en GH Actions."
            )
        else:
            print("  ✅ Pipeline corrió recientemente")
    except Exception as e:
        issues.append(f"⚠️ No se pudo leer upcoming_matches: {e}")

    # ── CHECK 3: bets 'pending' muy antiguas sin resolver (>5 días) ──────────
    # Más de 10 bets bloqueadas 5+ días = resolver roto o mercado sin fuente.
    try:
        stuck = pd.read_sql(
            """
            SELECT COUNT(*) AS n
            FROM bets_history
            WHERE result = 'pending'
              AND match_date < NOW() - INTERVAL '5 days'
            """,
            engine,
        )
        n_stuck = int(stuck.iloc[0]["n"]) if not stuck.empty else 0
        print(f"  Bets pending >5d: {n_stuck}")

        if n_stuck > 10:
            issues.append(
                f"🟡 <b>{n_stuck} BETS ATASCADAS</b>\n"
                f"Hay {n_stuck} bets con result='pending' de hace más de 5 días.\n"
                f"→ Puede indicar que resolve_pending.yml no está corriendo "
                f"o que hay ligas sin fuente de resultados."
            )
        else:
            print("  ✅ Sin bets atascadas")
    except Exception as e:
        issues.append(f"⚠️ No se pudo verificar bets atascadas: {e}")

    # ── CHECK 4: analyst_heartbeat — gap largo en horas de partido ───────────
    # Solo alerta si el gap es >3h Y estamos en horario de partidos (12-02 UTC),
    # y solo si el analista se supone EN SERVICIO. Con `pre_kickoff.yml`
    # desactivado a proposito el heartbeat envejece para siempre, y alertar por
    # ello en cada corrida convierte al watchdog en ruido de fondo: se aprende a
    # ignorarlo, y entonces no avisa el dia que el fallo es real.
    try:
        from config.settings import PRE_KICKOFF_ANALYST_ENABLED
    except Exception:
        PRE_KICKOFF_ANALYST_ENABLED = True   # ante la duda, vigilar

    if not PRE_KICKOFF_ANALYST_ENABLED:
        print("  ⏸️  Analyst heartbeat: chequeo omitido "
              "(PRE_KICKOFF_ANALYST_ENABLED=false)")
    else:
        try:
            hb = pd.read_sql(
                "SELECT MAX(ran_at) AS last_ran FROM analyst_heartbeat",
                engine,
            )
            last_ran = hb.iloc[0]["last_ran"] if not hb.empty else None
            hb_age_h = _hours_ago(last_ran)
            hour_utc = now_utc.hour
            in_match_window = 12 <= hour_utc or hour_utc <= 2
            print(f"  Analyst heartbeat: {hb_age_h:.1f}h atrás (match window={in_match_window})")

            if hb_age_h > 3 and in_match_window:
                issues.append(
                    f"🟡 <b>ANALISTA PRE-KICKOFF SIN CORRER</b>\n"
                    f"Último heartbeat hace {hb_age_h:.0f}h en horario de partidos.\n"
                    f"→ Verifica pre_kickoff.yml en GH Actions."
                )
            else:
                print("  ✅ Analyst heartbeat OK")
        except Exception as e:
            print(f"  ⚠️ No se pudo leer analyst_heartbeat (tabla puede no existir): {e}")

    # ── CHECK 5: el pipeline corre pero no produce nada ──────────────────────
    # CHECK 2 mira MAX(updated_at) de upcoming_matches, y esa marca se mueve
    # AUNQUE no entre un solo partido: el cleanup de update_all() borra filas
    # viejas y toca la tabla igual. Es un check de actividad, no de producto.
    #
    # Eso dejo pasar el incidente del 17-jun-26: la API key de The Odds API se
    # desactivo (401 DEACTIVATED_KEY, pago fallido) y las 15 ligas devolvieron
    # "0 partidos procesados" durante 82 dias. Cada corrida salia verde en
    # Actions —`run_step` captura el error del paso y sigue— y el watchdog
    # habria dicho "pipeline corrio recientemente" todo ese tiempo.
    #
    # Este check mira el PRODUCTO: si no hay partidos futuros cargados, el
    # fetch no esta trayendo nada, por muy verde que salga el workflow.
    try:
        fut = pd.read_sql(
            "SELECT COUNT(*) AS n FROM upcoming_matches WHERE match_date >= NOW()",
            engine,
        )
        n_fut = int(fut.iloc[0]["n"])
        print(f"  Partidos futuros cargados: {n_fut}")

        if n_fut == 0:
            issues.append(
                "🔴 <b>SIN PARTIDOS CARGADOS</b>\n"
                "`upcoming_matches` no tiene un solo partido futuro.\n"
                "El pipeline corre pero no trae datos.\n"
                "→ Causa mas probable: la API key de The Odds API caducada, sin "
                "credito o desactivada por pago fallido. Revisa el log de "
                "morning.yml buscando <code>DEACTIVATED_KEY</code> o "
                "<code>API error 401</code>."
            )
        else:
            print("  ✅ Hay partidos cargados")
    except Exception as e:
        issues.append(f"⚠️ No se pudo verificar partidos cargados: {e}")

    # ── CHECK 6: hace cuanto que no se registra una apuesta ──────────────────
    # Complemento del anterior y mas rapido de disparar: sin cuotas no hay
    # picks, asi que bets_history deja de crecer desde el dia uno, mientras que
    # upcoming_matches tarda dos o tres dias en vaciarse por el cleanup.
    #
    # Se mide sobre match_date (el kickoff) porque el INSERT de save_bets.py no
    # guarda timestamp de creacion. Con el modelo sano ese maximo esta en el
    # FUTURO, porque se apuesta a partidos por jugar; cuando el pipeline deja de
    # producir, retrocede hacia el pasado. Umbral holgado a proposito: el modelo
    # es selectivo y puede pasar un dia sin encontrar valor, no una semana.
    try:
        ub = pd.read_sql("SELECT MAX(match_date) AS last_bet FROM bets_history", engine)
        last_bet = ub.iloc[0]["last_bet"] if not ub.empty else None
        bet_age_d = _hours_ago(last_bet) / 24
        print(f"  Ultima apuesta registrada: {bet_age_d:.1f}d atras")

        if bet_age_d > DIAS_SIN_BETS_ALERTA:
            issues.append(
                "🔴 <b>SIN APUESTAS NUEVAS</b>\n"
                f"La apuesta mas reciente en `bets_history` es de hace "
                f"{bet_age_d:.0f} dias.\n"
                "→ O el fetch no trae cuotas, o los filtros rechazan todo. "
                "Empieza por el log de morning.yml."
            )
        else:
            print("  ✅ Se siguen registrando apuestas")
    except Exception as e:
        issues.append(f"⚠️ No se pudo verificar apuestas recientes: {e}")

    # ── Resultado ─────────────────────────────────────────────────────────────
    if not issues:
        print("[WATCHDOG] Todo OK — sin alertas.")
        return

    alert = (
        "🚨 <b>WATCHDOG — PROBLEMAS DETECTADOS</b>\n"
        f"<i>{now_utc.strftime('%Y-%m-%d %H:%M UTC')}</i>\n\n"
        + "\n\n".join(issues)
    )
    _send_alert(alert)
    print(f"[WATCHDOG] {len(issues)} problema(s) detectado(s) — alerta enviada.")


if __name__ == "__main__":
    run_watchdog()
