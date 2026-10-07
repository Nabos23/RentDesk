"""
Rent processing pipeline — intent-routed.

For every incoming WhatsApp message (text or transcribed audio):

  1. Mark message as read
  2. Call LLM → ClassifiedMessage (intent + data in one shot)
  3. Route by intent:
       LOG_PAYMENT  → validate → write Google Sheets → confirm in group
       QUERY_*      → read Google Sheets → format reply → send to group
       IRRELEVANT   → silent ignore (or clarify if near-miss)
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.ai.voice import transcribe_audio
from app.connectors.google_sheets_ledger import get_ledger
from app.connectors.whatsapp_client import get_whatsapp_client
from app.core.config import settings
from app.core.logging import get_logger
from app.rent.extractor import build_clarification_message, classify_message
from app.rent.query_handler import handle_query
from app.rent.schemas import (
    ClassifiedMessage,
    IncomingMessage,
    LedgerRow,
    MessageIntent,
    RentEventType,
)

logger = get_logger(__name__)


# ── Reply Templates ────────────────────────────────────────────────────────────

def _build_confirmation_message(classified: ClassifiedMessage, ledger_row: LedgerRow) -> str:
    """Human-friendly confirmation for a successful ledger write."""
    ex = classified.extraction
    event_emoji = {
        RentEventType.PAYMENT:     "✅",
        RentEventType.PARTIAL:     "⚠️",
        RentEventType.DUE:         "🔔",
        RentEventType.OVERDUE:     "🚨",
        RentEventType.WAIVED:      "🤝",
        RentEventType.ADVANCE:     "💰",
        RentEventType.MAINTENANCE: "🔧",
        RentEventType.UNKNOWN:     "📝",
    }.get(ex.event_type if ex else RentEventType.UNKNOWN, "📝")

    tenant = (ex.tenant_name if ex else None) or "Unknown"
    room   = (ex.room_number if ex else None) or "Unknown"
    amount_str = f"PKR {ex.amount:,.0f}" if ex and ex.amount else "—"
    month  = (ex.month_year if ex else None) or ledger_row.date[:7]

    lines = [
        f"{event_emoji} *Ledger Updated*",
        f"👤 Tenant: {tenant}",
        f"🏠 Room:   {room}",
        f"💵 Amount: {amount_str}",
        f"📅 Period: {month}",
        f"📋 Type:   {(ex.event_type.value.title() if ex else 'Unknown')}",
    ]
    if ex and ex.notes:
        lines.append(f"📌 Note:   {ex.notes}")
    lines.append("_Logged to Google Sheets_ ✓")
    return "\n".join(lines)


# ── LOG path ───────────────────────────────────────────────────────────────────

async def _handle_log(
    msg: IncomingMessage,
    classified: ClassifiedMessage,
) -> None:
    """Write a rent event to the ledger and confirm in the group."""
    wa = get_whatsapp_client()
    ledger = get_ledger()
    ex = classified.extraction

    if not ex:
        # LLM said log_payment but didn't fill extraction — ask for clarification
        wa.send_text(msg.group_id, build_clarification_message(classified))
        return

    threshold = settings.EXTRACTION_CONFIDENCE_THRESHOLD

    # Confidence gate
    if ex.confidence < threshold or ex.event_type == RentEventType.UNKNOWN:
        if ex.confidence < 0.2:
            logger.info("pipeline_log_ignored", reason="very_low_confidence")
            return
        wa.send_text(msg.group_id, build_clarification_message(classified))
        logger.info("pipeline_clarification_sent", confidence=ex.confidence)
        return

    # Build and write ledger row
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ledger_row = LedgerRow(
        date=today,
        tenant_name=ex.tenant_name or "Unknown",
        room_number=ex.room_number or "Unknown",
        amount=ex.amount or 0.0,
        event_type=ex.event_type.value,
        month_year=ex.month_year or today[:7],
        notes=ex.notes or "",
        source_message=msg.text[:200],
        group_id=msg.group_id,
        sender_phone=msg.sender_phone,
    )

    write_result = ledger.append_row(ledger_row)

    if write_result.get("status") == "error":
        logger.error("pipeline_ledger_write_failed", error=write_result.get("error"))
        wa.send_text(
            msg.group_id,
            "⚠️ I understood the payment but couldn't write to the ledger. "
            "Please check the Google Sheets connection.",
        )
        return

    # React ✅ + send confirmation
    react_res = wa.react(msg.group_id, msg.message_id, "✅")
    confirmation = _build_confirmation_message(classified, ledger_row)
    send_res = wa.send_text(msg.group_id, confirmation)

    logger.info(
        "pipeline_log_complete",
        tenant=ledger_row.tenant_name,
        room=ledger_row.room_number,
        amount=ledger_row.amount,
        range=write_result.get("range"),
        react_status=react_res.get("status"),
        send_status=send_res.get("status"),
        send_error=send_res.get("error"),
    )


# ── QUERY path ─────────────────────────────────────────────────────────────────

async def _handle_query(
    msg: IncomingMessage,
    classified: ClassifiedMessage,
) -> None:
    """Read from ledger and reply with the requested info."""
    wa = get_whatsapp_client()

    reply = await handle_query(msg, classified)

    react_res = wa.react(msg.group_id, msg.message_id, "👍")
    send_res = wa.send_text(msg.group_id, reply)

    logger.info(
        "pipeline_query_complete",
        intent=classified.intent,
        sender=msg.sender_phone,
        reply_length=len(reply),
        react_status=react_res.get("status"),
        send_status=send_res.get("status"),
        send_error=send_res.get("error"),
    )


# ── Main entry point ───────────────────────────────────────────────────────────

async def process_incoming_message(msg: IncomingMessage) -> None:
    """
    Main entry point called by the webhook handler for every message
    in a monitored group.
    """
    wa = get_whatsapp_client()

    logger.info(
        "pipeline_start",
        message_id=msg.message_id,
        group_id=msg.group_id,
        sender=msg.sender_phone,
        type=msg.raw_type,
        preview=msg.text[:60],
    )

    # Always mark as read immediately
    wa.mark_read(msg.message_id)

    # ── Step 1: Classify intent + extract data (single LLM call) ─────────────
    classified = await classify_message(msg.text, sender_name=msg.sender_name)

    logger.info(
        "pipeline_classified",
        intent=classified.intent,
        confidence=classified.confidence,
        language=classified.language_detected,
    )

    # ── Step 2: Route by intent ───────────────────────────────────────────────
    intent = classified.intent

    if intent == MessageIntent.LOG_PAYMENT:
        await _handle_log(msg, classified)

    elif intent in (
        MessageIntent.QUERY_BALANCE,
        MessageIntent.QUERY_HISTORY,
        MessageIntent.QUERY_DUE,
        MessageIntent.QUERY_SUMMARY,
    ):
        await _handle_query(msg, classified)

    elif intent == MessageIntent.IRRELEVANT:
        # Silently ignore — no reply, no write
        logger.info("pipeline_ignored", confidence=classified.confidence)

    else:
        logger.warning("pipeline_unknown_intent", intent=intent)


# ── Audio entry point ──────────────────────────────────────────────────────────

async def process_audio_message(
    msg: IncomingMessage,
    audio_bytes: bytes,
    mime_type: str,
) -> None:
    """
    Transcribe audio first, then run the normal intent-routing pipeline.
    """
    logger.info("pipeline_audio_transcribing", message_id=msg.message_id)
    transcript = await transcribe_audio(audio_bytes, mime_type)

    if not transcript:
        wa = get_whatsapp_client()
        wa.send_text(
            msg.group_id,
            "🎤 I received a voice note but couldn't transcribe it. "
            "Could you type out the message?",
        )
        logger.warning("pipeline_audio_empty", message_id=msg.message_id)
        return

    logger.info("pipeline_audio_transcribed", preview=transcript[:80])
    msg.text = transcript
    msg.raw_type = "audio_transcribed"
    await process_incoming_message(msg)
