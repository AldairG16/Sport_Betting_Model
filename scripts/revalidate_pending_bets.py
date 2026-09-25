"""
scripts/revalidate_pending_bets.py
===================================
REVALIDACIÓN de bets contra odds FRESCAS pre-kickoff.

Problema que resuelve:
  El ciclo morning (6AM) genera bets con las odds de ese momento. Durante
  el día el mercado se mueve: la odd 2.10 que vimos a las 6AM puede ser
  1.95 al mediodía. Si apostamos la 1.95 "creyendo" que era 2.10, el edge
  que calculamos ya no existe — apostamos peor de lo que el modelo decidió.

Solución (corre en el modo closing, justo antes de los kickoffs):
  Para cada bet pending del día, compara las odds guardadas vs las odds
  frescas de upcoming_matches (re-fetcheadas por step_pre_kickoff_closing):

  1. Odd fresca MEJOR o igual → actualizar la bet a la odd fresca
     (en paper, tomarías el número mejor; el edge sube o se mantiene).
  2. Odd fresca PEOR → recalcular edge con la odd fresca:
     - edge nuevo >= KEEP_EDGE (2%) → mantener la bet a la odd fresca
       (el value sobrevive la movida).
     - edge nuevo < KEEP_EDGE → CANCELAR: marcar result='stale' con
       profit 0 (excluida de métricas, igual que cleanup_stale_bets).
       El mercado absorbió el edge — no apostar.
  3. Filtro Pinnacle (25-sep-26), el mismo del pipeline: la bet que sigue
     viva se cancela si a la cuota de ahora no tiene valor frente a Pinnacle
     (p_pinnacle − 1/cuota < KEEP_EDGE), o si no hay precio de Pinnacle y su
     grupo "sinref:" todavía no se reactivó con evidencia. Así las pendientes
     de antes del filtro tampoco llegan como confirmadas sin valor real.

Todo cambia queda anotado en decision_log.revalidation para autopsia.

READ about créditos: este script NO llama a la API — solo lee las odds
que step_pre_kickoff_closing ya descargó.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from src.features.pinnacle import PIN_COLS, no_reference_group, pinnacle_prob
from src.utils.team_normalizer import normalize_team
from src.utils.log import get_logger
from src.utils.min_odds import (HOW_TO_READ, MIN_EDGE_TO_PLACE, fmt_american,
                                playdoit_line, to_american)

log = get_logger(__name__)

# Edge mínimo para MANTENER una bet cuya odd se movió en contra — el mismo
# umbral de la "cuota mínima" que se muestra para apostar en PlayDoit
KEEP_EDGE = MIN_EDGE_TO_PLACE


def sharp_decision(market: str, row, price, reactivated, recorded_pin=None):
    """
    Filtro Pinnacle del pipeline aplicado a una bet pendiente. Función pura:
    devuelve (motivo para cancelarla o None, probabilidad de Pinnacle usada).
    El precio de Pinnacle sale de la fila fresca; si esta no lo trae, del que
    se guardó con la bet (pin_close_prob o el del momento del pick).
    """
    p_pin = pinnacle_prob(market, row)
    if p_pin is None:
        try:
            p = float(recorded_pin)
            p_pin = p if 0 < p < 1 else None
        except (TypeError, ValueError):
            p_pin = None
    if p_pin is None:
        return (None if no_reference_group(market) in reactivated else "cancelled_no_reference"), None
    if not price or p_pin - 1.0 / float(price) < KEEP_EDGE:
        return "cancelled_no_value_pinnacle", p_pin
    return None, p_pin


def _reactivated_groups() -> set:
    """Grupos que el shadow ya reactivó (incluye los "sinref:")."""
    from scripts.clv_gate import load_shadow_reactivated
    return set(load_shadow_reactivated())


def _fresh_odds_for(result, market: str):
    """Mapea market → columna de odds frescas (mismo mapping que update_closing_odds)."""
    market_map = {
        "home_win":  result.home_odds,
        "draw":      result.draw_odds,
        "away_win":  result.away_odds,
        "over25":    result.over25_odds,
        "under25":   result.under25_odds,
        "btts_yes":  result.btts_yes_odds,
        "btts_no":   result.btts_no_odds,
        "btts":      result.btts_yes_odds,
        "dnb_home":  result.dnb_home_odds,
        "dnb_away":  result.dnb_away_odds,
        "dc_1x":     result.dc_1x_odds,
        "dc_x2":     result.dc_x2_odds,
        "dc_12":     result.dc_12_odds,
        "h1_home":   result.h1_home_odds,
        "h1_draw":   result.h1_draw_odds,
        "h1_away":   result.h1_away_odds,
        "h2_home":   result.h2_home_odds,
        "h2_draw":   result.h2_draw_odds,
        "h2_away":   result.h2_away_odds,
    }
    odds = market_map.get(market)

    if odds is None and market.startswith("ah_"):
        try:
            _, side, line_str = market.split("_", 2)
            bet_line = float(line_str)
            if result.ah_line is not None and abs(float(result.ah_line) - bet_line) < 0.01:
                odds = result.ah_away_odds if side == "away" else result.ah_home_odds
        except (ValueError, AttributeError):
            pass

    if odds is None and market.startswith("corners_"):
        try:
            _, side, line_str = market.split("_", 2)
            if result.corners_line is not None and abs(float(result.corners_line) - float(line_str)) < 0.01:
                odds = result.corners_under_odds if side == "under" else result.corners_over_odds
        except (ValueError, AttributeError):
            pass

    if odds is None and market.startswith("cards_"):
        try:
            _, side, line_str = market.split("_", 2)
            if result.cards_line is not None and abs(float(result.cards_line) - float(line_str)) < 0.01:
                odds = result.cards_under_odds if side == "under" else result.cards_over_odds
        except (ValueError, AttributeError):
            pass

    if odds is None:
        return None
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return None
    return odds if odds > 1 else None



def lineup_guard(verbose: bool = True) -> int:
    """
    GUARDIA DE ALINEACIONES (API-Football): para bets con kickoff en <75min,
    verifica si el goleador de élite del equipo apostado está en el once
    confirmado. Si falta → cancela la bet (result='stale') con la razón en
    decision_log. Sin API key o sin alineación publicada → no-op.
    """
    import os
    if not os.environ.get("API_FOOTBALL_KEY"):
        if verbose:
            print("   Lineup guard: sin API_FOOTBALL_KEY — omitido")
        return 0
    try:
        from src.features.lineups import find_fixture, get_lineup_surnames, star_missing
        from src.models.scorer_model import load_scorer_rates
    except Exception as e:
        print(f"   Lineup guard import fallo: {e}")
        return 0

    bets = pd.read_sql(text("""
        SELECT id, match, market, match_date
        FROM bets_history
        WHERE result = 'pending'
          AND match_date BETWEEN NOW() AND NOW() + INTERVAL '75 minutes'
    """), engine)
    if bets.empty:
        return 0

    rates = load_scorer_rates()
    if not rates:
        return 0

    def top_scorers(team_norm: str) -> list:
        cand = [(i["rate_raw"], p) for p, i in rates.items()
                if i["team"] == team_norm and i["goals"] >= 5 and i["rate_raw"] >= 0.30]
        return [p for _, p in sorted(cand, reverse=True)[:2]]

    HOME_SIDES = ("home_win", "dnb_home", "dc_1x")
    AWAY_SIDES = ("away_win", "dnb_away", "dc_x2")
    cancelled = 0

    with engine.begin() as conn:
        for (match, md), g in bets.groupby(["match", "match_date"]):
            try:
                h_raw, a_raw = str(match).split(" vs ")
            except ValueError:
                continue
            h_n = normalize_team(h_raw)
            a_n = normalize_team(a_raw)
            fid = find_fixture(pd.to_datetime(md).to_pydatetime().replace(tzinfo=None),
                               h_raw, a_raw)
            if not fid:
                continue
            lineups = get_lineup_surnames(fid)
            if not lineups:
                continue
            import difflib
            key_h = max(lineups, key=lambda k: difflib.SequenceMatcher(None, h_n, k).ratio())
            key_a = max(lineups, key=lambda k: difflib.SequenceMatcher(None, a_n, k).ratio())

            miss_home = star_missing(lineups[key_h], top_scorers(h_n))
            miss_away = star_missing(lineups[key_a], top_scorers(a_n))
            if not miss_home and not miss_away:
                continue

            for _, b in g.iterrows():
                mkt = str(b["market"])
                side, player = None, None
                if mkt in HOME_SIDES or mkt.startswith("ah_home"):
                    side, player = "home", miss_home
                elif mkt in AWAY_SIDES or mkt.startswith("ah_away"):
                    side, player = "away", miss_away
                if not side or not player:
                    continue
                note = '{"lineup_guard": {"out": "' + player + '", "side": "' + side + '"}}'
                conn.execute(text("""
                    UPDATE bets_history
                    SET result = 'stale',
                        decision_log = COALESCE(decision_log, '{}'::jsonb)
                                      || jsonb_build_object('lineup_guard',
                                           CAST(:note AS jsonb))
                    WHERE id = :id
                """), {"note": note, "id": int(b["id"])})
                cancelled += 1
                if verbose:
                    print(f"   Cancelada [{mkt}] {match}: {player} fuera del once")

    if verbose and cancelled:
        print(f"   Lineup guard cancelo {cancelled} bets")
    return cancelled


def _cancel(conn, bet_id: int, note: dict, dry_run: bool = False):
    if dry_run:
        return
    conn.execute(text("""
        UPDATE bets_history
        SET result = 'stale',
            profit = 0.0,
            decision_log = COALESCE(decision_log, '{}'::jsonb)
                          || jsonb_build_object('revalidation',
                               to_jsonb(CAST(:note AS jsonb)))
        WHERE id = :id
    """), {"note": str(_json_dumps(note)), "id": int(bet_id)})


def _set_odds(conn, bet_id: int, odds: float, note: dict, dry_run: bool = False):
    if dry_run:
        return
    conn.execute(text("""
        UPDATE bets_history
        SET odds = :odds,
            decision_log = COALESCE(decision_log, '{}'::jsonb)
                          || jsonb_build_object('revalidation',
                               to_jsonb(CAST(:note AS jsonb)))
        WHERE id = :id
    """), {"odds": odds, "note": str(_json_dumps(note)), "id": int(bet_id)})


def revalidate_pending_bets(verbose: bool = True, dry_run: bool = False) -> dict:
    """dry_run: decide igual pero no escribe nada ni corre el lineup guard
    (lo usa el smoke test para ver qué cancelaría ahora)."""
    bets = pd.read_sql(text("""
        SELECT id, match, market, probability, odds,
               COALESCE(pin_close_prob,
                        CAST(decision_log->'market_ctx'->>'pin_prob' AS float)) AS pin_prob
        FROM bets_history
        WHERE result = 'pending'
          AND match_date > NOW()
    """), engine)

    if bets.empty:
        if verbose:
            print("✅ No hay bets pendientes futuras para revalidar")
        return {"status": "no_bets"}

    kept_better, kept_edge, cancelled, no_odds, not_found = 0, 0, 0, 0, 0
    cancelled_sharp = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    # Sin el estado de reactivación no se sabe qué grupos "sinref:" ya tienen
    # evidencia: se asume ninguno (lo conservador es no apostar sin referencia)
    try:
        reactivated = _reactivated_groups()
    except Exception as e:
        log.warning(f"   ⚠️  Reactivaciones no disponibles, se asume ninguna: {e}")
        reactivated = set()

    with engine.begin() as conn:
        for _, bet in bets.iterrows():
            market = str(bet["market"])
            try:
                home_raw, away_raw = str(bet["match"]).split(" vs ")
            except ValueError:
                continue

            home_n = normalize_team(home_raw).lower().strip()
            away_n = normalize_team(away_raw).lower().strip()

            row = conn.execute(text(f"""
                SELECT home_odds, draw_odds, away_odds,
                       over25_odds, under25_odds,
                       btts_yes_odds, btts_no_odds,
                       ah_home_odds, ah_away_odds, ah_line,
                       dnb_home_odds, dnb_away_odds,
                       dc_1x_odds, dc_x2_odds, dc_12_odds,
                       h1_home_odds, h1_draw_odds, h1_away_odds,
                       h2_home_odds, h2_draw_odds, h2_away_odds,
                       corners_over_odds, corners_under_odds, corners_line,
                       cards_over_odds, cards_under_odds, cards_line,
                       {", ".join(PIN_COLS)}
                FROM upcoming_matches
                WHERE home_team_norm = :home
                  AND away_team_norm = :away
                ORDER BY match_date DESC
                LIMIT 1
            """), {"home": home_n, "away": away_n}).fetchone()

            if row is None:
                not_found += 1
                continue

            fresh = _fresh_odds_for(row, market)
            old = float(bet["odds"])
            prob = float(bet["probability"]) if bet["probability"] else None
            note = {"revalidated_at": now_iso, "odds_before": old, "odds_after": fresh}

            # 1. Odd peor y el edge del modelo no sobrevive → cancelar
            if (fresh is not None and fresh < old
                    and not (prob is not None and (prob - 1.0 / fresh) >= KEEP_EDGE)):
                note["decision"] = "cancelled_line_moved"
                note["new_edge"] = round(prob - 1.0 / fresh, 4) if prob else None
                _cancel(conn, bet["id"], note, dry_run)
                cancelled += 1
                continue

            # 2. Filtro Pinnacle a la cuota de ahora (la fresca, o la guardada)
            reason, p_pin = sharp_decision(market, row, fresh or old, reactivated,
                                           bet.get("pin_prob"))
            if reason:
                note["decision"] = reason
                note["pin_prob"] = None if p_pin is None else round(p_pin, 4)
                _cancel(conn, bet["id"], note, dry_run)
                cancelled_sharp += 1
                if verbose:
                    print(f"   🔒 Cancelada [{market}] {bet['match']}: "
                          + ("sin valor frente a Pinnacle" if p_pin is not None
                             else "sin referencia de Pinnacle"))
                continue

            if fresh is None:
                no_odds += 1
                continue

            if fresh >= old:
                # Odd igual o mejor → tomar el número fresco
                _set_odds(conn, bet["id"], fresh, note, dry_run)
                kept_better += 1
            else:
                # Odd peor pero el edge sobrevive (el caso contrario ya se canceló)
                note["decision"] = "kept_edge_survives"
                _set_odds(conn, bet["id"], fresh, note, dry_run)
                kept_edge += 1

    cancelled_ln = 0
    # ── GUARDIA DE ALINEACIONES (después de la revalidación de odds) ──
    try:
        if not dry_run:
            cancelled_ln = lineup_guard(verbose=verbose)
    except Exception as e:
        log.error(f"   ⚠️  Lineup guard falló: {e}")

    summary = {
        "status": "ok",
        "dry_run": dry_run,
        "total": len(bets),
        "lineup_cancelled": cancelled_ln,
        "kept_better_odds": kept_better,
        "kept_edge_survives": kept_edge,
        "cancelled": cancelled,
        "cancelled_sharp": cancelled_sharp,
        "no_fresh_odds": no_odds,
        "match_not_found": not_found,
    }

    if verbose:
        print(f"\n🔁 REVALIDACIÓN PRE-KICKOFF (umbral edge {KEEP_EDGE:.0%})"
              + (" — ENSAYO: no se escribió nada" if dry_run else ""))
        print(f"   Bets evaluadas:        {len(bets)}")
        print(f"   Odd mejor/igual:       {kept_better} (actualizadas al número fresco)")
        print(f"   Odd peor, edge vive:   {kept_edge} (mantenidas a la odd nueva)")
        # print, no log.error: es un contador del resumen, no un fallo. Como
        # ERROR entraba en los "errores tolerados" y mandaba un Telegram de
        # alarma en CADA closing, aun con 0 canceladas (23-sep-26).
        print(f"   ❌ Canceladas:          {cancelled} (línea absorbió el edge)")
        print(f"   🔒 Filtro Pinnacle:     {cancelled_sharp} canceladas (sin valor real o sin referencia)")
        print(f"   Sin odd fresca:        {no_odds}  |  Partido no encontrado: {not_found}")

    return summary


def _format_confirmations(rows: list[dict]) -> str:
    """
    Mensaje agrupado de apuestas confirmadas pre-kickoff. Puro (sin DB):
    rows = lista de dicts con match, market, odds, stake, edge, match_date.
    """
    from zoneinfo import ZoneInfo
    from config.settings import USER_TIMEZONE
    from dashboard.display import market_name, match_name
    tz = ZoneInfo(USER_TIMEZONE)

    lines = ["🎯 <b>APUESTAS CONFIRMADAS</b>",
             "<i>Cuota final revalidada · alineaciones verificadas</i>", ""]
    for r in rows:
        try:
            ko = datetime.fromisoformat(str(r["match_date"]))
            # La base guarda UTC sin zona. Sin esto, astimezone() tomaba la
            # hora del servidor: bien en GitHub (UTC), 6 h mal en una PC de México.
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=timezone.utc)
            hora = ko.astimezone(tz).strftime("%H:%M")
        except (ValueError, TypeError):
            hora = "?"
        edge = float(r.get("edge") or 0)
        lines.append(f"✅ {match_name(r['match'])}  ({hora})")
        lines.append(f"   {market_name(r['market'])} · {r['stake']}u · edge {edge:+.0%}")
        # En formato americano, como lo muestra PlayDoit (24-sep-26): la cuota
        # decimal europea no le servía al dueño para decidir.
        rule = playdoit_line(r.get("probability"), pin_prob=r.get("pin_prob"))
        lines.append(f"   {rule}" if rule else
                     f"   Mejor cuota: {fmt_american(to_american(r.get('odds')))}")
    lines.append("")
    lines.append("<i>Valor final — ya no cambia antes del kickoff. Si PlayDoit "
                 "paga menos que el mínimo, ya no hay valor: no la apuestes.</i>")
    lines.append(f"<i>{HOW_TO_READ}</i>")
    return "\n".join(lines)


def send_kickoff_confirmations(verbose: bool = True) -> int:
    """
    Apuestas OFICIALES pre-kickoff: pendientes con kickoff en <90 min que
    aún no fueron notificadas (marca 'kickoff_notified' en decision_log).
    Ya pasaron por revalidación de cuota y lineup guard de esta corrida —
    es el valor final. Envía UN mensaje agrupado por corrida y solo marca
    como notificadas si el envío a Telegram tuvo éxito.
    """
    rows = pd.read_sql(text("""
        SELECT id, match, market, probability, odds, stake, edge, match_date,
               COALESCE(pin_close_prob,
                        CAST(decision_log->'market_ctx'->>'pin_prob' AS float)) AS pin_prob
        FROM bets_history
        WHERE result = 'pending'
          AND match_date BETWEEN NOW() AND NOW() + INTERVAL '90 minutes'
          AND NOT COALESCE(decision_log, '{}'::jsonb) ? 'kickoff_notified'
        ORDER BY match_date
    """), engine)
    if rows.empty:
        if verbose:
            print("   Confirmaciones: nada nuevo para la ventana pre-kickoff")
        return 0

    msg = _format_confirmations(rows.to_dict("records"))
    from scripts.notify_telegram import send_message
    if not send_message(msg):
        if verbose:
            log.error("   ⚠️  Telegram falló — se reintentará en la próxima corrida")
        return 0

    ids = [int(r["id"]) for r in rows.to_dict("records")]
    note = _json_dumps({"at": datetime.now(timezone.utc).isoformat()})
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE bets_history
            SET decision_log = COALESCE(decision_log, '{}'::jsonb)
                              || jsonb_build_object('kickoff_notified',
                                   CAST(:note AS jsonb))
            WHERE id = ANY(:ids)
        """), {"note": note, "ids": ids})
    if verbose:
        print(f"   🎯 {len(ids)} apuestas confirmadas enviadas a Telegram")
    return len(ids)


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, default=str)


if __name__ == "__main__":
    revalidate_pending_bets()
