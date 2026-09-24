"""
Tests del formato de confirmaciones pre-kickoff (flujo de dos fases).
Verifica el mensaje puro sin tocar DB ni Telegram.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.revalidate_pending_bets import _format_confirmations


ROWS = [
    {
        "match": "tottenham vs everton",
        "market": "over25",
        "odds": 2.06,
        "stake": 0.16,
        "edge": 0.096,
        "match_date": "2026-09-15T19:30:00+00:00",
    },
    {
        "match": "ath bilbao vs elche",
        "market": "cards_under_4.5",
        "odds": 1.85,
        "stake": 0.06,
        "edge": 0.071,
        "match_date": "2026-09-15T21:00:00+00:00",
    },
]


class TestFormatConfirmations:

    def test_contains_bets_and_header(self):
        msg = _format_confirmations(ROWS)
        assert "APUESTAS CONFIRMADAS" in msg
        assert "Tottenham vs Everton" in msg  # match_name capitaliza
        assert "+106" in msg and "2.06" not in msg   # americano, como PlayDoit
        assert "0.16u" in msg
        assert "Valor final" in msg

    def test_market_labels_formal(self):
        msg = _format_confirmations(ROWS)
        assert "Más 2.5 goles" in msg          # over25 → etiqueta formal
        assert "Tarjetas −4.5" in msg          # cards_under_4.5 → formal

    def test_local_time_conversion(self):
        msg = _format_confirmations(ROWS)
        # 19:30 UTC = 13:30 America/Mexico_City
        assert "13:30" in msg
        assert "15:00" in msg                  # 21:00 UTC → 15:00 MX

    def test_naive_utc_from_the_database(self):
        """La base guarda UTC sin zona: 19:30 → 13:30 en México, sin
        depender de la zona horaria del servidor que corre el closing."""
        msg = _format_confirmations([dict(ROWS[0], match_date="2026-09-15 19:30:00")])
        assert "(13:30)" in msg

    def test_empty_rows_returns_header_only(self):
        msg = _format_confirmations([])
        assert "APUESTAS CONFIRMADAS" in msg
        assert "✅" not in msg

    def test_edge_none_does_not_crash(self):
        rows = [dict(ROWS[0], edge=None)]
        msg = _format_confirmations(rows)
        assert "edge +0%" in msg

    def test_bad_date_does_not_crash(self):
        rows = [dict(ROWS[0], match_date="fecha rota")]
        msg = _format_confirmations(rows)
        assert "(?)" in msg
