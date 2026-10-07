"""
WhatsApp Business API connector — outbound messaging.

Ported from Legacy_AI/ai/connectors/whatsapp.py.
Standalone (no MongoDB permission checks needed for this project).
API version: v19.0
"""
from __future__ import annotations

import json
import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_BASE = "https://graph.facebook.com/v19.0"


def _handle_error(tool_name: str, e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code in (401, 403):
            return (
                f"Authorization error in {tool_name}: the WhatsApp Business account "
                f"lacks permission (HTTP {e.response.status_code}). "
                "Check WHATSAPP_ACCESS_TOKEN in .env."
            )
        return f"API error in {tool_name} (HTTP {e.response.status_code}): {e.response.text[:300]}"
    return f"Unexpected error in {tool_name}: {e}"


class WhatsAppClient:
    """
    Outbound WhatsApp Business API client.
    Uses WHATSAPP_ACCESS_TOKEN + WHATSAPP_PHONE_NUMBER_ID from settings.
    """

    def __init__(self) -> None:
        self._phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID

    @property
    def _token(self) -> str:
        """Always read token fresh from settings."""
        return settings.WHATSAPP_ACCESS_TOKEN

    @property
    def _auth_headers(self) -> dict[str, str]:
        """Build auth headers with fresh token every time."""
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ── Send ──────────────────────────────────────────────────────────────────

    def send_text(self, to: str, text: str) -> dict:
        """Send a plain text message to a phone number or group."""
        try:
            body = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            }
            resp = httpx.post(
                f"{_BASE}/{self._phone_number_id}/messages",
                headers=self._auth_headers,
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            messages = data.get("messages", [{}])
            msg_id = messages[0].get("id") if messages else None
            logger.info("whatsapp_sent", to=to, message_id=msg_id, length=len(text))
            return {"status": "sent", "message_id": msg_id, "to": to}
        except Exception as e:
            err = _handle_error("send_text", e)
            logger.error("whatsapp_send_error", to=to, error=err)
            return {"status": "error", "error": err}

    def react(self, to: str, message_id: str, emoji: str = "✅") -> dict:
        """React to a specific message with an emoji (acknowledge receipt)."""
        try:
            body = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": emoji},
            }
            resp = httpx.post(
                f"{_BASE}/{self._phone_number_id}/messages",
                headers=self._auth_headers,
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            return {"status": "reacted", "emoji": emoji}
        except Exception as e:
            err = _handle_error("react", e)
            logger.warning("whatsapp_react_error", error=err)
            return {"status": "error", "error": err}

    # ── Media ─────────────────────────────────────────────────────────────────

    def get_media_url(self, media_id: str) -> str | None:
        """
        Resolve a WhatsApp media_id to a download URL.
        The URL expires in ~5 minutes — download promptly.
        """
        try:
            resp = httpx.get(
                f"{_BASE}/{media_id}",
                headers=self._auth_headers,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("url")
        except Exception as e:
            logger.error("whatsapp_get_media_url_error", media_id=media_id, error=str(e))
            return None

    def download_media(self, url: str) -> bytes | None:
        """
        Download raw bytes from a WhatsApp media URL.
        Must use the same Bearer token as the API.
        """
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=60,
                follow_redirects=True,
            )
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.error("whatsapp_download_media_error", error=str(e))
            return None

    def mark_read(self, message_id: str) -> dict:
        """Mark an incoming message as read (shows blue ticks to sender)."""
        try:
            body = {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            }
            resp = httpx.post(
                f"{_BASE}/{self._phone_number_id}/messages",
                headers=self._auth_headers,
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            return {"status": "marked_read"}
        except Exception as e:
            logger.warning("whatsapp_mark_read_error", message_id=message_id, error=str(e))
            return {"status": "error", "error": str(e)}


# ── Singleton ─────────────────────────────────────────────────────────────────

_client: WhatsAppClient | None = None


def get_whatsapp_client() -> WhatsAppClient:
    global _client
    if _client is None:
        _client = WhatsAppClient()
    return _client


def reset_whatsapp_client() -> None:
    """Force re-creation of the client (e.g. after token update)."""
    global _client
    _client = None
