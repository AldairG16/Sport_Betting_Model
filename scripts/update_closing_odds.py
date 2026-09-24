"""
scripts/update_closing_odds.py
================================
Actualiza closing odds de bets pendientes.

SIN LLAMADA EXTRA A LA API:
  Reutiliza las odds que ya estan guardadas en upcoming_matches.
  Solo hace una llamada API de respaldo si el partido ya no esta en DB
  (porque fue eliminado tras completarse).

Esto ahorra todos los creditos que antes consumia get_live_odds().
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.utils.log import get_logger

log = get_logger(__name__)


# ============================================================
# QUÉ PARTIDOS RECARGAR PARA EL CIERRE (22-sep-26)
# ============================================================
# Ventana de kickoff (minutos desde ahora) en la que vale la pena descargar
# la cuota de cierre. Con el closing corriendo cada ~30 min, cada partido
# cae en 2 corridas dentro de la ventana.
CLOSING_WINDOW_MIN = (10, 80)

# Mercados que llegan por evento: una bet en ellos necesita re-enriquecer
# su evento (cuesta ~5-7 créditos) para tener un cierre real.
_SPECIALTY_PREFIXES = ("btts", "dnb_", "dc_", "h1_", "h2_", "corners_", "cards_")


def _norm(team: str) -> str:
    return normalize_team(str(team)).lower().strip()


def plan_closing_targets(upcoming: pd.DataFrame, bets: pd.DataFrame,
                         shadow: pd.DataFrame) -> list[dict]:
    """
    Función pura. De los partidos de upcoming_matches en la ventana, los que
    tienen bets pendientes o candidatas shadow, con dos marcas:
      specialty         alguna BET es de un mercado por evento (se recarga
                        en cada corrida, como antes);
      shadow_specialty  alguna candidata SHADOW lo es (24-sep-26: sin esto,
                        ambos anotan y empate anulado nunca tenían un cierre
                        válido y el aprendizaje no recibía esos datos; ver
                        update_upcoming_matches.refresh_for_closing).
    """
    wanted: dict[tuple, dict] = {}
    for df, flag in ((bets, "specialty"), (shadow, "shadow_specialty")):
        for _, r in df.iterrows():
            try:
                h, a = str(r["match"]).split(" vs ")
            except ValueError:
                continue
            flags = wanted.setdefault((_norm(h), _norm(a)),
                                      {"specialty": False, "shadow_specialty": False})
            if str(r.get("market", "")).startswith(_SPECIALTY_PREFIXES):
                flags[flag] = True
    targets = []
    for _, u in upcoming.iterrows():
        key = (u["home_team_norm"], u["away_team_norm"])
        if key in wanted:
            targets.append({"sport_key": u["sport_key"], "home_norm": key[0],
                            "away_norm": key[1], "match_date": u["match_date"],
                            **wanted[key]})
    return targets


def select_closing_targets(window_min: tuple = CLOSING_WINDOW_MIN) -> list[dict]:
    """Partidos con kickoff dentro de la ventana que tienen bets pendientes
    o candidatas shadow (solo esos merecen gastar créditos)."""
    lo, hi = window_min
    params = {"lo": lo, "hi": hi}
    upcoming = pd.read_sql(text("""
        SELECT DISTINCT ON (home_team_norm, away_team_norm, sport_key)
               sport_key, home_team_norm, away_team_norm, match_date
        FROM upcoming_matches
        WHERE match_date BETWEEN NOW() + make_interval(mins => :lo)
                             AND NOW() + make_interval(mins => :hi)
        ORDER BY home_team_norm, away_team_norm, sport_key, updated_at DESC NULLS LAST
    """), engine, params=params)
    if upcoming.empty:
        return []
    # bets_history.match_date es TIMESTAMP sin zona (UTC): mismo patrón que
    # la consulta de update_closing_odds de abajo
    bets = pd.read_sql(text("""
        SELECT match, market FROM bets_history
        WHERE result = 'pending'
          AND match_date BETWEEN (NOW() AT TIME ZONE 'UTC') + make_interval(mins => :lo)
                             AND (NOW() AT TIME ZONE 'UTC') + make_interval(mins => :hi)
    """), engine, params=params)
    try:
        shadow = pd.read_sql(text("""
            SELECT DISTINCT match, market FROM shadow_bets
            WHERE match_date BETWEEN NOW() + make_interval(mins => :lo)
                                 AND NOW() + make_interval(mins => :hi)
        """), engine, params=params)
    except Exception:
        shadow = pd.DataFrame(columns=["match", "market"])   # tabla aún no creada
    return plan_closing_targets(upcoming, bets, shadow)


# ============================================================
# MAIN
# ============================================================
def update_closing_odds(only_near_kickoff: bool = True):
    """
    Actualiza closing odds en bets_history.

    only_near_kickoff: si True (default), SOLO procesa bets cuyo partido
        arranca dentro de los próximos 90 minutos. Esto es CRÍTICO porque
        este script suele correr varias veces al día — sin este filtro,
        partidos que arrancan a las 23:00 reciben "closing odds" del
        mediodía, contaminando la métrica CLV.

        El audit del 04-may-26 reveló que 157 bets con CLV+ acumularon
        -23u — síntoma claro de closing odds capturadas demasiado pronto.
    """
    print("\n📡 ACTUALIZANDO CLOSING ODDS (desde DB, sin llamada API)...\n")

    from src.models.save_bets import BETS_CLOSING_ALTER_SQL
    with engine.begin() as conn:
        conn.execute(text(BETS_CLOSING_ALTER_SQL))

    # Bets sin cierre, o con un cierre que puede mejorarse: sin hora de
    # descarga conocida o descargado antes del kickoff (22-sep-26). Cada
    # corrida acerca el cierre al precio final; closing_fetched_at dice
    # cuándo se descargó (ver src/utils/closing_quality.py).
    # Nota: bets_history usa result='pending' (no NULL) — filtrar por NULL
    # dejaba el script sin trabajo y ningún closing odds se guardaba.
    if only_near_kickoff:
        # D1 (ronda 9): antes solo bets 'pending' con partido en
        # [NOW-4h, NOW+90min]. Dos defectos: (1) una bet RESUELTA sin closing
        # quedaba invisible para siempre (btts 0/22 medido), (2) la ventana
        # estrecha perdía confirmaciones tardías. Ahora: resultados finales
        # incluidos y ventana de recuperación de 7 días (la retención de
        # upcoming_matches), preservando el objetivo de capturar el precio
        # cercano al kickoff.
        bets = pd.read_sql(text("""
            SELECT id, match, market, odds, match_date, closing_odds, closing_fetched_at,
                   pin_close_at
            FROM bets_history
            WHERE (closing_odds IS NULL OR closing_fetched_at IS NULL
                   OR closing_fetched_at < match_date)
              AND result IN ('pending','win','loss','half_win','half_loss','push')
              AND match_date <= NOW() AT TIME ZONE 'UTC' + INTERVAL '90 minutes'
              AND match_date >= NOW() AT TIME ZONE 'UTC' - INTERVAL '7 days'
        """), engine)
    else:
        # Modo backfill (one_shot_data_quality_cleanup): TODO lo rellenable
        bets = pd.read_sql("""
            SELECT id, match, market, odds, match_date, closing_odds, closing_fetched_at,
                   pin_close_at
            FROM bets_history
            WHERE closing_odds IS NULL
              AND match_date >= NOW() AT TIME ZONE 'UTC' - INTERVAL '120 days'
        """, engine)

    if bets.empty:
        print("✅ No hay bets pendientes de closing odds")
        _close_shadow()     # las candidatas shadow se cierran igual
        return

    print(f"📊 Bets pendientes: {len(bets)}")

    updates = 0
    not_found = 0

    # Un solo mapeo mercado → cuota de cierre para apuestas Y shadow
    # (save_bets.closing_updates). Antes este script tenía su propia
    # copia, que buscaba "btts_yes" cuando el mercado se guarda como "btts":
    # ninguna apuesta BTTS recibía cierre ni CLV. Además tomaba la fila más
    # reciente de los dos equipos (podía ser OTRO partido entre ellos); la
    # compartida busca la más cercana al kickoff (±4h). Desde el 24-sep-26
    # guarda también el cierre de Pinnacle de cada apuesta (solo mide).
    from src.models.save_bets import _nearest_market_row, apply_closing_updates, closing_updates

    with engine.begin() as conn:
        for _, bet in bets.iterrows():
            match   = bet["match"]
            market  = bet["market"]
            match_date = pd.to_datetime(bet["match_date"], utc=True)

            try:
                home_raw, away_raw = match.split(" vs ")
            except ValueError:
                continue

            try:
                odds_df = _nearest_market_row(home_raw, away_raw, match_date)
            except Exception as e:
                log.warning(f"⚠️  closing: no se pudo buscar {match}: {type(e).__name__}")
                continue

            if odds_df.empty:
                not_found += 1
                continue

            # sin hora conocida solo rellena un hueco (el CLV gate y el
            # aprendizaje no usan cierres sin hora) — ver closing_updates
            sets = closing_updates(bet, market, odds_df.iloc[0], match_date)
            if not sets:
                continue
            apply_closing_updates(conn, "bets_history", bet["id"], sets)

            updates += 1

    print(f"✅ Closing odds actualizadas: {updates}")

    _close_shadow()
    if not_found > 0:
        log.warning(f"⚠️  Partidos no encontrados en DB (ya completados): {not_found}")


def _close_shadow():
    """
    C1/D1 (rondas 8-9): closing de las candidatas shadow — el dato del que
    aprende el peso del modelo. El gemelo en src/models/save_bets.py casi no
    corre en producción; el shadow se rellena AQUÍ, donde corre el closing.
    Corre SIEMPRE: hasta el 23-sep-26 quedaba después del `return` de "no
    hay bets pendientes" y un slate sin apuestas no cerraba su shadow.
    """
    try:
        from src.models.save_bets import _update_shadow_closing
        _update_shadow_closing()
    except Exception as e:
        log.warning(f"⚠️  shadow closing omitido: {type(e).__name__}")


if __name__ == "__main__":
    update_closing_odds()
