"""
Por liga (v2.10.0, 7-nov-26).

El dueño: "necesito que me des un resumen de los partidos que tenemos hasta
hoy, pero me lo ordenes por ligas, dime cuántas se ganaron y cuántas se
perdieron, y necesito que eso también me lo muestres en el dashboard". La
pestaña "Por liga": cada liga en orden alfabético con sus ganados y perdidos,
la ganancia apostando 100 a cada uno y, al tocarla, sus partidos. Sin DB.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dashboard import board as B
from tests.test_board import _row

JS = (Path(__file__).resolve().parent.parent / "dashboard" / "ui_es5.js").read_text(encoding="utf-8")


def _rows():
    return [_row("2026-10-24", "Serie A", "A", "B", "Gana el local", "win", 0.9, 0.55),
            _row("2026-10-26", "Serie A", "C", "D", "Más de 2.5 goles", "loss", -1.0, 0.6, rule=False),
            _row("2026-10-25", "Serie A", "E", "F", "Ambos anotan", "push", 0.0, 0.5, rule=False),
            _row("2026-10-25", "Súper Lig (Turquía)", "G", "H", "Gana el local", "win", None, 0.7),  # sin momio
            _row("2026-10-25", "Super League (Grecia)", "I", "J", "Gana el local", "loss", -1.0, 0.7),
            _row("2026-10-25", "Allsvenskan", "K", "L", "Gana el local", "loss", -1.0, 0.6, low=True),  # poca historia
            _row("2026-10-20", "Allsvenskan", "M", "N", "Gana el local", "win", 0.8, 0.6, old=True),    # modelo con error
            _row("2026-10-27", "Allsvenskan", "O", "P", "Gana el local", "win", 0.7, 0.6, started=True)]  # provisional


def test_each_league_in_alphabetical_order_with_its_wins_losses_and_matches():
    out = B.league_board(_rows())
    assert out["since"] == "2026-10-22" and out["min_n"] == B.RESULTS_MIN_N
    g = out["groups"]["picks"]
    # sin los de poca historia, los del modelo viejo ni los provisionales; los acentos no mandan al final
    assert [lg["name"] for lg in g["leagues"]] == ["Serie A", "Super League (Grecia)", "Súper Lig (Turquía)"]
    serie_a = g["leagues"][0]
    assert {k: serie_a[k] for k in ("n", "won", "lost", "push", "units", "priced", "few")} == \
        {"n": 3, "won": 1, "lost": 1, "push": 1, "units": -0.1, "priced": 3, "few": True}
    # sus partidos, el más reciente primero, con el pick, el resultado y si era 🎯
    assert [(m["date"], m["match"], m["type"], m["result"], m["rule"]) for m in serie_a["matches"]] == [
        ("2026-10-26", "C vs D", "Más de 2.5 goles", "loss", False),
        ("2026-10-25", "E vs F", "Ambos anotan", "push", False),
        ("2026-10-24", "A vs B", "Gana el local", "win", True)]
    turkey = g["leagues"][2]
    assert turkey["won"] == 1 and turkey["priced"] == 0 and turkey["units"] == 0     # sin momio: cuenta, sin ganancia
    assert {k: g["total"][k] for k in ("n", "won", "lost", "push", "priced")} == \
        {"n": 5, "won": 2, "lost": 2, "push": 1, "priced": 4}
    # solo los 🎯
    rule = out["groups"]["rule"]
    assert rule["total"]["n"] == 3 and [lg["name"] for lg in rule["leagues"]] == \
        ["Serie A", "Super League (Grecia)", "Súper Lig (Turquía)"]
    assert [m["match"] for m in rule["leagues"][0]["matches"]] == ["A vs B"]


def test_the_same_picks_as_ganadas_y_perdidas():
    rows = _rows()
    res = B.results_board(rows)["groups"]["picks"]
    lg = B.league_board(rows)["groups"]["picks"]
    assert {k: lg["total"][k] for k in res["total"]} == res["total"]
    assert sorted((x["name"], x["won"], x["lost"]) for x in res["by_league"]) == \
        sorted((x["name"], x["won"], x["lost"]) for x in lg["leagues"])


def _block():
    a = JS.index("// ── Por liga (v2.10.0")
    b = JS.index("function renderLeagues")
    one = lambda n: re.search(r"^function " + n + r"\(.*$", JS, re.M).group(0)
    return ("function _storeGet(k, d){ return d; }\n" + one("esc") + "\n" + one("shortDate") + "\n" +
            re.search(r"^var MESES = .*$", JS, re.M).group(0) + "\n" + JS[a:b])


def test_the_table_says_it_in_plain_words():
    if not shutil.which("node"):
        pytest.skip("sin node")
    code = _block() + """
      var txt = function(s){ return s.replace(/<[^>]+>/g, ''); };
      var lg = {name: 'Serie A', n: 4, won: 2, lost: 1, push: 1, hit: 0.667, units: 0.64, priced: 3, few: true};
      var list = [{name: 'A', won: 1, n: 5, hit: null}, {name: 'B', won: 3, n: 4, hit: 0.5}, {name: 'C', won: 3, n: 3, hit: 0.7}];
      console.log(JSON.stringify([
        cien(0.64), cien(-1), cien(0), cien(-0.004), cien(null),
        txt(lgRow(lg, false, 5)), txt(lgRow(lg, true, 5)),
        lgRow(lg, false, 5).indexOf('Pocos partidos: menos de 5') >= 0,
        txt(lgMatch({date: '2026-10-24', match: 'A vs B', type: 'Gana el local', result: 'win', profit: 0.9, rule: true}, true)),
        txt(lgMatch({date: '2026-10-26', match: 'C vs D', type: 'Más de 2.5 goles', result: 'loss', profit: -1, rule: false}, true)),
        txt(lgMatch({date: '2026-10-25', match: 'E vs F', type: 'Gana el local', result: 'push', profit: null, rule: true}, false)),
        lgSorted(list, 'won').map(function(x){ return x.name; }),
        lgSorted(list, 'hit').map(function(x){ return x.name; }),
        lgSorted(list, 'name').map(function(x){ return x.name; })
      ]));"""
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got[:5] == ["+64", "-100", "0", "0", "0"]
    assert got[5] == "▸ Serie A4 (1 anulado)2167+64 *" and got[6].startswith("▾ Serie A")
    assert got[7] is True
    assert got[8] == "✅ 24 oct · A vs B · 🎯 Gana el local · +90"
    assert got[9] == "❌ 26 oct · C vs D · Más de 2.5 goles · -100"
    assert got[10] == "↔️ 25 oct · E vs F · Gana el local · —"          # sin 🎯 en la vista de solo recomendados
    assert got[11] == ["B", "C", "A"] and got[12] == ["C", "B", "A"] and got[13] == ["A", "B", "C"]
    for s in got[5:7] + got[8:11]:
        assert not re.search(r"\d+(\.\d+)?u\b", s) and "ROI" not in s and "unidad" not in s
