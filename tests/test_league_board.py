"""
Por liga (v2.10.0, 7-nov-26).

El dueño: "necesito que me des un resumen de los partidos que tenemos hasta
hoy, pero me lo ordenes por ligas, dime cuántas se ganaron y cuántas se
perdieron, y necesito que eso también me lo muestres en el dashboard", y luego:
"solamente quiero ver los partidos de hoy". La pestaña "Por liga" abre en Hoy
(los terminados, aunque se cobren en la noche, y los que faltan) y tiene
"Desde el 22 oct" (todo lo cobrado): cada liga en orden alfabético con sus
ganados y perdidos, la ganancia apostando 100 a cada uno y, al tocarla, sus
partidos. Sin DB.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from dashboard import board as B
from tests.test_board import _row

JS = (Path(__file__).resolve().parent.parent / "dashboard" / "ui_es5.js").read_text(encoding="utf-8")
NOW = pd.Timestamp("2026-11-08 03:30", tz="UTC")          # sábado 7 nov, 21:30 en México


def _rows():
    return [_row("2026-10-24", "Serie A", "A", "B", "Gana el local", "win", 0.9, 0.55),
            _row("2026-10-26", "Serie A", "C", "D", "Más de 2.5 goles", "loss", -1.0, 0.6, rule=False),
            _row("2026-10-25", "Serie A", "E", "F", "Ambos anotan", "push", 0.0, 0.5, rule=False),
            _row("2026-10-25", "Súper Lig (Turquía)", "G", "H", "Gana el local", "win", None, 0.7),  # sin momio
            _row("2026-10-25", "Super League (Grecia)", "I", "J", "Gana el local", "loss", -1.0, 0.7),
            _row("2026-10-25", "Allsvenskan", "K", "L", "Gana el local", "loss", -1.0, 0.6, low=True),  # poca historia
            _row("2026-10-20", "Allsvenskan", "M", "N", "Gana el local", "win", 0.8, 0.6, old=True),    # modelo con error
            _row("2026-10-27", "Allsvenskan", "O", "P", "Gana el local", "win", 0.7, 0.6, started=True)]  # provisional


def _today_rows():
    """Sábado 7 nov: cobrados, uno terminado sin cobrar (córners), uno en vivo,
    uno esperando el resultado, uno de poca historia y uno de ayer."""
    prov = _row("2026-11-07", "Liga MX", "Tigres", "Cruz Azul", "Más de 9.5 córners", "win", 0.64, 0.5, rule=False)
    prov["pick"]["provisional"] = True
    live = _row("2026-11-07", "Liga MX", "Pumas", "América", "Gana el visitante", "pending", None, 0.5,
                rule=False, started=True)
    live["live"] = {"status": "live", "clock": "63'"}
    wait = _row("2026-11-07", "Primera División (Argentina)", "Central Córdoba", "Lanús", "Menos de 2.5 goles",
                "pending", None, 0.5, rule=False, started=True)
    return [_row("2026-11-07", "Bundesliga", "Leipzig", "Augsburg", "Gana el local", "win", 0.403, 0.75),
            _row("2026-11-07", "Bundesliga", "Stuttgart", "Werder Bremen", "Más de 3.5 goles", "loss", -1.0, 0.5,
                 rule=False),
            prov, live, wait,
            _row("2026-11-07", "Pro League (Bélgica)", "Antwerp", "Lommel", "Más de 2.5 goles", "win", 0.582, 0.5,
                 rule=False, low=True),
            _row("2026-11-06", "Ligue 1", "Toulouse", "Strasbourg", "Gana el local", "win", 0.5, 0.6)]


UPCOMING = [{"match": "FC Seoul vs Ulsan", "league": "K League 1", "type": "Gana el local", "rule": False,
             "time": "23:00"}]


def test_since_each_league_in_alphabetical_order_with_its_wins_losses_and_matches():
    out = B.league_board(_rows(), now=NOW)
    assert out["since"] == "2026-10-22" and out["today"] == "2026-11-07" and out["min_n"] == B.RESULTS_MIN_N
    g = out["periods"]["since"]["picks"]
    # sin los de poca historia, los del modelo viejo ni los provisionales; los acentos no mandan al final
    assert [lg["name"] for lg in g["leagues"]] == ["Serie A", "Super League (Grecia)", "Súper Lig (Turquía)"]
    serie_a = g["leagues"][0]
    assert {k: serie_a[k] for k in ("n", "won", "lost", "push", "pending", "units", "priced", "few")} == \
        {"n": 3, "won": 1, "lost": 1, "push": 1, "pending": 0, "units": -0.1, "priced": 3, "few": True}
    # sus partidos, el más reciente primero, con el pick, el resultado y si era 🎯
    assert [(m["date"], m["match"], m["type"], m["result"], m["rule"]) for m in serie_a["matches"]] == [
        ("2026-10-26", "C vs D", "Más de 2.5 goles", "loss", False),
        ("2026-10-25", "E vs F", "Ambos anotan", "push", False),
        ("2026-10-24", "A vs B", "Gana el local", "win", True)]
    turkey = g["leagues"][2]
    assert turkey["won"] == 1 and turkey["priced"] == 0 and turkey["units"] == 0     # sin momio: cuenta, sin ganancia
    assert {k: g["total"][k] for k in ("n", "won", "lost", "push", "priced")} == \
        {"n": 5, "won": 2, "lost": 2, "push": 1, "priced": 4}
    rule = out["periods"]["since"]["rule"]                                            # solo los 🎯
    assert rule["total"]["n"] == 3 and [m["match"] for m in rule["leagues"][0]["matches"]] == ["A vs B"]


def test_since_is_the_same_as_ganadas_y_perdidas():
    rows = _rows()
    res = B.results_board(rows)["groups"]["picks"]
    lg = B.league_board(rows, now=NOW)["periods"]["since"]["picks"]
    assert {k: lg["total"][k] for k in res["total"]} == res["total"]
    assert sorted((x["name"], x["won"], x["lost"]) for x in res["by_league"]) == \
        sorted((x["name"], x["won"], x["lost"]) for x in lg["leagues"])


def test_today_has_every_match_of_the_day_finished_or_not():
    out = B.league_board(_today_rows() + _rows(), now=NOW, upcoming=UPCOMING)
    g = out["periods"]["today"]["picks"]
    # todos los de hoy, también el de poca historia y el que no empieza; el de ayer no
    assert [lg["name"] for lg in g["leagues"]] == ["Bundesliga", "K League 1", "Liga MX",
                                                   "Primera División (Argentina)", "Pro League (Bélgica)"]
    assert {k: g["total"][k] for k in ("n", "won", "lost", "pending", "priced")} == \
        {"n": 7, "won": 3, "lost": 1, "pending": 3, "priced": 4}
    assert g["total"]["units"] == round(0.403 - 1 + 0.64 + 0.582, 2)
    mx = g["leagues"][2]
    assert {k: mx[k] for k in ("n", "won", "lost", "pending")} == {"n": 2, "won": 1, "lost": 0, "pending": 1}
    # el que se está jugando primero, luego los terminados; el terminado sin cobrar sale "por confirmar"
    assert [(m["match"], m["status"], m["clock"], m["provisional"]) for m in mx["matches"]] == [
        ("Pumas vs América", "live", "63'", False), ("Tigres vs Cruz Azul", "done", "", True)]
    assert g["leagues"][3]["matches"][0]["status"] == "waiting"
    assert g["leagues"][1]["matches"][0] == {"date": "2026-11-07", "match": "FC Seoul vs Ulsan", "type": "Gana el local",
                                             "result": None, "profit": None, "rule": False, "status": "upcoming",
                                             "provisional": False, "clock": "", "time": "23:00"}
    rule = out["periods"]["today"]["rule"]
    assert rule["total"]["n"] == 1 and rule["total"]["won"] == 1 and rule["leagues"][0]["name"] == "Bundesliga"
    # el día es el de México: a las 23:30 del sábado en México (05:30 UTC del domingo) sigue siendo sábado
    assert B.league_board([], now=pd.Timestamp("2026-11-08 05:30", tz="UTC"))["today"] == "2026-11-07"
    assert B.league_board([], now=pd.Timestamp("2026-11-08 06:30", tz="UTC"))["today"] == "2026-11-08"


def _block():
    a = JS.index("// ── Por liga (v2.10.0")
    b = JS.index("function renderLeagues")
    one = lambda n: re.search(r"^function " + n + r"\(.*$", JS, re.M).group(0)
    return ("function _storeGet(k, d){ return d; }\n" + one("esc") + "\n" + one("shortDate") + "\n" +
            re.search(r"^var MESES = .*$", JS, re.M).group(0) + "\n" +
            re.search(r"^var DIAS = .*$", JS, re.M).group(0) + "\n" + JS[a:b])


def test_the_table_says_it_in_plain_words():
    if not shutil.which("node"):
        pytest.skip("sin node")
    code = _block() + """
      var txt = function(s){ return s.replace(/<[^>]+>/g, ''); };
      var lg = {name: 'Serie A', n: 4, won: 2, lost: 1, push: 1, pending: 0, hit: 0.667, units: 0.64, priced: 3, few: true};
      var day = {name: 'Liga MX', n: 4, won: 1, lost: 1, push: 0, pending: 2, hit: 0.5, units: -0.36, priced: 2, few: true};
      var list = [{name: 'A', won: 1, n: 5, hit: null}, {name: 'B', won: 3, n: 4, hit: 0.5}, {name: 'C', won: 3, n: 3, hit: 0.7}];
      var m = function(o){ var b = {date: '2026-11-07', match: 'A vs B', type: 'Gana el local', result: null, profit: null,
                                    rule: true, status: 'done', provisional: false, clock: '', time: ''};
                           for (var k in o) b[k] = o[k]; return b; };
      console.log(JSON.stringify([
        cien(0.64), cien(-1), cien(0), cien(-0.004), cien(null),
        txt(lgRow(lg, false, 5, false)), txt(lgRow(lg, true, 5, false)),
        lgRow(lg, false, 5, false).indexOf('Pocos partidos: menos de 5') >= 0,
        txt(lgMatch(m({date: '2026-10-24', result: 'win', profit: 0.9}), true, false)),
        txt(lgMatch(m({date: '2026-10-26', match: 'C vs D', type: 'Más de 2.5 goles', result: 'loss', profit: -1, rule: false}), true, false)),
        txt(lgMatch(m({date: '2026-10-25', match: 'E vs F', result: 'push', profit: null}), false, false)),
        lgSorted(list, 'won').map(function(x){ return x.name; }),
        lgSorted(list, 'hit').map(function(x){ return x.name; }),
        lgSorted(list, 'name').map(function(x){ return x.name; }),
        txt(lgRow(day, false, 5, true)), lgRow(day, false, 5, true).indexOf('Pocos partidos') >= 0,
        txt(lgMatch(m({match: 'Tigres vs Cruz Azul', type: 'Más de 9.5 córners', result: 'win', profit: 0.64,
                        rule: false, provisional: true}), true, true, 7)),
        txt(lgMatch(m({match: 'Pumas vs América', type: 'Gana el visitante', status: 'live', clock: "63'", rule: false}), true, true, 7)),
        txt(lgMatch(m({match: 'Central Córdoba vs Lanús', type: 'Menos de 2.5 goles', status: 'waiting', rule: false}), true, true, 7)),
        txt(lgMatch(m({match: 'FC Seoul vs Ulsan', status: 'upcoming', time: '23:00', rule: false}), true, true, 7)),
        txt(lgMatch(m({result: 'win', profit: 0.403}), true, true, 7)),
        lgCols(true).map(function(c){ return txt(c[1]); }), lgCols(false).length, longDay('2026-11-07'),
        lgCols(true)[2][1]
      ]));"""
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got[:5] == ["+64", "-100", "0", "0", "0"]
    # desde el 22 oct
    assert got[5] == "▸ Serie A4 (1 anulado)2167+64 *" and got[6].startswith("▾ Serie A")
    assert got[7] is True
    assert got[8] == "✅ 24 oct · A vs B · 🎯 Gana el local · +90"
    assert got[9] == "❌ 26 oct · C vs D · Más de 2.5 goles · -100"
    assert got[10] == "↔️ 25 oct · E vs F · Gana el local · —"          # sin 🎯 en la vista de solo recomendados
    assert got[11] == ["B", "C", "A"] and got[12] == ["C", "B", "A"] and got[13] == ["A", "B", "C"]
    # hoy: la columna de los que faltan, sin "pocos partidos"
    assert got[14] == "▸ Liga MX411250-36" and got[15] is False
    assert got[16] == "✅ Tigres vs Cruz Azul · Más de 9.5 córners · +64 · por confirmar"
    assert got[17] == "● Pumas vs América · Gana el visitante · en vivo 63'"
    assert got[18] == "⏳ Central Córdoba vs Lanús · Menos de 2.5 goles · esperando el resultado"
    assert got[19] == "🕒 23:00 FC Seoul vs Ulsan · Gana el local · todavía no empieza"
    assert got[20] == "✅ A vs B · 🎯 Gana el local · +40"
    assert got[21] == ["Liga", "Partidos", "✅ Ganados", "❌ Perdidos", "⏳ Faltan", "Ganados de cada 100",
                       "Apostando 100 a cada uno"] and got[22] == 6
    assert got[23] == "sábado 7 nov"
    assert got[24] == '✅<span class="hide-sm"> Ganados</span>'               # en el celular, solo el ícono
    for s in [x for x in got if isinstance(x, str) and len(x) > 6]:
        assert not re.search(r"\d+(\.\d+)?u\b", s) and "ROI" not in s and "unidad" not in s
