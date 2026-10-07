"""
Query handler — READ path of the pipeline.

When the LLM decides the message is a QUERY (not a log), this module:
  1. Resolves which tenant/room to look up (from explicit name or sender phone)
  2. Fetches rows from Google Sheets
  3. Calculates totals / balance
  4. Formats a human-readable reply in the same language as the question
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.connectors.google_sheets_ledger import get_ledger
from app.core.logging import get_logger
from app.rent.schemas import ClassifiedMessage, IncomingMessage, MessageIntent

logger = get_logger(__name__)

# Event types that count as money coming IN (reduce balance owed)
_CREDIT_TYPES = {"payment", "partial", "waived", "advance"}
# Event types that count as money going OUT / additional charges
_DEBIT_TYPES = {"due", "overdue", "maintenance"}


# ── Tenant resolution ──────────────────────────────────────────────────────────

def _resolve_tenant(
    classified: ClassifiedMessage,
    msg: IncomingMessage,
) -> tuple[Optional[str], Optional[str]]:
    """
    Return (tenant_name, room_number) to look up.

    If is_self_query → use sender_name / sender_phone as the tenant identifier.
    Otherwise use the name the LLM extracted from the message.
    """
    q = classified.query
    if not q:
        return None, None

    if q.is_self_query:
        # Use the sender's WhatsApp display name as the tenant lookup key
        name = msg.sender_name or msg.sender_phone
        return name, q.room_number

    return q.tenant_name, q.room_number


# ── Aggregation helpers ────────────────────────────────────────────────────────

def _filter_by_month(rows: list[dict], month_year: Optional[str]) -> list[dict]:
    """Filter ledger rows to a specific month if requested."""
    if not month_year:
        return rows
    target = month_year.strip().lower()
    return [r for r in rows if target in r.get("Month/Year", "").lower()]


def _compute_totals(rows: list[dict]) -> dict:
    """
    Sum amounts by credit vs debit event types.
    Returns a dict with: total_paid, total_charged, balance_owed, row_count.
    """
    total_paid = 0.0
    total_charged = 0.0

    for row in rows:
        try:
            amount = float(row.get("Amount (PKR)", 0) or 0)
        except (ValueError, TypeError):
            amount = 0.0

        event_type = row.get("Type", "").lower()
        if event_type in _CREDIT_TYPES:
            total_paid += amount
        elif event_type in _DEBIT_TYPES:
            total_charged += amount

    balance_owed = max(total_charged - total_paid, 0.0)
    return {
        "total_paid": total_paid,
        "total_charged": total_charged,
        "balance_owed": balance_owed,
        "row_count": len(rows),
    }


# ── Reply formatters ───────────────────────────────────────────────────────────

def _fmt_currency(amount: float) -> str:
    return f"PKR {amount:,.0f}"


def _format_balance_reply(
    tenant: str,
    room: str,
    month_year: Optional[str],
    totals: dict,
    rows: list[dict],
) -> str:
    period = month_year or "all time"
    tenant_label = f"*{tenant}*" + (f" (Room {room})" if room else "")

    if totals["row_count"] == 0:
        return (
            f"🔍 No records found for {tenant_label} "
            f"({'for ' + period if month_year else 'in the ledger'}).\n"
            f"_Check the name or ask your property manager._"
        )

    lines = [
        f"📊 *Balance Summary — {tenant_label}*",
        f"📅 Period: {period}",
        f"",
        f"💰 Total Paid:    {_fmt_currency(totals['total_paid'])}",
    ]
    if totals["total_charged"] > 0:
        lines.append(f"📋 Total Charged: {_fmt_currency(totals['total_charged'])}")
        lines.append(f"⚠️  Balance Owed: {_fmt_currency(totals['balance_owed'])}")
    lines += [
        f"",
        f"📝 Transactions:  {totals['row_count']}",
    ]
    return "\n".join(lines)


def _format_history_reply(
    tenant: str,
    room: str,
    month_year: Optional[str],
    rows: list[dict],
) -> str:
    period = month_year or "all time"
    tenant_label = f"*{tenant}*" + (f" (Room {room})" if room else "")

    if not rows:
        return (
            f"🔍 No payment history found for {tenant_label} "
            f"({'for ' + period if month_year else ''})."
        )

    # Show latest 10 rows
    display = rows[-10:]
    lines = [f"📜 *Payment History — {tenant_label}*", f"📅 Period: {period}", ""]
    for r in display:
        date = r.get("Date", "?")
        amount = r.get("Amount (PKR)", "?")
        etype = r.get("Type", "?").title()
        m = r.get("Month/Year", "")
        lines.append(f"• {date}  {_fmt_currency(float(amount or 0))}  [{etype}]  {m}")

    if len(rows) > 10:
        lines.append(f"_...and {len(rows) - 10} older entries_")

    return "\n".join(lines)


def _format_due_reply(
    tenant: str,
    room: str,
    month_year: Optional[str],
    totals: dict,
    rows: list[dict],
) -> str:
    """For 'how much do I owe / what is my rent'."""
    period = month_year or "this period"
    tenant_label = f"*{tenant}*" + (f" (Room {room})" if room else "")

    if totals["row_count"] == 0:
        return (
            f"🔍 No records found for {tenant_label}.\n"
            f"_Your rent may not have been logged yet or the name doesn't match._"
        )

    owed = totals["balance_owed"]
    paid = totals["total_paid"]

    if owed <= 0 and paid > 0:
        return (
            f"✅ *{tenant_label}* — No balance owed!\n"
            f"💰 Paid {_fmt_currency(paid)} for {period}.\n"
            f"_All clear_ 🎉"
        )

    return (
        f"🔔 *Rent Due — {tenant_label}*\n"
        f"📅 Period: {period}\n"
        f"\n"
        f"💳 Amount Due:  {_fmt_currency(owed)}\n"
        f"✅ Paid So Far: {_fmt_currency(paid)}"
    )


def _format_summary_reply(rows: list[dict], month_year: Optional[str]) -> str:
    """Landlord-view: all tenants summary."""
    period = month_year or "all time"

    if not rows:
        return f"📋 No records in the ledger for {period}."

    # Group by tenant
    from collections import defaultdict
    by_tenant: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = r.get("Tenant", "Unknown")
        by_tenant[key].append(r)

    lines = [f"📋 *All Tenants Summary — {period}*", ""]
    for tenant, t_rows in sorted(by_tenant.items()):
        filtered = _filter_by_month(t_rows, month_year)
        totals = _compute_totals(filtered)
        room = filtered[0].get("Room", "") if filtered else ""
        room_str = f" (Room {room})" if room else ""
        owed = totals["balance_owed"]
        paid = totals["total_paid"]
        status = "✅" if owed <= 0 else "⚠️"
        lines.append(
            f"{status} *{tenant}*{room_str}: Paid {_fmt_currency(paid)}"
            + (f" | Owed {_fmt_currency(owed)}" if owed > 0 else "")
        )

    return "\n".join(lines)


# ── Main entry point ───────────────────────────────────────────────────────────

async def handle_query(
    msg: IncomingMessage,
    classified: ClassifiedMessage,
) -> str:
    """
    Execute a READ query against the ledger and return a formatted reply string.

    Args:
        msg:        The incoming WhatsApp message (for sender context).
        classified: The LLM classification result with intent + query params.

    Returns:
        A formatted string ready to send back to the WhatsApp group.
    """
    intent = classified.intent
    q = classified.query
    ledger = get_ledger()

    logger.info(
        "query_handler_start",
        intent=intent,
        sender=msg.sender_phone,
        is_self_query=q.is_self_query if q else False,
    )

    # ── Summary (landlord view — no tenant filter) ────────────────────────────
    if intent == MessageIntent.QUERY_SUMMARY:
        month_year = q.month_year if q else None
        all_rows = ledger.get_all_rows()
        filtered = _filter_by_month(all_rows, month_year)
        return _format_summary_reply(filtered, month_year)

    # ── Tenant-specific queries ───────────────────────────────────────────────
    tenant_name, room_number = _resolve_tenant(classified, msg)
    month_year = q.month_year if q else None

    if not tenant_name:
        return (
            "❓ I'm not sure whose record to look up.\n"
            "_Could you specify the tenant name? e.g. 'Show Ali's balance'_"
        )

    # Fetch rows for this tenant
    rows = ledger.get_tenant_history(tenant_name, room_number or "")
    filtered = _filter_by_month(rows, month_year)

    logger.info(
        "query_handler_fetched",
        tenant=tenant_name,
        room=room_number,
        total_rows=len(rows),
        filtered_rows=len(filtered),
    )

    if intent == MessageIntent.QUERY_BALANCE:
        totals = _compute_totals(filtered)
        return _format_balance_reply(tenant_name, room_number or "", month_year, totals, filtered)

    elif intent == MessageIntent.QUERY_HISTORY:
        return _format_history_reply(tenant_name, room_number or "", month_year, filtered)

    elif intent == MessageIntent.QUERY_DUE:
        totals = _compute_totals(filtered)
        return _format_due_reply(tenant_name, room_number or "", month_year, totals, filtered)

    # Fallback
    return "❓ I understood you have a question but couldn't figure out exactly what. Please try again."
