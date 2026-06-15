"""
Tests de integración del flujo de notificaciones.
Datos sintéticos — sin DB, sin HTTP real.

Estos tests habrían atrapado los dos bugs del Mundial (jun-2026):
  1. _read_paper_trades_for_date() filtraba por fecha exacta → bets
     para partidos futuros nunca aparecían en la notificación del día.
  2. send_tomorrow_preview() no tenía sección paper → picks del Mundial
     no aparecían en el preview nocturno aunque estuvieran generados.
"""

import json
import pytest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _trade(match_date_utc: datetime, match: str = "Team A vs Team B",
           market: str = "home_win", league: str = "soccer_fifa_world_cup",
           edge: float = 0.07, odds: float = 2.1, stake: float = 1.5) -> dict:
    return {
        "logged_at":   datetime.now(timezone.utc).isoformat(),
        "match":       match,
        "match_date":  match_date_utc.isoformat(),
        "league":      league,
        "market":      market,
        "probability": 0.55,
        "odds":        odds,
        "edge":        edge,
        "stake":       stake,
    }


def _write_jsonl(path: Path, trades: list):
    with open(path, "w", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")


def _dt(y, m, d, h=19) -> datetime:
    return datetime(y, m, d, h, 0, tzinfo=timezone.utc)


TODAY      = date(2026, 6, 15)
TODAY_DT   = _dt(2026, 6, 15)
TMRW_DT    = _dt(2026, 6, 16)
DAY2_DT    = _dt(2026, 6, 17)
DAY3_DT    = _dt(2026, 6, 18)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _read_paper_trades_for_date
# ─────────────────────────────────────────────────────────────────────────────

class TestReadPaperTradesForDate:

    def test_days_ahead_0_returns_only_exact_date(self, tmp_path, monkeypatch):
        """days_ahead=0 → solo el día exacto."""
        import scripts.notify_telegram as nt
        log = tmp_path / "paper_trades.jsonl"
        _write_jsonl(log, [
            _trade(TODAY_DT, match="Today"),
            _trade(TMRW_DT,  match="Tomorrow"),
        ])
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)
        result = nt._read_paper_trades_for_date(TODAY, days_ahead=0)
        assert len(result) == 1
        assert result[0]["match"] == "Today"

    def test_days_ahead_2_covers_three_day_window(self, tmp_path, monkeypatch):
        """days_ahead=2 → hoy + mañana + pasado mañana (ventana del pipeline)."""
        import scripts.notify_telegram as nt
        log = tmp_path / "paper_trades.jsonl"
        _write_jsonl(log, [
            _trade(TODAY_DT, match="Today"),
            _trade(TMRW_DT,  match="Tomorrow"),
            _trade(DAY2_DT,  match="Day2"),
            _trade(DAY3_DT,  match="Day3"),  # fuera del rango
        ])
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)
        result = nt._read_paper_trades_for_date(TODAY, days_ahead=2)
        matches = [r["match"] for r in result]
        assert "Today"    in matches
        assert "Tomorrow" in matches
        assert "Day2"     in matches
        assert "Day3" not in matches

    def test_empty_file_returns_empty_list(self, tmp_path, monkeypatch):
        import scripts.notify_telegram as nt
        log = tmp_path / "paper_trades.jsonl"
        log.write_text("", encoding="utf-8")
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)
        assert nt._read_paper_trades_for_date(TODAY, days_ahead=2) == []

    def test_missing_file_returns_empty_list(self, monkeypatch):
        import scripts.notify_telegram as nt
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", Path("/nonexistent/paper_trades.jsonl"))
        assert nt._read_paper_trades_for_date(TODAY, days_ahead=2) == []

    def test_dedup_same_match_market_across_reruns(self, tmp_path, monkeypatch):
        """Reruns del pipeline no duplican el mismo pick en el mensaje."""
        import scripts.notify_telegram as nt
        log = tmp_path / "paper_trades.jsonl"
        # Mismo match+market escrito 3 veces (3 reruns del morning)
        _write_jsonl(log, [_trade(TODAY_DT, market="home_win")] * 3)
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)
        result = nt._read_paper_trades_for_date(TODAY, days_ahead=0)
        assert len(result) == 1

    def test_sorted_by_date_then_edge_desc(self, tmp_path, monkeypatch):
        """Resultados ordenados: match_date ASC, edge DESC."""
        import scripts.notify_telegram as nt
        log = tmp_path / "paper_trades.jsonl"
        _write_jsonl(log, [
            _trade(TMRW_DT, match="B vs C", market="over25",   edge=0.05),
            _trade(TODAY_DT, match="A vs B", market="home_win", edge=0.12),
            _trade(TODAY_DT, match="A vs B", market="over25",   edge=0.08),
        ])
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)
        result = nt._read_paper_trades_for_date(TODAY, days_ahead=2)
        assert result[0]["edge"] == 0.12   # today, highest edge first
        assert result[1]["edge"] == 0.08
        assert result[2]["match"] == "B vs C"   # tomorrow last


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _format_paper_bets_section
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatPaperBetsSection:

    def test_returns_empty_string_when_no_trades(self, monkeypatch):
        import scripts.notify_telegram as nt
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", Path("/nonexistent/paper_trades.jsonl"))
        assert nt._format_paper_bets_section(TODAY, days_ahead=2) == ""

    def test_returns_nonempty_string_when_trades_exist(self, tmp_path, monkeypatch):
        import scripts.notify_telegram as nt
        log = tmp_path / "paper_trades.jsonl"
        _write_jsonl(log, [_trade(TODAY_DT)])
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)
        section = nt._format_paper_bets_section(TODAY, days_ahead=0)
        assert section != ""
        assert "PAPER" in section

    def test_section_contains_match_name(self, tmp_path, monkeypatch):
        import scripts.notify_telegram as nt
        log = tmp_path / "paper_trades.jsonl"
        _write_jsonl(log, [_trade(TODAY_DT, match="Brazil vs Argentina")])
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)
        section = nt._format_paper_bets_section(TODAY, days_ahead=0)
        assert "Brazil vs Argentina" in section

    def test_future_bets_appear_with_days_ahead(self, tmp_path, monkeypatch):
        """El bug del Mundial: bets de mañana deben aparecer con days_ahead=2."""
        import scripts.notify_telegram as nt
        log = tmp_path / "paper_trades.jsonl"
        _write_jsonl(log, [_trade(TMRW_DT, match="Uzbekistan vs Colombia")])
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)

        # Sin days_ahead → no aparece
        section_no = nt._format_paper_bets_section(TODAY, days_ahead=0)
        assert "Uzbekistan" not in section_no

        # Con days_ahead=2 → sí aparece
        section_yes = nt._format_paper_bets_section(TODAY, days_ahead=2)
        assert "Uzbekistan" in section_yes


# ─────────────────────────────────────────────────────────────────────────────
# Tests: send_tomorrow_preview incluye sección paper
# ─────────────────────────────────────────────────────────────────────────────

class TestSendTomorrowPreviewIncludesPaper:
    """
    Segundo bug del Mundial: send_tomorrow_preview() no tenía sección paper,
    así el preview nocturno siempre enviaba 'sin picks' aunque hubiera bets WC.
    """

    def test_preview_includes_paper_section_when_no_real_bets(self, tmp_path, monkeypatch):
        import scripts.notify_telegram as nt
        import pandas as pd

        TOMORROW = date(2026, 6, 16)
        # Paper trade para mañana (mismo día que _local_tomorrow devolverá)
        log = tmp_path / "paper_trades.jsonl"
        _write_jsonl(log, [_trade(TMRW_DT, match="France vs Senegal")])  # TMRW_DT = jun 16
        monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)

        # _local_tomorrow devuelve jun 16 para que _format_paper_bets_section lo encuentre
        monkeypatch.setattr(nt, "_local_tomorrow", lambda: TOMORROW)
        empty_df = pd.DataFrame(columns=["match", "match_date", "market",
                                          "probability", "odds", "edge", "stake", "league"])
        monkeypatch.setattr(pd, "read_sql", lambda *a, **kw: empty_df)

        sent_messages = []
        monkeypatch.setattr(nt, "send_message", lambda msg: sent_messages.append(msg) or True)
        monkeypatch.setattr(nt, "_set_last_picks_shown", lambda n: None)

        nt.send_tomorrow_preview()

        assert len(sent_messages) == 1
        assert "France vs Senegal" in sent_messages[0], (
            "El preview debe incluir paper bets del Mundial aunque no haya bets reales"
        )
        assert "PAPER" in sent_messages[0]

    def test_preview_sends_no_picks_message_when_truly_empty(self, tmp_path, monkeypatch):
        import scripts.notify_telegram as nt
        import pandas as pd

        monkeypatch.setattr(nt, "PAPER_LOG_PATH", Path("/nonexistent/paper_trades.jsonl"))
        monkeypatch.setattr(nt, "_local_tomorrow", lambda: TODAY)
        empty_df = pd.DataFrame(columns=["match", "match_date", "market",
                                          "probability", "odds", "edge", "stake", "league"])
        monkeypatch.setattr(pd, "read_sql", lambda *a, **kw: empty_df)

        sent_messages = []
        monkeypatch.setattr(nt, "send_message", lambda msg: sent_messages.append(msg) or True)
        monkeypatch.setattr(nt, "_set_last_picks_shown", lambda n: None)

        nt.send_tomorrow_preview()

        assert len(sent_messages) == 1
        assert "sin value bets" in sent_messages[0].lower()
        assert "PAPER" not in sent_messages[0]
