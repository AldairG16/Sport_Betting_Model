"""
src/utils/anthropic_budget.py
==============================
Tracking y guard de costos de Anthropic API.

Cada llamada a Claude se registra en la tabla `anthropic_usage` con:
  • input/output tokens
  • web searches usadas
  • costo estimado (USD)

Antes de cada llamada, los scripts deben preguntar `can_call(estimated_usd)`.
Si el gasto del día más la estimación supera ANTHROPIC_DAILY_BUDGET_USD,
la llamada se rechaza y el script saltea sin gastar nada.

Default: $0.30/día → con $9.72 = 32 días de runway.
Ajustable vía env var ANTHROPIC_DAILY_BUDGET_USD.
"""

import os
from sqlalchemy import text


# ============================================================
# PRICING (USD por 1M tokens) — actualizado may-26
# ============================================================
# https://docs.anthropic.com/claude/docs/models-overview
PRICING = {
    "claude-haiku-4-5":       {"input": 1.00, "output": 5.00},   # ~5x más barato que Sonnet
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-5":      {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    "claude-opus-4-5":        {"input": 15.00, "output": 75.00},
}
DEFAULT_PRICING = PRICING["claude-haiku-4-5"]

# Web search: $10 por 1000 búsquedas según docs Anthropic
WEB_SEARCH_COST_USD = 0.01

# Budget diario default — conservador. Override vía env var.
DAILY_BUDGET_USD = float(os.environ.get("ANTHROPIC_DAILY_BUDGET_USD", "0.30"))


# ============================================================
# DDL — idempotent
# ============================================================
_TABLE_ENSURED = False

def _ensure_table(engine):
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS anthropic_usage (
                    day            DATE PRIMARY KEY,
                    calls          INT     DEFAULT 0,
                    input_tokens   BIGINT  DEFAULT 0,
                    output_tokens  BIGINT  DEFAULT 0,
                    cache_read_tokens BIGINT DEFAULT 0,
                    web_searches   INT     DEFAULT 0,
                    cost_usd       NUMERIC(10, 5) DEFAULT 0
                )
            """))
        _TABLE_ENSURED = True
    except Exception:
        pass  # no bloqueamos por DDL


# ============================================================
# QUERIES
# ============================================================

def get_daily_spent_usd(engine) -> float:
    """Devuelve cuánto se gastó HOY (en USD), 0.0 si nada."""
    _ensure_table(engine)
    try:
        with engine.connect() as conn:
            r = conn.execute(text(
                "SELECT cost_usd FROM anthropic_usage WHERE day = CURRENT_DATE"
            )).first()
        return float(r.cost_usd) if r else 0.0
    except Exception:
        return 0.0


def can_call(engine, estimated_cost_usd: float) -> tuple[bool, str]:
    """
    Returns (ok, reason). Si estimated_cost_usd + spent > DAILY_BUDGET_USD,
    devuelve (False, mensaje). Si ok, (True, "").
    """
    spent = get_daily_spent_usd(engine)
    if spent + estimated_cost_usd > DAILY_BUDGET_USD:
        return False, (f"Daily budget tope: spent=${spent:.3f} + "
                       f"est=${estimated_cost_usd:.3f} > "
                       f"limit=${DAILY_BUDGET_USD:.2f}")
    return True, ""


def estimate_call_cost(model: str, est_input_tokens: int = 4000,
                       est_output_tokens: int = 350,
                       web_searches: int = 1) -> float:
    """Estimación conservadora del costo de UNA llamada."""
    p = PRICING.get(model, DEFAULT_PRICING)
    cost = (est_input_tokens / 1_000_000.0) * p["input"]
    cost += (est_output_tokens / 1_000_000.0) * p["output"]
    cost += web_searches * WEB_SEARCH_COST_USD
    return cost


def record_call(engine, input_tokens: int, output_tokens: int,
                model: str, web_searches: int = 0,
                cache_read_tokens: int = 0) -> float:
    """
    Registra el costo REAL de una llamada (usando usage del response).
    Devuelve el cost_usd computado.
    """
    p = PRICING.get(model, DEFAULT_PRICING)
    # Cache reads se cobran a 0.1x del input price
    billable_input = max(0, input_tokens - cache_read_tokens)
    cache_cost     = (cache_read_tokens / 1_000_000.0) * p["input"] * 0.1
    input_cost     = (billable_input / 1_000_000.0) * p["input"]
    output_cost    = (output_tokens / 1_000_000.0) * p["output"]
    search_cost    = web_searches * WEB_SEARCH_COST_USD
    cost = input_cost + cache_cost + output_cost + search_cost

    _ensure_table(engine)
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO anthropic_usage
                    (day, calls, input_tokens, output_tokens,
                     cache_read_tokens, web_searches, cost_usd)
                VALUES
                    (CURRENT_DATE, 1, :i, :o, :cr, :w, :c)
                ON CONFLICT (day) DO UPDATE SET
                    calls          = anthropic_usage.calls + 1,
                    input_tokens   = anthropic_usage.input_tokens   + EXCLUDED.input_tokens,
                    output_tokens  = anthropic_usage.output_tokens  + EXCLUDED.output_tokens,
                    cache_read_tokens = anthropic_usage.cache_read_tokens + EXCLUDED.cache_read_tokens,
                    web_searches   = anthropic_usage.web_searches   + EXCLUDED.web_searches,
                    cost_usd       = anthropic_usage.cost_usd       + EXCLUDED.cost_usd
            """), {
                "i": int(input_tokens), "o": int(output_tokens),
                "cr": int(cache_read_tokens),
                "w": int(web_searches), "c": float(cost),
            })
    except Exception as e:
        print(f"⚠️  No se pudo grabar anthropic_usage: {e}")
    return cost


def extract_usage_from_response(resp) -> dict:
    """Extrae tokens y web_searches del response de Anthropic."""
    usage = getattr(resp, "usage", None)
    input_tokens  = getattr(usage, "input_tokens", 0) if usage else 0
    output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
    cache_read    = getattr(usage, "cache_read_input_tokens", 0) if usage else 0

    web_searches = 0
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "server_tool_use":
            # web_search calls aparecen como server_tool_use
            web_searches += 1

    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cache_read_tokens": int(cache_read or 0),
        "web_searches": int(web_searches),
    }
