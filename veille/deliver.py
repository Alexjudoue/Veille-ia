"""Étape 4 (optionnelle) : envoi du brief sur Telegram."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("Telegram non configuré : envoi ignoré.")
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=15,
    )
    if not resp.ok:
        log.error("Échec de l'envoi Telegram : %s", resp.text[:200])
        return False
    log.info("Brief envoyé sur Telegram.")
    return True
