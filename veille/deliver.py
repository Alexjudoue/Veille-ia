"""Étape 4 (optionnelle) : envoi du brief sur Telegram ou par e-mail."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

import markdown
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
# Envoi via le serveur SMTP de Gmail, avec un « mot de passe d'application »
# (pas ton vrai mot de passe Google : un code de 16 lettres dédié à ce script,
# révocable à tout moment). Aucun service tiers, aucune bibliothèque payante.

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL direct

# Styles en ligne : les clients mail (Gmail en tête) ignorent les feuilles de style
# externes et une partie des balises <style>. Sobre, lisible sur téléphone.
EMAIL_CSS = """
body { margin:0; padding:0; background:#f4f4f5; }
.wrap { max-width:680px; margin:0 auto; padding:24px 16px; background:#ffffff;
        font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
        font-size:15px; line-height:1.55; color:#1f2328; }
h1 { font-size:22px; margin:0 0 16px; }
h2 { font-size:18px; margin:28px 0 8px; padding-bottom:4px; border-bottom:2px solid #e5e7eb; }
h3 { font-size:15px; margin:18px 0 4px; }
a { color:#1a56db; }
blockquote { margin:8px 0; padding:6px 12px; border-left:3px solid #1a56db;
             background:#f3f6fc; color:#374151; }
em { color:#6b7280; }
hr { border:none; border-top:1px solid #e5e7eb; margin:24px 0; }
"""


def markdown_to_html(md_text: str) -> str:
    body = markdown.markdown(md_text, extensions=["extra", "sane_lists"])
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<style>{EMAIL_CSS}</style></head>"
            f"<body><div class='wrap'>{body}</div></body></html>")


def _recipients(raw: str) -> list[str]:
    return [r.strip() for r in raw.replace(";", ",").split(",") if r.strip()]


def build_email(md_text: str, subject: str, sender: str, recipients: list[str]) -> EmailMessage:
    """Construit un mail avec deux versions : texte brut (le Markdown) et HTML."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Veille IA <{sender}>"
    # Plusieurs destinataires : copie cachée, pour ne pas exposer les adresses
    # de chacun (utile le jour où le brief part aux membres d'INSA Finance).
    msg["To"] = sender if len(recipients) > 1 else recipients[0]
    if len(recipients) > 1:
        msg["Bcc"] = ", ".join(recipients)
    msg.set_content(md_text)
    msg.add_alternative(markdown_to_html(md_text), subtype="html")
    return msg


def send_email(md_text: str, subject: str) -> bool:
    sender = os.environ.get("EMAIL_SENDER", "").strip()
    password = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "")
    recipients = _recipients(os.environ.get("EMAIL_TO", "")) or ([sender] if sender else [])
    if not sender or not password:
        log.info("E-mail non configuré (EMAIL_SENDER / EMAIL_APP_PASSWORD) : envoi ignoré.")
        return False

    msg = build_email(md_text, subject, sender, recipients)
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
