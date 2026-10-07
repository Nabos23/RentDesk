"""
Rent message classifier + extractor using OpenAI structured output.

Single LLM call → ClassifiedMessage containing:
  - intent   (log_payment / query_* / irrelevant)
  - extraction (for LOG_PAYMENT)
  - query      (for QUERY_* intents)

English, Hindi, and Roman Hindi are all supported.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import get_logger
from app.rent.schemas import (
    ClassifiedMessage,
    ExtractionResult,
    MessageIntent,
    QueryParams,
    RentEventType,
)

logger = get_logger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


# ── System Prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a bilingual rent assistant for a Pakistani/Indian property management group.
Read the WhatsApp message (English, Hindi, or Roman Hindi) and return ONE JSON object.

TODAY: {today}

INTENT OPTIONS:
- "log_payment"    → Someone is REPORTING a payment or rent event (write to ledger)
- "query_balance"  → Asking their remaining balance / "kitna bacha hai"
- "query_history"  → Asking to see their payment history / "meri history"
- "query_due"      → Asking how much they owe / "kitna dena hai" / "what is my rent"
- "query_summary"  → Landlord asking for all-tenants summary / "sab ka status"
- "irrelevant"     → Greetings, unrelated chat, spam

OUTPUT JSON SCHEMA (always return all top-level keys):
{{
  "intent": "<one of the 6 values above>",
  "confidence": 0.0-1.0,
  "original_text": "<the exact input>",
  "language_detected": "en|hi|roman_hindi",

  "extraction": {{
    "tenant_name": "string or null",
    "room_number": "string or null",
    "amount": number or null,
    "event_type": "payment|partial|due|overdue|waived|advance|maintenance|unknown",
    "month_year": "string like 'October 2026' or null",
    "notes": "string or null",
    "confidence": 0.0-1.0,
    "original_text": "<same input>",
    "language_detected": "en|hi|roman_hindi"
  }},

  "query": {{
    "tenant_name": "string or null",
    "room_number": "string or null",
    "month_year": "string or null",
    "is_self_query": true|false
  }}
}}

RULES:
1. Fill "extraction" only when intent == "log_payment". Otherwise set it to null.
2. Fill "query" only when intent starts with "query_". Otherwise set it to null.
3. is_self_query = true when the sender says "my" (mera, meri, apna, my own).
4. amount: expand shorthand → 15k=15000, 1.5L=150000. "half" → null.
5. month_year: infer from context ("last month" relative to today). If unclear → null.
6. confidence: be honest. Clear payment → 0.85-0.95. Ambiguous → 0.3-0.6.
7. If the message has NOTHING to do with rent → intent="irrelevant", confidence < 0.2.

EXAMPLES:
  "Ali paid 15k for room 3" → intent=log_payment, extraction={{tenant_name=Ali, room=Room 3, amount=15000, event_type=payment}}
  "Ali ne 15k diya Room 3 ka" → same, language=roman_hindi
  "What is my rent this month?" → intent=query_due, query={{is_self_query=true, month_year=<current month>}}
  "Mera balance kya hai?" → intent=query_balance, query={{is_self_query=true}}
  "Ahmed ki history dikhao" → intent=query_history, query={{tenant_name=Ahmed, is_self_query=false}}
  "Sab ka October ka status" → intent=query_summary, query={{month_year=October 2026}}
  "Good morning everyone" → intent=irrelevant
"""


async def classify_message(text: str, sender_name: str = "") -> ClassifiedMessage:
    """
    Classify intent and extract structured data from a WhatsApp message.

    Args:
        text: Raw message text (already transcribed if it was audio).
        sender_name: Display name of the sender (used as context for is_self_query).

    Returns:
        ClassifiedMessage with intent + either extraction or query data.
    """
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    system = _SYSTEM_PROMPT.format(today=today)

    user_content = text
    if sender_name:
        user_content = f"[Sender name: {sender_name}]\nMessage: {text}"

    try:
        client = _get_client()
        response = await client.chat.completions.create(
            model=settings.DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
            max_tokens=600,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content
        data = json.loads(raw)
        data["original_text"] = text

        result = _parse_classified(data, text)

        logger.info(
            "classify_message_success",
            intent=result.intent,
            confidence=result.confidence,
            language=result.language_detected,
            tenant=result.extraction.tenant_name if result.extraction else (
                result.query.tenant_name if result.query else None
            ),
        )
        return result

    except json.JSONDecodeError as e:
        logger.error("classify_message_json_error", error=str(e), text=text[:100])
    except Exception as e:
        logger.error("classify_message_error", error=str(e), text=text[:100])

    # Fallback: treat as irrelevant
    return ClassifiedMessage(
        intent=MessageIntent.IRRELEVANT,
        confidence=0.0,
        original_text=text,
    )


def _parse_classified(data: dict, original_text: str) -> ClassifiedMessage:
    """Parse the raw LLM JSON dict into a ClassifiedMessage."""

    # Parse nested extraction
    extraction = None
    if data.get("extraction") and data.get("intent") == "log_payment":
        ex = data["extraction"]
        ex["original_text"] = original_text
        try:
            extraction = ExtractionResult(**ex)
        except Exception as e:
            logger.warning("extraction_parse_error", error=str(e))

    # Parse nested query
    query = None
    intent_str = data.get("intent", "irrelevant")
    if data.get("query") and intent_str.startswith("query_"):
        try:
            query = QueryParams(**data["query"])
        except Exception as e:
            logger.warning("query_parse_error", error=str(e))

    return ClassifiedMessage(
        intent=MessageIntent(intent_str) if intent_str in MessageIntent._value2member_map_ else MessageIntent.IRRELEVANT,
        extraction=extraction,
        query=query,
        confidence=float(data.get("confidence", 0.0)),
        original_text=original_text,
        language_detected=data.get("language_detected", "en"),
    )


def build_clarification_message(result: ClassifiedMessage) -> str:
    """
    Generate a clarification question when a LOG_PAYMENT is ambiguous.
    """
    ex = result.extraction
    missing = []
    if not ex or not ex.tenant_name:
        missing.append("tenant name")
    if not ex or not ex.room_number:
        missing.append("room number")
    if not ex or ex.amount is None:
        missing.append("amount")

    if missing:
        fields = " and ".join(missing)
        return (
            f"❓ I couldn't log this — could you clarify the {fields}?\n"
            f'_(e.g. "Ali Room 3 — 15,000 paid for October")_'
        )
    return (
        f"❓ I'm not sure I understood this. Could you rephrase?\n"
        f'_(e.g. "[Tenant] [Room] — [Amount] paid for [Month]")_'
    )
