"""
Google Sheets ledger writer.

Ported & simplified from Legacy_AI/ai/connectors/google_sheets.py.
Uses a service-account or OAuth2 token stored in GOOGLE_TOKEN_PATH.

The ledger sheet has these columns (in order):
  Date | Tenant | Room | Amount (PKR) | Type | Month/Year |
  Notes | Source Message | Logged At (UTC) | Sender Phone
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.core.config import settings
from app.core.logging import get_logger
from app.rent.schemas import LedgerRow

logger = get_logger(__name__)

_SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"

# Required OAuth scopes
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _handle_error(tool_name: str, e: Exception) -> str:
    """Mirror of Legacy_AI error handler pattern."""
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code in (401, 403):
            return (
                f"Authorization error in {tool_name}: Google Sheets lacks permission "
                f"(HTTP {e.response.status_code}). Re-run the OAuth flow."
            )
        return f"API error in {tool_name} (HTTP {e.response.status_code}): {e.response.text[:300]}"
    return f"Unexpected error in {tool_name}: {e}"


class GoogleSheetsLedger:
    """
    Manages the rent ledger Google Sheet.

    Auth: tries GOOGLE_TOKEN_PATH first (stored OAuth token).
    If missing or expired, runs InstalledAppFlow (opens browser for consent).
    For production/headless, swap to a service account JSON.
    """

    def __init__(self) -> None:
        self._creds: Credentials | None = None
        self._token_path = settings.GOOGLE_TOKEN_PATH
        self._creds_path = settings.GOOGLE_CREDENTIALS_PATH
        self._spreadsheet_id = settings.SPREADSHEET_ID
        self._sheet_name = settings.LEDGER_SHEET_NAME

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _get_creds(self) -> Credentials:
        """Load or refresh OAuth2 credentials. Runs OAuth flow if needed."""
        creds: Credentials | None = None

        if Path(self._token_path).exists():
            creds = Credentials.from_authorized_user_file(self._token_path, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(self._token_path, "w") as f:
                    f.write(creds.to_json())
            else:
                raise RuntimeError(
                    f"Google token not found or invalid at '{self._token_path}'. "
                    "Please visit http://localhost:8000/auth/google/connect to authorize Google Sheets."
                )

        self._creds = creds
        return creds

    def _auth_headers(self) -> dict[str, str]:
        creds = self._get_creds()
        return {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Sheet Initialisation ──────────────────────────────────────────────────

    def ensure_headers(self) -> None:
        """
        Write the header row to the sheet if it is empty.
        Call this once on startup.
        """
        try:
            headers = self._auth_headers()
            resp = httpx.get(
                f"{_SHEETS_BASE}/{self._spreadsheet_id}/values/{self._sheet_name}!A1:Z1",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            existing = resp.json().get("values", [])
            if existing:
                logger.info("ledger_headers_exist", sheet=self._sheet_name)
                return

            # Write headers
            body = {"values": [LedgerRow.sheet_headers()]}
            write_resp = httpx.put(
                f"{_SHEETS_BASE}/{self._spreadsheet_id}/values/{self._sheet_name}!A1",
                headers=headers,
                json=body,
                params={"valueInputOption": "USER_ENTERED"},
                timeout=15,
            )
            write_resp.raise_for_status()
            logger.info("ledger_headers_written", sheet=self._sheet_name)

        except Exception as e:
            logger.warning("ledger_headers_error", error=str(e))

    # ── Write ─────────────────────────────────────────────────────────────────

    def append_row(self, row: LedgerRow) -> dict[str, Any]:
        """
        Append a single LedgerRow to the sheet.
        Returns the API response metadata.
        """
        try:
            headers = self._auth_headers()
            body = {"values": [row.to_sheet_row()]}
            resp = httpx.post(
                f"{_SHEETS_BASE}/{self._spreadsheet_id}/values/{self._sheet_name}:append",
                headers=headers,
                json=body,
                params={"valueInputOption": "USER_ENTERED"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            updated_range = data.get("updates", {}).get("updatedRange", "")
            logger.info(
                "ledger_row_appended",
                tenant=row.tenant_name,
                room=row.room_number,
                amount=row.amount,
                range=updated_range,
            )
            return {"status": "ok", "range": updated_range}

        except Exception as e:
            logger.error("ledger_append_error", error=str(e), tenant=row.tenant_name)
            return {"status": "error", "error": _handle_error("append_row", e)}

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_tenant_history(self, tenant_name: str, room_number: str = "") -> list[dict]:
        """
        Fetch all ledger rows for a given tenant (and optionally room).
        Used by the bot to answer "what's Ali's balance?" queries.
        """
        try:
            headers = self._auth_headers()
            resp = httpx.get(
                f"{_SHEETS_BASE}/{self._spreadsheet_id}/values/{self._sheet_name}",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json().get("values", [])
            if not raw:
                return []

            col_headers = raw[0]
            rows = []
            for row_data in raw[1:]:
                # Pad short rows
                padded = row_data + [""] * (len(col_headers) - len(row_data))
                row_dict = dict(zip(col_headers, padded))
                tenant_match = tenant_name.lower() in row_dict.get("Tenant", "").lower()
                room_match = (
                    not room_number
                    or room_number.lower() in row_dict.get("Room", "").lower()
                )
                if tenant_match and room_match:
                    rows.append(row_dict)

            return rows

        except Exception as e:
            logger.error("ledger_read_error", error=str(e))
            return []

    def get_all_rows(self) -> list[dict]:
        """Return all ledger rows as list of dicts (for summary/reporting)."""
        try:
            headers = self._auth_headers()
            resp = httpx.get(
                f"{_SHEETS_BASE}/{self._spreadsheet_id}/values/{self._sheet_name}",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json().get("values", [])
            if len(raw) < 2:
                return []
            col_headers = raw[0]
            return [
                dict(zip(col_headers, row + [""] * (len(col_headers) - len(row))))
                for row in raw[1:]
            ]
        except Exception as e:
            logger.error("ledger_get_all_error", error=str(e))
            return []


# ── Singleton ─────────────────────────────────────────────────────────────────

_ledger: GoogleSheetsLedger | None = None


def get_ledger() -> GoogleSheetsLedger:
    global _ledger
    if _ledger is None:
        _ledger = GoogleSheetsLedger()
    return _ledger
