"""Étape 4 (optionnelle) : envoi du brief sur Telegram ou par e-mail."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

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


# --- E-mail -----------------------------------------------------------------
# Le mail ne sert qu'à transporter le PDF du brief (une page A4).
# Envoi via le serveur SMTP de Gmail, avec un « mot de passe d'application »
# (pas ton vrai mot de passe Google : un code de 16 lettres dédié à ce script,
# révocable à tout moment). Aucun service tiers, aucune bibliothèque payante.

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL direct

def _recipients(raw: str) -> list[str]:
    return [r.strip() for r in raw.replace(";", ",").split(",") if r.strip()]


# Corps du mail : très court, le contenu est dans le PDF joint
EMAIL_BODY = "Bonjour,\n\nTon brief du jour est en pièce jointe (PDF, une page).\n"


def build_email(pdf: bytes, filename: str, subject: str, sender: str,
                recipients: list[str]) -> EmailMessage:
    """Mail de deux lignes avec le brief en PDF en pièce jointe."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Veille IA <{sender}>"
    # Plusieurs destinataires : copie cachée, pour ne pas exposer les adresses
    # de chacun (utile le jour où le brief part aux membres d'INSA Finance).
    msg["To"] = sender if len(recipients) > 1 else recipients[0]
    if len(recipients) > 1:
        msg["Bcc"] = ", ".join(recipients)
    msg.set_content(EMAIL_BODY)
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)
    return msg


def send_email(pdf: bytes, filename: str, subject: str) -> bool:
    sender = os.environ.get("EMAIL_SENDER", "").strip()
    password = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "")
    recipients = _recipients(os.environ.get("EMAIL_TO", "")) or ([sender] if sender else [])
    if not sender or not password:
        log.info("E-mail non configuré (EMAIL_SENDER / EMAIL_APP_PASSWORD) : envoi ignoré.")
        return False

    msg = build_email(pdf, filename, subject, sender, recipients)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(),
                              timeout=30) as server:
            server.login(sender, password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        log.error("Gmail a refusé la connexion : vérifie EMAIL_SENDER et le mot de passe "
                  "d'application (validation en 2 étapes activée ?).")
        return False
    except (smtplib.SMTPException, OSError) as e:
        log.error("Échec de l'envoi de l'e-mail : %s", e)
        return False
    log.info("Brief envoyé par e-mail à %d destinataire(s).", len(recipients))
    return True
