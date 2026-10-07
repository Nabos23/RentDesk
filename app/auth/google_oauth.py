"""
Google OAuth2 callback route.

After the user clicks "Allow" in Google's consent screen, they are redirected
to this endpoint with an auth code. We exchange it for tokens and save them.

Usage:
  1. Visit /auth/google/connect in your browser
  2. Complete Google consent → redirected to /auth/google/callback
  3. Tokens saved to GOOGLE_TOKEN_PATH — bot can now write to Sheets
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/auth/google", tags=["auth"])

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _get_flow() -> Flow:
    if not Path(settings.GOOGLE_CREDENTIALS_PATH).exists():
        raise FileNotFoundError(
            f"'{settings.GOOGLE_CREDENTIALS_PATH}' not found. "
            "Download your OAuth 2.0 client JSON from Google Cloud Console."
        )
    return Flow.from_client_secrets_file(
        settings.GOOGLE_CREDENTIALS_PATH,
        scopes=_SCOPES,
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
    )


@router.get("/connect")
async def google_connect():
    """
    Initiate Google OAuth2 flow.
    Visit this URL in a browser to connect your Google account.
    """
    try:
        flow = _get_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return RedirectResponse(url=auth_url)
    except FileNotFoundError as e:
        return HTMLResponse(
            content=f"<pre>Error: {e}\n\nSetup instructions:\n"
            "1. Go to https://console.cloud.google.com\n"
            "2. Create OAuth 2.0 credentials (Desktop app)\n"
            "3. Download and save as google_credentials.json in the project root</pre>",
            status_code=500,
        )


@router.get("/callback")
async def google_callback(request: Request):
    """
    Handle the OAuth2 callback from Google.
    Exchanges the code for tokens and saves to GOOGLE_TOKEN_PATH.
    """
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse(
            content="<h1>Error</h1><p>No authorization code received from Google.</p>",
            status_code=400,
        )

    try:
        flow = _get_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials

        with open(settings.GOOGLE_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

        logger.info("google_oauth_complete", token_path=settings.GOOGLE_TOKEN_PATH)

        return HTMLResponse(content="""
        <html>
        <body style="font-family: sans-serif; max-width: 600px; margin: 60px auto; text-align: center;">
            <h1>✅ Google Sheets Connected!</h1>
            <p>The bot can now write to your Google Sheet ledger.</p>
            <p>You can close this window.</p>
        </body>
        </html>
        """)

    except Exception as e:
        logger.error("google_oauth_error", error=str(e))
        return HTMLResponse(
            content=f"<h1>OAuth Error</h1><pre>{e}</pre>",
            status_code=500,
        )


@router.get("/status")
async def google_status():
    """Check if Google Sheets is connected."""
    token_exists = Path(settings.GOOGLE_TOKEN_PATH).exists()
    return {
        "connected": token_exists,
        "token_path": settings.GOOGLE_TOKEN_PATH,
        "spreadsheet_id": settings.SPREADSHEET_ID or "not configured",
        "sheet_name": settings.LEDGER_SHEET_NAME,
    }
