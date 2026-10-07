"""
Pydantic models for rent event extraction and ledger operations.

These are the data contracts the LLM must fill when it reads a WhatsApp message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class RentEventType(str, Enum):
    """What kind of financial event is being reported."""
    PAYMENT = "payment"         # Tenant paid rent (full or partial)
    PARTIAL = "partial"         # Explicitly stated partial payment
    DUE = "due"                 # Rent is due / reminder
    OVERDUE = "overdue"         # Rent is overdue
    WAIVED = "waived"           # Landlord waived this month's rent
    ADVANCE = "advance"         # Advance/deposit payment
    MAINTENANCE = "maintenance" # Deduction for maintenance
    UNKNOWN = "unknown"         # Could not determine type


class MessageIntent(str, Enum):
    """
    Top-level intent of the incoming message.

    LOG    → someone is reporting a payment/event  (WRITE to sheet)
    QUERY* → someone is asking a question          (READ from sheet)
    IRRELEVANT → not rent-related at all           (ignore silently)
    """
    LOG_PAYMENT = "log_payment"       # "Ali paid 15k for room 3"
    QUERY_BALANCE = "query_balance"   # "What's my balance?" / "Kitna bacha hai?"
    QUERY_HISTORY = "query_history"   # "Show my payments" / "Meri history dikhao"
    QUERY_DUE = "query_due"           # "How much do I owe?" / "Kitna dena hai?"
    QUERY_SUMMARY = "query_summary"   # "All tenants summary" (landlord view)
    IRRELEVANT = "irrelevant"         # Greetings, unrelated chat


class ExtractionResult(BaseModel):
    """
    Structured data extracted when the intent is LOG_PAYMENT.
    Confidence ∈ [0.0, 1.0].
    """
    tenant_name: Optional[str] = Field(
        None,
        description="Name or identifier of the tenant (e.g. 'Ali', 'Room 3 wala')",
    )
    room_number: Optional[str] = Field(
        None,
        description="Room/unit identifier (e.g. 'Room 3', '2B', 'upper portion')",
    )
    amount: Optional[float] = Field(
        None,
        description="Monetary amount in local currency (PKR/INR). Expand shorthand: 15k → 15000",
    )
    event_type: RentEventType = Field(
        RentEventType.UNKNOWN,
        description="Type of rent event",
    )
    month_year: Optional[str] = Field(
        None,
        description="Month/year this payment is for, e.g. 'October 2026'. Infer or leave None.",
    )
    notes: Optional[str] = Field(
        None,
        description="Any additional context from the message worth preserving",
    )
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    original_text: str = ""
    language_detected: str = "en"

    @field_validator("amount", mode="before")
    @classmethod
    def expand_shorthand(cls, v):
        """Convert '15k' → 15000.0, '1.5L' → 150000.0, etc."""
        if isinstance(v, str):
            v = v.strip().lower().replace(",", "")
            if v.endswith("k"):
                try:
                    return float(v[:-1]) * 1000
                except ValueError:
                    pass
            if v.endswith("l") or v.endswith("lac") or v.endswith("lakh"):
                try:
                    num = v.rstrip("laclakh").strip()
                    return float(num) * 100_000
                except ValueError:
                    pass
            try:
                return float(v)
            except ValueError:
                return None
        return v


class QueryParams(BaseModel):
    """
    Parameters extracted when the intent is a READ query.

    is_self_query = True  → sender said "my rent"  (resolve phone → tenant)
    is_self_query = False → sender named someone explicitly ("Ali's balance")
    """
    tenant_name: Optional[str] = None
    room_number: Optional[str] = None
    month_year: Optional[str] = None   # None = all-time or current month
    is_self_query: bool = False        # "my rent" vs "Ali's rent"


class ClassifiedMessage(BaseModel):
    """
    Single LLM response covering BOTH intent classification and data extraction.
    One API call — no two round-trips.

    intent      → routes the pipeline (log / query / ignore)
    extraction  → filled when intent == LOG_PAYMENT
    query       → filled when intent is any QUERY_* variant
    """
    intent: MessageIntent = MessageIntent.IRRELEVANT
    extraction: Optional[ExtractionResult] = None   # for LOG_PAYMENT
    query: Optional[QueryParams] = None             # for QUERY_* intents
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    original_text: str = ""
    language_detected: str = "en"


class LedgerRow(BaseModel):
    """
    One row written to the Google Sheet.
    Column order: Date | Tenant | Room | Amount (PKR) | Type | Month/Year |
                  Notes | Source Message | Logged At (UTC) | Sender Phone
    """
    date: str = Field(..., description="Date of the event (YYYY-MM-DD)")
    tenant_name: str
    room_number: str
    amount: float
    event_type: str
    month_year: str
    notes: str = ""
    source_message: str = ""
    logged_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    group_id: str = ""
    sender_phone: str = ""

    def to_sheet_row(self) -> list:
        """Convert to a flat list matching the Google Sheet column order."""
        return [
            self.date,
            self.tenant_name,
            self.room_number,
            str(self.amount),
            self.event_type,
            self.month_year,
            self.notes,
            self.source_message[:200],
            self.logged_at,
            self.sender_phone,
        ]

    @classmethod
    def sheet_headers(cls) -> list:
        return [
            "Date", "Tenant", "Room", "Amount (PKR)",
            "Type", "Month/Year", "Notes",
            "Source Message", "Logged At (UTC)", "Sender Phone",
        ]


class IncomingMessage(BaseModel):
    """
    Normalized inbound WhatsApp message (text or transcribed audio).
    Produced by the webhook handler before being passed to the pipeline.
    """
    message_id: str
    group_id: str
    sender_phone: str
    sender_name: str = ""
    text: str
    raw_type: str               # "text" | "audio" | "audio_transcribed"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_group_message: bool = True
