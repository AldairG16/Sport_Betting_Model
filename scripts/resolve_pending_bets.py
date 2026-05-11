"""
scripts/resolve_pending_bets.py
================================
Agente resolver de bets pending. Busca con web_search los resultados
finales (goles, corners, tarjetas, HT score) de partidos que ya
terminaron pero quedaron en `pending` porque:
  • Football-data.co.uk aún no subió el CSV (corners/cards europeos)
  • The Odds API no cubre la liga (China, ligas menores)
  • Team-name mismatch entre bets_history y matches

Flujo:
  1. Junta bets pending con match_date < NOW - 6h, agrupa por partido.
  2. Por cada partido, decide qué columnas faltan en `matches`.
  3. Llama a Claude API con web_search para obtener los datos faltantes.
  4. UPSERT en `matches`.
  5. Corre update_bet_results() para resolver las bets.
  6. Manda resumen a Telegram con cuántas resolvió, cuántas quedaron y costo.

NO crea apuestas. NO modifica probabilidades. Solo termina de resolver.
"""

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from config.settings import ANTHROPIC_API_KEY
from src.utils.team_normalizer import normalize_team


# ============================================================
# SYSTEM PROMPT
# ============================================================
SYSTEM_PROMPT = """\
Sos un INVESTIGADOR DE RESULTADOS DEPORTIVOS. Tu trabajo es buscar el
RESULTADO FINAL completo de UN partido de fútbol específico y devolver
los datos en JSON estricto.

Buscás en fuentes autoritativas (en este orden de preferencia):
  1. ESPN, BBC Sport, Sky Sports
  2. Sofascore, Flashscore, FotMob
  3. Sitios oficiales de la liga
  4. Wikipedia (solo para datos de goles, NO para corners/tarjetas)

NUNCA inventes datos. Si una métrica no aparece en las fuentes, devolvé
null. Mejor null honesto que dato incorrecto.

Cap web_search: 3 búsquedas máximo. Sé eficiente. Una búsqueda bien
formulada suele resolver todo: "<home> <away> full time result <fecha>"
con la fecha exacta y suele aparecer la página con FT + corners + cards.

VERIFICACIÓN OBLIGATORIA:
  • Fecha del partido debe coincidir con la del usuario (±1 día por
    timezone). Si no coincide → devolvé null en todos los campos.
  • Si el partido fue ABANDONADO, POSTPONED o aún jugándose, "completed"
    debe ser false y todos los datos null.
  • Para AET (alargue/penales): devolvé el resultado al MINUTO 90,
    no el de extra time o penales. Aclaralo en source.

Salida JSON ESTRICTA — solo el JSON, sin markdown, sin prosa:
{
  "home_goals": int | null,
  "away_goals": int | null,
  "home_goals_ht": int | null,        // goles al medio tiempo (min 45)
  "away_goals_ht": int | null,
  "home_corners": int | null,
  "away_corners": int | null,
  "home_yellow": int | null,
  "away_yellow": int | null,
  "home_red": int | null,
  "away_red": int | null,
  "completed": true | false,
  "source_url": "url principal usada",
  "confidence": "high" | "medium" | "low",
  "notes": "máx 1 oración con caveat si corresponde, o null"
}

REGLAS:
  • confidence="high": ≥2 fuentes coinciden en TODOS los campos pedidos.
  • confidence="medium": 1 fuente confiable, o múltiples con leves
    discrepancias en datos secundarios.
  • confidence="low": 1 fuente de baja autoridad, o datos parciales.
  • Si solo conseguís goles (no corners ni tarjetas), devolvé los goles
    y null en el resto. NO inventes corners/tarjetas.
  • Tarjetas: si solo ves "tarjetas totales" sin separar amarillas/rojas,
    asumí TODAS amarillas y dejá rojas en null (el modelo evaluará total).
"""


# ============================================================
# UTILS — qué campos necesita cada bet
# ============================================================

def _required_fields_for_markets(markets: list[str]) -> set[str]:
    """Devuelve el conjunto mínimo de columnas de `matches` que se
    necesitan para resolver TODAS las bets con esos markets."""
    fields = {"home_goals", "away_goals"}  # base — casi todo lo necesita
    for m in markets:
        ml = (m or "").lower()
        if ml.startswith("h1_") or ml.startswith("h2_"):
            fields |= {"home_goals_ht", "away_goals_ht"}
        elif ml.startswith("corners_"):
            fields |= {"home_corners", "away_corners"}
        elif ml.startswith("cards_"):
            fields |= {"home_yellow", "away_yellow", "home_red", "away_red"}
    return fields


def _which_fields_missing(home_norm: str, away_norm: str, date,
                          required: set[str]) -> set[str]:
    """Consulta la tabla matches y devuelve qué campos faltan (NULL).
    Si la row no existe, faltan todos los `required`.
    """
    with engine.connect() as conn:
        # Buscamos en ventana ±1d por timezone
        row = conn.execute(text(f"""
            SELECT {", ".join(required)}
            FROM matches
            WHERE LOWER(home_team) = :h
              AND LOWER(away_team) = :a
              AND date BETWEEN :df AND :dt
            ORDER BY ABS(EXTRACT(EPOCH FROM (date::timestamp - CAST(:exact AS timestamp))))
            LIMIT 1
        """), {
            "h": home_norm.lower(),
            "a": away_norm.lower(),
            "df": (pd.to_datetime(date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            "dt": (pd.to_datetime(date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            "exact": pd.to_datetime(date).strftime("%Y-%m-%d"),
        }).first()
    if row is None:
        return set(required)
    missing = {f for f in required if getattr(row, f) is None}
    return missing


# ============================================================
# FETCH PENDING + GROUPING
# ============================================================

def _fetch_pending_grouped(hours_lag: int = 6,
                            limit_matches: int = 30) -> pd.DataFrame:
    """
    Bets pending > N horas, agrupadas por partido único.
    Devuelve DataFrame con: match, match_date, markets (list).

    `limit_matches` controla el costo: con 30 partidos × $0.03 ≈ $0.90 por
    corrida. Si hay más pending, las extras se procesan en la siguiente.
    """
    df = pd.read_sql(text("""
        SELECT match, MIN(match_date)::timestamp AS match_date,
               ARRAY_AGG(DISTINCT market ORDER BY market) AS markets,
               COUNT(*) AS n_bets
        FROM bets_history
        WHERE result = 'pending'
          AND match_date < NOW() - (:hours || ' hours')::interval
          AND match LIKE '% vs %'
        GROUP BY match
        ORDER BY MIN(match_date)
        LIMIT :lim
    """), engine, params={"hours": hours_lag, "lim": limit_matches})
    return df


# ============================================================
# LLM CALL
# ============================================================

def _ask_claude_for_result(client, home: str, away: str, date_str: str,
                            required_fields: set[str]) -> dict | None:
    """Pide a Claude que busque el resultado del partido. Devuelve dict
    parseado o None si falló."""
    user_msg = (
        f"Buscá el resultado FINAL del siguiente partido y devolvé "
        f"el JSON.\n\n"
        f"Local:  {home}\n"
        f"Visit:  {away}\n"
        f"Fecha:  {date_str} (UTC, ±1 día por timezone)\n\n"
        f"Campos prioritarios necesarios: {', '.join(sorted(required_fields))}\n\n"
        f"Igual devolvé el JSON COMPLETO con todos los campos del schema "
        f"(los que no estén disponibles, null)."
    )

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 3,
        }],
        messages=[{"role": "user", "content": user_msg}],
    )

    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = text_blocks[-1].strip() if text_blocks else "{}"
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"   ⚠️  Parse error: {e}  raw[:120]={raw[:120]}")
        return None


# ============================================================
# UPSERT MATCHES
# ============================================================

VALID_INT_FIELDS = {
    "home_goals", "away_goals",
    "home_goals_ht", "away_goals_ht",
    "home_corners", "away_corners",
    "home_yellow", "away_yellow",
    "home_red", "away_red",
}


def _upsert_match_row(home_raw: str, away_raw: str, date_str: str,
                      data: dict, league_hint: str = "") -> bool:
    """
    UPSERT en `matches`. Solo escribe campos no-NULL del dict para no
    pisar datos correctos con NULL. Devuelve True si insertó/actualizó.
    """
    # Normalizar nombres (igual que fbdata backup y update_bet_results)
    home_norm = normalize_team(home_raw)
    away_norm = normalize_team(away_raw)

    # Filtrar solo campos válidos y no-nulos
    payload = {}
    for k in VALID_INT_FIELDS:
        v = data.get(k)
        if v is None:
            continue
        try:
            payload[k] = int(v)
        except (TypeError, ValueError):
            continue

    if not payload:
        # El LLM no devolvió ningún dato útil → no hacemos nada
        return False

    # Para el INSERT path, completamos también goals con 0 si solo vinieron
    # secundarios (porque la unique constraint requiere row existente).
    # Si los goals no vinieron pero hay corners/cards, intentamos UPDATE only.
    cols_to_set = ", ".join(f"{k} = COALESCE(EXCLUDED.{k}, matches.{k})"
                            for k in payload.keys())

    cols_insert = ["date", "home_team", "away_team",
                    "team_home_norm", "team_away_norm"]
    vals_insert = [":date", ":home", ":away", ":home_norm", ":away_norm"]
    params = {
        "date": date_str,
        "home": home_norm.lower(),
        "away": away_norm.lower(),
        "home_norm": home_norm.lower(),
        "away_norm": away_norm.lower(),
    }

    if league_hint:
        cols_insert.append("league")
        vals_insert.append(":league")
        params["league"] = league_hint

    # Agregar columnas del payload
    for k, v in payload.items():
        cols_insert.append(k)
        vals_insert.append(f":{k}")
        params[k] = v

    sql = f"""
        INSERT INTO matches ({", ".join(cols_insert)})
        VALUES ({", ".join(vals_insert)})
        ON CONFLICT (date, home_team, away_team)
        DO UPDATE SET {cols_to_set}
    """

    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params)
        return True
    except Exception as e:
        print(f"   ❌ Upsert error: {e}")
        return False


# ============================================================
# MAIN
# ============================================================

def main(hours_lag: int = 6, limit_matches: int = 30):
    print(f"\n🔎 RESOLVE PENDING BETS — lag={hours_lag}h, "
          f"max={limit_matches} partidos por corrida\n")

    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY no configurada — abortando.")
        return

    df = _fetch_pending_grouped(hours_lag=hours_lag, limit_matches=limit_matches)
    if df.empty:
        print("✓ Sin bets pending > 6h. Nada que resolver.")
        return

    print(f"📋 {len(df)} partido(s) único(s) con bets pending\n")

    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    n_queried = 0
    n_upserted = 0
    n_skipped_complete = 0

    for _, row in df.iterrows():
        match = row["match"]
        markets = list(row["markets"]) if row["markets"] is not None else []
        if " vs " not in match:
            continue
        home_raw, away_raw = match.split(" vs ", 1)
        home_norm = normalize_team(home_raw)
        away_norm = normalize_team(away_raw)
        date_str = pd.to_datetime(row["match_date"]).strftime("%Y-%m-%d")

        required = _required_fields_for_markets(markets)
        missing = _which_fields_missing(home_norm, away_norm, date_str, required)

        if not missing:
            n_skipped_complete += 1
            print(f"  · {match[:50]} — matches ya completo, skip")
            continue

        print(f"  🔎 {match[:50]:50s} {date_str} faltan: {sorted(missing)}")
        try:
            data = _ask_claude_for_result(client, home_raw, away_raw, date_str,
                                          missing)
        except Exception as e:
            print(f"     ❌ Claude error: {e}")
            continue
        n_queried += 1

        if data is None:
            continue
        if not data.get("completed"):
            print(f"     ⚠️  Claude reporta 'not completed' "
                  f"(notes={data.get('notes')})")
            continue

        ok = _upsert_match_row(home_raw, away_raw, date_str, data)
        if ok:
            n_upserted += 1
            conf = data.get("confidence", "?")
            print(f"     ✅ upsert OK (confidence={conf}, "
                  f"src={(data.get('source_url') or '')[:60]})")

    print(f"\n📊 Resumen: consultas Claude={n_queried}, "
          f"upserts={n_upserted}, skipped (ya completo)={n_skipped_complete}")

    if n_upserted == 0:
        print("✓ Nada se upserteo — no re-evaluamos bets.")
        return

    # ── Re-evaluar bets pending con los nuevos datos ─────────────────
    print("\n📡 Re-evaluando bets_history con los datos nuevos...\n")
    from src.models.save_bets import update_bet_results
    update_bet_results()

    # ── Notificar a Telegram (silencio si nada cambió) ──────────────
    try:
        from scripts.notify_telegram import send_message
        send_message(
            f"🔎 <b>RESOLVE PENDING</b>\n"
            f"Consultas a Claude: {n_queried}\n"
            f"Upserts en matches: {n_upserted}\n"
            f"Ya estaban completas: {n_skipped_complete}\n\n"
            f"<i>Ver bets_history para confirmar las resoluciones.</i>"
        )
    except Exception as e:
        print(f"⚠️  No se pudo notificar a Telegram: {e}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours-lag", type=int, default=6,
                    help="Solo bets con kickoff > N horas (default 6)")
    ap.add_argument("--limit", type=int, default=30,
                    help="Máximo partidos por corrida (default 30)")
    args = ap.parse_args()

    try:
        main(hours_lag=args.hours_lag, limit_matches=args.limit)
    except BaseException as _exc:
        traceback.print_exc()
        try:
            from scripts.notify_telegram import send_message
            send_message(
                "🚨 <b>RESOLVE PENDING CRASH</b>\n"
                f"<code>{type(_exc).__name__}: {_exc}</code>"
            )
        except Exception:
            pass
        sys.exit(1)
