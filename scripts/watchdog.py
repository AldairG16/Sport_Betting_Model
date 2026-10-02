"""
scripts/watchdog.py
===================
Monitoreo de ausencia del pipeline. Corre cada 6 horas vía watchdog.yml.

Detecta silenciosamente y alerta SOLO si algo está mal:
  1. DB inaccesible (causa raíz del silencio apr-jun 2026: contraseña expirada)
  2. upcoming_matches sin actualizar en >26h (morning/evening no corrió)
  3. Apuestas reales sin liquidar: 'pending' de hace más de 4 días
  4. Analyst heartbeat con gap largo durante ventana activa
  5-6. Sin partidos cargados / sin candidatas nuevas en 4 días
  7-10. Closing parado o irregular, weekly, reintento de resultados y
        resolvedor sin correr (src/utils/pipeline_runs.py)

Si todo está bien → no envía nada. Silencio = salud. Si la alerta no se
puede entregar, la corrida termina en rojo (GitHub avisa por correo).
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _send_alert(msg: str) -> bool:
    """True si Telegram aceptó el mensaje. Hasta el 1-oct-26 no se miraba la
    respuesta: un 400 (HTML inválido) o un token vencido pasaban en silencio,
    y el watchdog es justamente el que tiene que avisar."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print(f"[WATCHDOG] Sin credenciales Telegram — alert no enviada:\n{msg}")
        return False
    import requests
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
            if r.status_code == 200:
                return True
            print(f"[WATCHDOG] Telegram respondió {r.status_code}: {r.text[:200]}")
            if r.status_code == 400:
                # HTML inválido: se manda en texto plano para que llegue igual
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": msg},
                    timeout=10,
                )
                if r.status_code == 200:
                    return True
        except Exception as e:
            print(f"[WATCHDOG] No se pudo enviar Telegram (intento {attempt + 1}/3): {e}")
    return False


def _hours_ago(dt) -> float:
    """Devuelve horas desde dt hasta ahora (UTC)."""
    if dt is None:
        return float("inf")
    import pandas as pd
    ts = pd.to_datetime(dt)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


# Días sin una sola candidata nueva antes de alertar. Desde el modo recolección
# (25-sep-26) pueden pasar semanas sin una apuesta REAL, así que lo que prueba
# que el pipeline produce son las candidatas en sombra (todos los días hay).
DIAS_SIN_CANDIDATAS_ALERTA = 4
# Una apuesta real todavía 'pending' a los 4 días = la liquidación no corrió:
# su propio timeout la pasa a 'unresolved' a los 3 (save_bets.update_bet_results).
# Las que terminan sin resultado ('stale') avisan UNA vez, como ERROR, en la
# corrida que las marca; aquí se repetirían cada 6 horas.
DIAS_PENDING_ATASCADA = 4


def run_watchdog() -> int:
    """0 = sano o alerta entregada; 1 = DB caída o alerta NO entregada (la
    corrida queda en rojo y GitHub avisa por correo: segundo canal)."""
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
        # Sin DB no podemos hacer más checks — alertar y salir en rojo
        _send_alert(
            "🚨 <b>WATCHDOG — DB CAÍDA</b>\n\n"
            + issues[0]
            + "\n\n<i>El pipeline está completamente detenido.</i>"
        )
        print(f"  ❌ DB inaccesible: {e}")
        return 1

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

    # ── CHECK 3: apuestas REALES sin liquidar ────────────────────────────────
    # Antes: más de 10 bets 'pending' de >5 días. Hoy hay pocas apuestas
    # reales y basta una (1-oct-26): si sigue 'pending' a los 4 días, la
    # liquidación del evening / late_results no está corriendo.
    try:
        stuck = pd.read_sql(
            f"""
            SELECT COUNT(*) AS n FROM bets_history
            WHERE result = 'pending'
              AND match_date < NOW() - INTERVAL '{DIAS_PENDING_ATASCADA} days'
            """,
            engine,
        )
        n_stuck = int(stuck.iloc[0]["n"]) if not stuck.empty else 0
        print(f"  Bets pending >{DIAS_PENDING_ATASCADA}d: {n_stuck}")

        if n_stuck:
            issues.append(
                f"🟡 <b>{n_stuck} APUESTAS SIN LIQUIDAR</b>\n"
                f"Siguen 'pending' {DIAS_PENDING_ATASCADA}+ días después del partido.\n"
                f"→ La liquidación (evening / late_results) no está corriendo: "
                f"sin ella no cuentan en el bankroll."
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

    # ── CHECK 6: el pipeline sigue produciendo candidatas ────────────────────
    # Complemento del anterior y mas rapido de disparar: sin cuotas no hay
    # candidatas, mientras que upcoming_matches tarda dos o tres dias en
    # vaciarse por el cleanup. Hasta el 1-oct-26 se medía la última apuesta
    # REAL; con el modo recolección pueden pasar semanas sin una, y la alerta
    # habría sonado cada semana sin que nada estuviera roto.
    try:
        sb = pd.read_sql("SELECT MAX(created_at) AS last_shadow FROM shadow_bets", engine)
        last_shadow = sb.iloc[0]["last_shadow"] if not sb.empty else None
        shadow_age_d = _hours_ago(last_shadow) / 24
        print(f"  Ultima candidata registrada: {shadow_age_d:.1f}d atras")

        if shadow_age_d > DIAS_SIN_CANDIDATAS_ALERTA:
            issues.append(
                "🔴 <b>SIN CANDIDATAS NUEVAS</b>\n"
                f"La última candidata en `shadow_bets` es de hace {shadow_age_d:.0f} días.\n"
                "→ El pipeline no está evaluando partidos: o el fetch no trae "
                "cuotas o la etapa de predicción falla. Empieza por el log de morning.yml."
            )
        else:
            print("  ✅ Se siguen registrando candidatas")
    except Exception as e:
        issues.append(f"⚠️ No se pudo verificar candidatas recientes: {e}")

    # ── CHECK 7-8: closing y weekly, que dispara un servicio externo ─────────
    # CHECK 2 solo ve morning/evening. Si cron-job.org deja de disparar el
    # closing (cada 30 min) o el weekly (lunes), nada falla: simplemente no
    # corren, y se pierden las confirmaciones antes de cada partido y el
    # aprendizaje. Cada corrida deja una fila en pipeline_runs (24-sep-26).
    try:
        from src.utils.pipeline_runs import check_scheduled_runs
        run_issues = check_scheduled_runs(engine, now_utc)
        issues.extend(run_issues)
        if not run_issues:
            print("  ✅ Closing y weekly corren a su ritmo")
    except Exception as e:
        import html
        issues.append(f"⚠️ No se pudo verificar el closing ni el weekly: "
                      f"{html.escape(str(e)[:200])}")

    # ── Resultado ─────────────────────────────────────────────────────────────
    if not issues:
        print("[WATCHDOG] Todo OK — sin alertas.")
        return 0

    alert = (
        "🚨 <b>WATCHDOG — PROBLEMAS DETECTADOS</b>\n"
        f"<i>{now_utc.strftime('%Y-%m-%d %H:%M UTC')}</i>\n\n"
        + "\n\n".join(issues)
    )
    if _send_alert(alert):
        print(f"[WATCHDOG] {len(issues)} problema(s) detectado(s) — alerta enviada.")
        return 0
    print(f"::error::Watchdog: {len(issues)} problema(s) y la alerta de Telegram NO se entregó")
    print(alert)
    return 1


if __name__ == "__main__":
    sys.exit(run_watchdog())
