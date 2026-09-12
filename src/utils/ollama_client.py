"""
src/utils/ollama_client.py
==========================
Cliente mínimo para el Ollama local (http://localhost:11434).

Uso en el sistema: ANALISTA REVISOR — lee el contexto estructurado de cada
bet (forma, xG, H2H, fatiga, movimiento de línea) y devuelve una explicación
de una línea + banderas de incoherencia.

REGLA DE HIERRO: su salida es INFORMATIVA. Nunca modifica probabilidades,
edges ni stakes — el modelo estadístico calibrado manda. (Un LLM genérico
no predice fútbol mejor que Dixon-Coles; sí sirve para leer/redactar.)

Diseño defensivo: si Ollama no corre, no responde o no hay modelo de chat,
todo devuelve None y el pipeline sigue igual. Timeout corto para no
bloquear el morning.
"""

import json
import urllib.request

OLLAMA_BASE = "http://localhost:11434"
# Modelo de chat recomendado (descargar con: ollama pull qwen2.5:7b)
CHAT_MODEL = "qwen2.5:7b"
TIMEOUT_S = 20

_available_cache: bool | None = None


def ollama_available() -> bool:
    """True si Ollama responde y tiene al menos un modelo de chat."""
    global _available_cache
    if _available_cache is not None:
        return _available_cache
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/tags",
                                     headers={"User-Agent": "betting-model"})
        with urllib.request.urlopen(req, timeout=3) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        _available_cache = any(CHAT_MODEL.split(":")[0] in m for m in models)
    except Exception:
        _available_cache = False
    return _available_cache


def reset_cache():
    global _available_cache
    _available_cache = None


PROMPT = """Eres un analista deportivo revisando una apuesta generada por un modelo estadístico.
Responde SOLO con JSON válido: {"explicacion": "...", "riesgo": "..."} donde
"explicacion" es UNA línea (máx 25 palabras) de por qué la apuesta tiene sentido,
y "riesgo" es UNA línea con la mayor incoherencia que detectes, o "none" si no hay.

Datos de la apuesta:
{context}
"""


def review_bet(context: dict) -> dict | None:
    """
    Pide al modelo local una revisión de la apuesta.

    context: dict plano con las señales (teams, market, prob, edge, odds,
             form, xg, h2h, fatigue, line_movement, books...)

    Returns: {"explicacion": str, "riesgo": str} o None si Ollama no está.
    """
    if not ollama_available():
        return None
    try:
        body = json.dumps({
            "model": CHAT_MODEL,
            "prompt": PROMPT.format(context=json.dumps(context, ensure_ascii=False, default=str)),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 120},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/generate", data=body,
            headers={"Content-Type": "application/json", "User-Agent": "betting-model"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read())
        out = json.loads(data.get("response", "{}"))
        if "explicacion" in out:
            return {
                "explicacion": str(out.get("explicacion", ""))[:300],
                "riesgo": str(out.get("riesgo", "none"))[:300],
            }
    except Exception:
        return None
    return None
