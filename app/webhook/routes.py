"""
WhatsApp webhook routes.

Meta calls GET /webhook to verify the endpoint, and
POST /webhook with all inbound message events.

Meta sends a 200 ACK within 3 seconds or it will retry — we respond
immediately with 200 and process in a background asyncio task.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.ai.pipeline import process_audio_message, process_incoming_message
from app.connectors.whatsapp_client import get_whatsapp_client
from app.core.config import settings
from app.core.logging import get_logger
from app.rent.schemas import IncomingMessage

logger = get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


# ── Verification ───────────────────────────────────────────────────────────────

@router.get("")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Meta webhook verification handshake.

    Meta sends this GET request when you first register the webhook URL
    in the Meta App Dashboard. We must echo back hub.challenge if the
    verify token matches.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("webhook_verified")
        return PlainTextResponse(content=hub_challenge)

    logger.warning(
        "webhook_verification_failed",
        mode=hub_mode,
        token_match=(hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN),
    )
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Inbound Events ─────────────────────────────────────────────────────────────

@router.post("")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive all inbound WhatsApp events from Meta.

    We always return 200 immediately (Meta requires < 3s response).
    Actual processing is deferred to a background task.
    """
    try:
        payload = await request.json()
        logger.info("webhook_received", keys=list(payload.keys()))
    except Exception as e:
        logger.error("webhook_parse_error", error=str(e))
        return {"status": "ok"}

    background_tasks.add_task(_process_payload, payload)
    return {"status": "ok"}


# ── Payload Processing ─────────────────────────────────────────────────────────

async def _process_payload(payload: dict[str, Any]) -> None:
    try:
        if payload.get("object") != "whatsapp_business_account":
            return

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])
                statuses = value.get("statuses", [])

                for status in statuses:
                    s_id = status.get("id")
                    st = status.get("status")
                    recipient = status.get("recipient_id")
                    errors = status.get("errors")
                    logger.info("webhook_status_update", status=st, recipient=recipient, errors=errors, msg_id=s_id)

                # Build a name map: wa_id → display name
                name_map: dict[str, str] = {}
                for contact in contacts:
                    wa_id = contact.get("wa_id", "")
                    name = contact.get("profile", {}).get("name", "")
                    if wa_id:
                        name_map[wa_id] = name

                for raw_msg in messages:
                    await _dispatch_message(raw_msg, name_map, value)

    except Exception as e:
        logger.error("webhook_processing_error", error=str(e), exc_info=True)


async def _dispatch_message(
    raw_msg: dict,
    name_map: dict[str, str],
    value: dict,
) -> None:
    """
    Normalise a single raw WhatsApp message and route it to the pipeline.
    """
    try:
        msg_id = raw_msg.get("id", "")
        sender_phone = raw_msg.get("from", "")
        msg_type = raw_msg.get("type", "")
        sender_name = name_map.get(sender_phone, "")
        group_id = raw_msg.get("group_id") or sender_phone

        logger.info(
            "webhook_message_received",
            msg_id=msg_id,
            sender=sender_phone,
            type=msg_type,
            group_id=group_id,
        )

        # ── Text message ──────────────────────────────────────────────────────────
        if msg_type == "text":
            text = raw_msg.get("text", {}).get("body", "").strip()
            if not text:
                return

            msg = IncomingMessage(
                message_id=msg_id,
                group_id=group_id,
                sender_phone=sender_phone,
                sender_name=sender_name,
                text=text,
                raw_type="text",
            )
            await process_incoming_message(msg)

        # ── Audio / Voice note ────────────────────────────────────────────────────
        elif msg_type == "audio":
            audio_info = raw_msg.get("audio", {})
            media_id = audio_info.get("id")
            mime_type = audio_info.get("mime_type", "audio/ogg")

            if not media_id:
                logger.warning("webhook_audio_no_media_id", msg_id=msg_id)
                return

            # Download media bytes
            wa_client = get_whatsapp_client()
            media_url = wa_client.get_media_url(media_id)
            if not media_url:
                logger.error("webhook_audio_url_failed", media_id=media_id)
                return

            audio_bytes = wa_client.download_media(media_url)
            if not audio_bytes:
                logger.error("webhook_audio_download_failed", media_id=media_id)
                return

            msg = IncomingMessage(
                message_id=msg_id,
                group_id=group_id,
                sender_phone=sender_phone,
                sender_name=sender_name,
                text="",   # will be filled by transcription
                raw_type="audio",
            )
            await process_audio_message(msg, audio_bytes, mime_type)

        else:
            logger.info("webhook_unsupported_type", msg_type=msg_type, msg_id=msg_id)

    except Exception as e:
        logger.error("dispatch_message_failed", error=str(e), exc_info=True)
