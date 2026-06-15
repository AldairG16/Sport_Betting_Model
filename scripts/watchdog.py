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
    # Solo alerta si el gap es >3h Y estamos en horario de partidos (12-02 UTC).
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
