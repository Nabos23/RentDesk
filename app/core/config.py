"""
Application settings loaded from environment / .env file.
Pattern ported from Legacy_AI/backend/core/config.py
"""
import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── WhatsApp Business API ─────────────────────────────────────────────────
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_WABA_ID: str = ""
    # Verify token: any string you set in Meta App Dashboard → Webhooks
    WHATSAPP_VERIFY_TOKEN: str = "rental_bot_verify"

    # ── OpenAI ────────────────────────────────────────────────────────────────
    OPENAI_API_KEY: str = ""
    DEFAULT_MODEL: str = "gpt-4.1"
    WHISPER_MODEL: str = "whisper-1"

    # ── Google Sheets ─────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"
    SPREADSHEET_ID: str = ""
    LEDGER_SHEET_NAME: str = "Ledger"
    # Path to stored token JSON (written after first OAuth flow)
    GOOGLE_TOKEN_PATH: str = "google_token.json"
    GOOGLE_CREDENTIALS_PATH: str = "google_credentials.json"


    # ── Security ──────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ENCRYPTION_KEY: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ── Rent extraction ───────────────────────────────────────────────────────
    # Confidence threshold: 0.0 – 1.0. Below this, bot asks for clarification.
    EXTRACTION_CONFIDENCE_THRESHOLD: float = 0.6

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    @property
    def whatsapp_token_header(self) -> dict:
        return {
            "Authorization": f"Bearer {self.WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }


settings = Settings()
