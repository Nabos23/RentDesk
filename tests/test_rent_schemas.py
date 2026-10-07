"""
Unit tests for rent schemas, extraction logic, and query handler aggregation.
No external API calls required.
"""
import pytest
from app.rent.schemas import (
    ClassifiedMessage,
    ExtractionResult,
    MessageIntent,
    QueryParams,
    RentEventType,
    LedgerRow,
)


# ── Amount shorthand expansion ─────────────────────────────────────────────────

class TestAmountShorthandExpansion:
    def test_k_suffix(self):
        r = ExtractionResult(amount="15k", confidence=0.9, event_type=RentEventType.PAYMENT, original_text="")
        assert r.amount == 15000.0

    def test_lakh_suffix(self):
        r = ExtractionResult(amount="1.5lakh", confidence=0.9, event_type=RentEventType.PAYMENT, original_text="")
        assert r.amount == 150000.0

    def test_lac_suffix(self):
        r = ExtractionResult(amount="2lac", confidence=0.9, event_type=RentEventType.PAYMENT, original_text="")
        assert r.amount == 200000.0

    def test_l_suffix(self):
        r = ExtractionResult(amount="1l", confidence=0.9, event_type=RentEventType.PAYMENT, original_text="")
        assert r.amount == 100000.0

    def test_numeric_string(self):
        r = ExtractionResult(amount="12000", confidence=0.9, event_type=RentEventType.PAYMENT, original_text="")
        assert r.amount == 12000.0

    def test_float(self):
        r = ExtractionResult(amount=15000.0, confidence=0.9, event_type=RentEventType.PAYMENT, original_text="")
        assert r.amount == 15000.0

    def test_comma_separated(self):
        r = ExtractionResult(amount="15,000", confidence=0.9, event_type=RentEventType.PAYMENT, original_text="")
        assert r.amount == 15000.0

    def test_none(self):
        r = ExtractionResult(amount=None, confidence=0.3, event_type=RentEventType.UNKNOWN, original_text="")
        assert r.amount is None


# ── LedgerRow ──────────────────────────────────────────────────────────────────

class TestLedgerRow:
    def test_to_sheet_row_length(self):
        row = LedgerRow(
            date="2026-10-07", tenant_name="Ali", room_number="Room 3",
            amount=15000.0, event_type="payment", month_year="October 2026",
        )
        assert len(row.to_sheet_row()) == len(LedgerRow.sheet_headers())

    def test_headers_count(self):
        assert len(LedgerRow.sheet_headers()) == 10

    def test_source_message_truncated(self):
        row = LedgerRow(
            date="2026-10-07", tenant_name="Ali", room_number="3",
            amount=0, event_type="payment", month_year="Oct 2026",
            source_message="x" * 300,
        )
        assert len(row.to_sheet_row()[7]) <= 200


# ── ClassifiedMessage ──────────────────────────────────────────────────────────

class TestClassifiedMessage:
    def test_default_intent(self):
        c = ClassifiedMessage(confidence=0.0, original_text="hi")
        assert c.intent == MessageIntent.IRRELEVANT

    def test_log_payment_with_extraction(self):
        ex = ExtractionResult(
            tenant_name="Ali", room_number="3", amount=15000.0,
            event_type=RentEventType.PAYMENT, confidence=0.9, original_text="",
        )
        c = ClassifiedMessage(
            intent=MessageIntent.LOG_PAYMENT,
            extraction=ex,
            confidence=0.9,
            original_text="Ali paid 15k",
        )
        assert c.intent == MessageIntent.LOG_PAYMENT
        assert c.extraction is not None
        assert c.extraction.amount == 15000.0
        assert c.query is None

    def test_query_with_params(self):
        q = QueryParams(is_self_query=True, month_year="October 2026")
        c = ClassifiedMessage(
            intent=MessageIntent.QUERY_DUE,
            query=q,
            confidence=0.85,
            original_text="What is my rent this month?",
        )
        assert c.intent == MessageIntent.QUERY_DUE
        assert c.query.is_self_query is True
        assert c.query.month_year == "October 2026"
        assert c.extraction is None

    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            ClassifiedMessage(confidence=1.5, original_text="")
        with pytest.raises(Exception):
            ClassifiedMessage(confidence=-0.1, original_text="")


# ── Query handler aggregation logic ───────────────────────────────────────────

class TestQueryAggregation:
    """Test the pure aggregation functions in query_handler without hitting Sheets."""

    def _make_rows(self, entries: list[tuple]) -> list[dict]:
        """Helper: list of (amount, type, month) tuples → sheet row dicts."""
        return [
            {"Amount (PKR)": str(amt), "Type": typ, "Month/Year": month,
             "Tenant": "Ali", "Room": "3", "Date": "2026-10-01"}
            for amt, typ, month in entries
        ]

    def test_credit_reduces_balance(self):
        from app.rent.query_handler import _compute_totals
        rows = self._make_rows([
            (15000, "payment", "October 2026"),
            (15000, "due", "October 2026"),
        ])
        totals = _compute_totals(rows)
        assert totals["total_paid"] == 15000.0
        assert totals["total_charged"] == 15000.0
        assert totals["balance_owed"] == 0.0

    def test_partial_payment_leaves_balance(self):
        from app.rent.query_handler import _compute_totals
        rows = self._make_rows([
            (5000, "partial", "October 2026"),
            (15000, "due", "October 2026"),
        ])
        totals = _compute_totals(rows)
        assert totals["balance_owed"] == 10000.0

    def test_balance_never_negative(self):
        from app.rent.query_handler import _compute_totals
        rows = self._make_rows([
            (20000, "payment", "October 2026"),
            (15000, "due", "October 2026"),
        ])
        totals = _compute_totals(rows)
        assert totals["balance_owed"] == 0.0   # overpaid → 0, not negative

    def test_filter_by_month(self):
        from app.rent.query_handler import _filter_by_month
        rows = self._make_rows([
            (15000, "payment", "October 2026"),
            (15000, "payment", "September 2026"),
        ])
        filtered = _filter_by_month(rows, "October 2026")
        assert len(filtered) == 1
        assert filtered[0]["Month/Year"] == "October 2026"

    def test_filter_none_returns_all(self):
        from app.rent.query_handler import _filter_by_month
        rows = self._make_rows([
            (15000, "payment", "October 2026"),
            (15000, "payment", "September 2026"),
        ])
        assert len(_filter_by_month(rows, None)) == 2

    def test_empty_rows_totals(self):
        from app.rent.query_handler import _compute_totals
        totals = _compute_totals([])
        assert totals["total_paid"] == 0.0
        assert totals["balance_owed"] == 0.0
        assert totals["row_count"] == 0
