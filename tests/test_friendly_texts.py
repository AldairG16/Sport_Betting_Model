"""
Textos del dashboard en palabras normales (v2.9.2, 6-nov-26).

El dueño: "necesito que el texto que usas en el dashboard sea más amigable, no
entiendo nada eso de al favorito de 65%". Casi todas las tarjetas decían "solo 🎯
al favorito de 65%+ con pago anticipado", también en el PSG de 83% o el Arsenal
de 75%: el motivo hablaba del pick que se ve (más/menos goles) y no de por qué
el favorito no era 🎯. Ahora cada pick sin 🎯 guarda star_note: lo que le falta
al favorito ("No hay favorito claro", "Liga sin pago anticipado", "PSG paga muy
poco (-650)", "a Napoli le faltan 5 titulares"), y la página lo dice así. Sin DB.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from dashboard import board as B
from dashboard import visibility as V
from src.models import program_picks as PP
from tests.test_program_parlays import _mvs, _up

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "dashboard" / "ui_es5.js").read_text(encoding="utf-8")


def _cands(mvs=None, **up):
    r = mvs or _mvs()
    return PP._candidates_of(r, SimpleNamespace(**_up(**up)))


# ─── por qué no es 🎯, dicho por el favorito ─────────────────────────────────

def test_the_note_names_what_the_favourite_is_missing():
    teams = ("Napoli", "Lecce")
    # el favorito fuerte en Serie A, a -256 (llega a su mínimo de -260): cumple todo (es 🎯) → sin nota
    assert PP.no_star_note(_cands(), "soccer_italy_serie_a", None, teams) is None
    # partido parejo: Pinnacle 2.3 / 3.3 / 3.4 → el local 42 de cada 100
    even = PP.no_star_note(_cands(pin_home_odds=2.3, pin_draw_odds=3.3, pin_away_odds=3.4),
                           "soccer_italy_serie_a", None, teams)
    assert even.startswith("No hay favorito claro:") and "Napoli, con 42 de cada 100" in even
    # el mismo favorito en la MLS: la liga no tiene pago anticipado
    mls = PP.no_star_note(_cands(), "soccer_usa_mls", None, teams)
    assert mls.startswith("Napoli es muy favorito, pero esta liga no tiene pago anticipado:") and "gana 69 de cada 100" in mls
    # más corto que -500: paga muy poco, con su momio
    short = PP.no_star_note(_cands(consensus_home_odds=1.15), "soccer_italy_serie_a", None, teams)
    assert re.match(r"Napoli paga muy poco \(-\d+\):", short)
    # de 65 a 70 con un precio que no llega al mínimo (12-nov-26): lo dice con los dos momios
    low = PP.no_star_note(_cands(consensus_home_odds=1.36), "soccer_italy_serie_a", None, teams)
    assert low.startswith("Napoli paga poco para lo que gana (-278):") and low.endswith("lo paga a -260 o mejor")
    # con 4+ titulares fuera: el motivo de la alineación
    weak = {"home_win": PP.WEAKENED.format(team="Napoli", n=5, names="a, b, c, d, e")}
    assert PP.no_star_note(_cands(), "soccer_italy_serie_a", weak, teams) == weak["home_win"]
    # el visitante muy favorito: su nombre
    away = PP.no_star_note(_cands(pin_home_odds=8.0, pin_away_odds=1.40, consensus_home_odds=7.6,
                                  consensus_away_odds=1.36), "soccer_usa_mls", None, teams)
    assert "Lecce es muy favorito" in away


def test_the_note_never_shows_a_number_that_contradicts_its_own_rule():
    """13-nov-26: Besiktas, 69.7 de cada 100, salía como "70 de cada 100" en la nota que le quita el 🎯
    por ganar menos de 70. En el corte que no alcanza, el número va con un decimal."""
    teams = ("Napoli", "Lecce")
    assert PP.shown_pct(0.687, PP.STRONG_FAV, PP.PRICE_BAND) == "69"
    assert PP.shown_pct(0.697, PP.STRONG_FAV, PP.PRICE_BAND) == "69.7"        # se leería 70 y es menos de 70
    assert PP.shown_pct(0.6996, PP.STRONG_FAV, PP.PRICE_BAND) == "69.9"       # nunca "70.0"
    assert PP.shown_pct(0.70, PP.STRONG_FAV, PP.PRICE_BAND) == "70"           # sí llega: entero
    assert PP.shown_pct(0.646, PP.STRONG_FAV, PP.PRICE_BAND) == "64.6" and PP.shown_pct(0.65, PP.STRONG_FAV) == "65"
    # Pinnacle 1.337 / 5.0 / 8.0 → 69.7; el promedio paga -333 y el mínimo es -273: sin 🎯 por precio
    low = PP.no_star_note(_cands(pin_home_odds=1.337, consensus_home_odds=1.30), "soccer_italy_serie_a", None, teams)
    assert low.startswith("Napoli paga poco para lo que gana (-333):") and "(69.7 de cada 100 según Pinnacle)" in low
    # Pinnacle 1.686 / 5.0 / 8.0 → 64.6: "65 o más" y a la vez "con 65" se contradecían
    near = PP.no_star_note(_cands(pin_home_odds=1.686, consensus_home_odds=1.62), "soccer_italy_serie_a", None, teams)
    assert near.startswith("No hay favorito claro:") and near.endswith("Napoli, con 64.6 de cada 100")


def test_without_winner_odds_the_note_says_so():
    assert PP.no_star_note([], "soccer_italy_serie_a") == PP.STAR_NO_ODDS
    # sin momios del 1X2 el favorito sale del Pinnacle de cuando se analizó: sin momio de PlayDoit
    no_1x2 = {k: None for k in ("home_odds", "draw_odds", "away_odds", "pin_home_odds", "pin_draw_odds",
                                "pin_away_odds", "consensus_home_odds", "consensus_draw_odds", "consensus_away_odds")}
    note = PP.no_star_note(_cands(**no_1x2), "soccer_italy_serie_a", None, ("Napoli", "Lecce"))
    assert note.startswith("No hay momio de PlayDoit para Napoli:")


def test_every_short_part_fits_the_card_and_has_no_percent_jargon():
    for note in (PP.STAR_NO_ODDS, PP.STAR_NO_FAV, PP.STAR_NO_PA, PP.STAR_NO_PRICE, PP.STAR_SHORT, PP.STAR_LOW_PRICE,
                 PP.NOT_STRONG):
        short = note.split(":")[0].format(team="Borussia Dortmund", pct=74, us="-650")
        assert len(short) <= 75, short
        assert "%" not in note and "65%+" not in note


# ─── se guarda con el pick y llega a la página ───────────────────────────────

def _build(mvs, **up):
    return PP.build_picks(pd.DataFrame([mvs]), pd.DataFrame([_up(**up)]))[0]


def test_build_picks_stores_the_note_only_without_target():
    star = _build(_mvs())
    assert star["reason"] is None and star["star_note"] is None                 # 🎯: nada que explicar
    mls = _build(_mvs(league="soccer_usa_mls"))
    assert mls["reason"] and mls["star_note"].startswith("Napoli es muy favorito, pero esta liga no tiene pago")
    low = _build(_mvs(), consensus_home_odds=1.36)                              # el precio no llega: pick sin 🎯
    assert low["market"] and low["reason"] and low["star_note"].startswith("Napoli paga poco para lo que gana (-278):")
    assert "star_note" in PP._COLS
    assert any("ADD COLUMN IF NOT EXISTS star_note" in a for a in PP.PICKS_ALTERS)
    # sin pick (la última corrida ya no lo evaluó): sin nota
    gone = _build(_mvs(skipped_at=pd.Timestamp("2030-01-05", tz="UTC")))
    assert gone["market"] is None and gone["star_note"] is None


def test_the_note_reaches_upcoming_and_past_rows():
    p = {"market": "over25", "label": "Más de 2.5 goles", "family": "Goles", "model_prob": 0.6,
         "market_prob": 0.55, "playdoit_odds": 1.8, "reason": PP.NOT_STRONG, "lineup": None,
         "star_note": "No hay favorito claro: x"}
    assert V.saved_pick(p)["star_note"] == "No hay favorito claro: x"
    assert V.saved_pick({**p, "star_note": None})["star_note"] == ""          # picks de antes: sin nota
    info = B._pick_info({**p, "match_date": "2026-11-07T15:00:00Z", "result": None, "profit": None, "source": None})
    assert info["star_note"] == "No hay favorito claro: x" and info["rule"] is False


# ─── la página: lo que se lee en cada tarjeta ────────────────────────────────

def _js_functions(*names) -> str:
    out = []
    for n in names:
        m = re.search(r"^function " + n + r"\(.*?^}\n", JS, re.S | re.M)
        assert m, n
        out.append(m.group(0))
    return "\n".join(out)


def _run_js(body: str):
    if not shutil.which("node"):
        pytest.skip("sin node")
    code = _js_functions("esc", "p100", "noteShort", "shortWhy", "whyTag", "lineupTag", "pxOdds") + body
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_cards_say_why_there_is_no_target_in_plain_words():
    got = _run_js("""
      var w = function(pk){ return whyTag(pk).replace(/<[^>]+>/g, ''); };
      console.log(JSON.stringify([
        w({rule: false, reason: 'x', star_note: 'No hay favorito claro: el 🎯 es solo para...'}),
        w({rule: false, reason: 'x', star_note: 'Paris Saint Germain paga muy poco (-650): es muy favorito'}),
        w({rule: false, reason: 'x', star_note: 'Rosenborg es muy favorito, pero esta liga no tiene pago anticipado: gana 78'}),
        w({rule: false, reason: 'Solo 🎯 al favorito de 65%+ con pago anticipado', star_note: ''}),
        w({rule: false, reason: 'El programa se separa demasiado del mercado: probablemente se equivoca'}),
        w({rule: false, reason: 'Alineación: a Napoli le faltan 5 titulares habituales (a, b)'}),
        w({rule: false, reason: 'Región América en pausa: sus 🎯 pierden'}),
        w({rule: false, reason: 'Poca historia para apostar: x', star_note: 'No hay favorito claro: y'}),
        w({rule: true, reason: '', star_note: ''}),
        lineupTag({label: 'x'}).replace(/<[^>]+>/g, ''),
        pxOdds(0.14), pxOdds(0.35)
      ]));""")
    assert got[0] == "sin 🎯: no hay favorito claro"
    assert got[1] == "sin 🎯: Paris Saint Germain paga muy poco (-650)"
    assert got[2] == "sin 🎯: Rosenborg es muy favorito, pero esta liga no tiene pago anticipado"
    assert got[3] == "sin 🎯: no era el muy favorito"                       # picks de antes de v2.9.2
    assert got[4] == "sin 🎯: el programa no coincide con las casas"
    assert got[5] == "sin 🎯: a Napoli le faltan 5 titulares"
    assert got[6] == "sin 🎯: en América el programa viene perdiendo"
    assert got[7] == "sin 🎯: no hay favorito claro · ⚠️ pocos partidos de estos equipos"
    assert got[8] == ""                                                        # 🎯: nada que explicar
    assert got[9] == "⏳ falta la alineación"
    assert got[10] == "más o menos 1 de cada 7 veces" and got[11] == "más o menos 1 de cada 3 veces"


def test_singular_and_plural_read_right():
    # se veía "Hoy: 1 partidos" y "⏳ falta las alineaciones de todos" (6-nov-26)
    if not shutil.which("node"):
        pytest.skip("sin node")
    code = _js_functions("pxReady") + """
      function mxtime(){ return '08:25'; }
      var t = function(k){ return pxReady({kind: k, ready: false, lock: 'x', parlay: {status: 'pending'}}).replace(/<[^>]+>/g, ''); };
      console.log(JSON.stringify([t('dia'), t('horario')]));"""
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got[0].startswith("⏳ falta la alineación del primer partido (sale ~1 h antes)")
    assert got[1].startswith("⏳ faltan las alineaciones de todos (salen ~1 h antes)")
    assert "(n === 1 ? ' partido' : ' partidos')" in JS


def test_the_old_jargon_is_gone_from_the_page():
    from dashboard.app import PAGE
    page = PAGE + re.sub(r"(^|\s)//.*$", "", JS, flags=re.M)          # lo que se ve, sin comentarios
    for jargon in ("favorito de 65%+", "favoritos de ' + p100", "muy lejos del mercado", "Pasan la regla",
                   "pasa la regla", "programa (arriba)", "mercado sin margen", "Rendimiento (ROI)",
                   "liquidados", "salen todas"):
        assert jargon not in page, jargon
    # el encabezado cuenta los 🎯 de hoy, no todos los picks (decía "🎯 Hoy: 6 picks" con cero 🎯)
    assert "pick.label && _up[j].pick.rule) good++" in JS
    assert "🎯 = <b>apuesta recomendada</b>" in PAGE
