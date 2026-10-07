"""
debug_runner.py - Diagnostics and end-to-end testing script.
"""
import asyncio
import os
import sys
import httpx

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.core.config import settings
from app.connectors.whatsapp_client import get_whatsapp_client
from app.rent.extractor import classify_message
from app.rent.query_handler import handle_query
from app.rent.schemas import IncomingMessage

def test_config():
    print("=== CONFIG CHECK ===")
    print("PHONE_NUMBER_ID:", settings.WHATSAPP_PHONE_NUMBER_ID)
    print("VERIFY_TOKEN:", settings.WHATSAPP_VERIFY_TOKEN)
    token = settings.WHATSAPP_ACCESS_TOKEN
    masked = (token[:6] + "..." + token[-6:]) if len(token) > 12 else "NONE"
    print("ACCESS_TOKEN:", masked)
    print("OPENAI_KEY_SET:", bool(settings.OPENAI_API_KEY))
    print("SPREADSHEET_ID:", settings.SPREADSHEET_ID)

def inspect_meta_phone_number():
    print("\n=== META GRAPH API PHONE NUMBER INFO ===")
    url = f"https://graph.facebook.com/v19.0/{settings.WHATSAPP_PHONE_NUMBER_ID}"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    try:
        r = httpx.get(url, headers=headers, timeout=10)
        print("Phone info response:", r.status_code, r.text)
    except Exception as e:
        print("Phone info failed:", e)

def test_ngrok_webhook_get():
    print("\n=== NGROK WEBHOOK GET (VERIFICATION) TEST ===")
    url = "https://crave-pendant-moonscape.ngrok-free.dev/webhook"
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN,
        "hub.challenge": "test_challenge_9999",
    }
    try:
        r = httpx.get(url, params=params, headers={"ngrok-skip-browser-warning": "true"}, timeout=10)
        print(f"GET {url} -> Status: {r.status_code}, Body: {r.text}")
    except Exception as e:
        print(f"GET {url} failed: {e}")

def test_ngrok_webhook_post():
    print("\n=== NGROK WEBHOOK POST TEST ===")
    url = "https://crave-pendant-moonscape.ngrok-free.dev/webhook"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550284451",
                                "phone_number_id": settings.WHATSAPP_PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Soban"},
                                    "wa_id": "923129214688",
                                }
                            ],
                            "messages": [
                                {
                                    "from": "923129214688",
                                    "id": "wamid.TEST_NGROK_WEBHOOK_001",
                                    "timestamp": "1728312345",
                                    "text": {
                                        "body": "How much Ali has Paid?"
                                    },
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    try:
        r = httpx.post(url, json=payload, headers={"ngrok-skip-browser-warning": "true"}, timeout=15)
        print(f"POST {url} -> Status: {r.status_code}, Body: {r.text}")
    except Exception as e:
        print(f"POST {url} failed: {e}")

def test_whatsapp_send(to_phone: str = "923129214688"):
    print(f"\n=== WHATSAPP SEND CHECK to {to_phone} ===")
    wa = get_whatsapp_client()
    res = wa.send_text(to_phone, "Diagnostic test message from bot.")
    print("send_text result:", res)

async def test_query_pipeline():
    print("\n=== PIPELINE QUERY TEST ===")
    test_text = "How much Ali has Paid?"
    msg = IncomingMessage(
        message_id="diag_123",
        group_id="923129214688",
        sender_phone="923129214688",
        sender_name="Ali",
        text=test_text,
        raw_type="text",
    )
    classified = await classify_message(test_text, sender_name="Ali")
    print("Classified:", classified)
    reply = await handle_query(msg, classified)
    print("Generated reply:\n", reply)

def simulate_webhook_post(domain: str = "http://localhost:8000"):
    print(f"\n=== SIMULATE INBOUND WEBHOOK POST to {domain} ===")
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550284451",
                                "phone_number_id": settings.WHATSAPP_PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Test User"},
                                    "wa_id": "923129214688",
                                }
                            ],
                            "messages": [
                                {
                                    "from": "923129214688",
                                    "id": "wamid.TEST_DIAG_001",
                                    "timestamp": "1728312345",
                                    "text": {
                                        "body": "How much Ali has Paid?"
                                    },
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    try:
        r = httpx.post(f"{domain}/webhook", json=payload, headers={"ngrok-skip-browser-warning": "true"}, timeout=15)
        print(f"POST {domain}/webhook -> Status: {r.status_code}, Body: {r.text}")
    except Exception as e:
        print(f"POST {domain}/webhook failed: {e}")

from app.webhook.routes import _process_payload

async def test_direct_payload_dispatch():
    print("\n=== DIRECT _process_payload TEST ===")
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550284451",
                                "phone_number_id": settings.WHATSAPP_PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Soban"},
                                    "wa_id": "923129214688",
                                }
                            ],
                            "messages": [
                                {
                                    "from": "923129214688",
                                    "id": "wamid.HBgMOTIzMTI5MjE0Njg4FQIAERgSQTcxM0FGNzE5NEQwQUY4MTQ0AA==",
                                    "timestamp": "1728312345",
                                    "text": {
                                        "body": "How much Ali has Paid?"
                                    },
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    await _process_payload(payload)
    print("_process_payload completed successfully!")

async def main():
    test_config()
    inspect_meta_phone_number()
    test_ngrok_webhook_get()
    test_ngrok_webhook_post()

if __name__ == "__main__":
    asyncio.run(main())
