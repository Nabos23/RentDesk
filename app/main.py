"""
FastAPI application factory.

Modelled after Legacy_AI/backend/main.py but streamlined for this project.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth.google_oauth import router as google_auth_router
from app.connectors.google_sheets_ledger import get_ledger
from app.core.config import settings
from app.core.database import close_client, init_db
from app.core.logging import get_logger, setup_logging
from app.webhook.routes import router as webhook_router

setup_logging()
logger = get_logger("app.main")


def create_app() -> FastAPI:
    app = FastAPI(
        title="RentDesk",
        description=(
            "AI-powered WhatsApp assistant that automates rental bookkeeping, "
            "tracks payments, and answers tenant queries directly from Google Sheets."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.is_dev else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(webhook_router)
    app.include_router(google_auth_router)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    @app.on_event("startup")
    async def on_startup():
        logger.info("app_starting", environment=settings.ENVIRONMENT)

        # Initialize SQLite database
        try:
            init_db()
        except Exception as e:
            logger.warning("sqlite_unavailable", error=str(e))

        # Ensure Google Sheets header row exists (no-op if already there)
        try:
            ledger = get_ledger()
            ledger.ensure_headers()
        except FileNotFoundError:
            logger.warning(
                "google_sheets_not_configured",
                hint="Visit /auth/google/connect to set up Google Sheets",
            )
        except Exception as e:
            logger.warning("google_sheets_startup_error", error=str(e))

        logger.info("app_ready", webhook_url="/webhook", docs_url="/docs")

    @app.on_event("shutdown")
    async def on_shutdown():
        await close_client()
        logger.info("app_shutdown")

    # ── Health ────────────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health():
        return {
            "status": "ok",
            "environment": settings.ENVIRONMENT,
            "whatsapp_configured": bool(settings.WHATSAPP_ACCESS_TOKEN),
            "openai_configured": bool(settings.OPENAI_API_KEY),
            "spreadsheet_id": settings.SPREADSHEET_ID or "not set",
        }

    @app.get("/", tags=["system"])
    async def root():
        return {
            "name": "RentDesk",
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/health",
            "webhook": "/webhook",
            "google_connect": "/auth/google/connect",
        }

    return app


app = create_app()
