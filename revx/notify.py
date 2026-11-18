"""Telegram alerts. Send-only and best effort: a failed alert never affects trading.

Settings come from the environment (read from .env at runtime, never committed):
  REVX_TELEGRAM_TOKEN    bot token (may be the same bot that OrangePolyBot uses)
  REVX_TELEGRAM_CHAT_ID  chat to send to
Without both, every call is a silent no-op. This module only sends; it never polls
getUpdates, so sharing a token with another bot's command listener causes no conflict.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 8
IP_LOOKUP_URL = "https://api.ipify.org"   # IPv4 only, plain text
WHITELIST_MARKER = "whitelisted IP"       # text of the exchange's 401 for a non-whitelisted address


def send(text: str) -> bool:
    """Send one message. Returns True if Telegram accepted it."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        token = os.environ.get("REVX_TELEGRAM_TOKEN")
        chat_id = os.environ.get("REVX_TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return False
        resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             data={"chat_id": chat_id, "text": text}, timeout=TIMEOUT_SECONDS)
        return resp.ok
    except Exception as exc:   # alerts must never break the bot
        log.warning("telegram alert failed: %s", type(exc).__name__)
        return False


def public_ip() -> str | None:
    """The address the exchange sees for this machine, or None if the lookup fails."""
    try:
        resp = requests.get(IP_LOOKUP_URL, timeout=TIMEOUT_SECONDS)
        return resp.text.strip() if resp.ok else None
    except Exception:
        return None


def halt_alert(reason: str) -> bool:
    """Tell the user the bot halted. A whitelist rejection gets the fix spelled out."""
    if WHITELIST_MARKER in reason:
        ip = public_ip()
        text = ("revx bot HALTED: the exchange rejected this server's IP address.\n"
                f"Current public IP: {ip or 'lookup failed, run `curl -4 ifconfig.me` on the server'}\n"
                "Fix: add that IP to the API key's whitelist in Revolut X, then run "
                "`main.py resume` and restart the service.\n"
                "Late? The IP may have changed again. Tap /ip for the address right now "
                "(answered by OrangePolyBot on the same Pi).")
    else:
        text = f"revx bot HALTED: {reason}\nFix the cause, then run `main.py resume`."
    return send(text)
