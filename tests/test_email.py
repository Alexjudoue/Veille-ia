"""Tests de l'envoi par e-mail, sans réseau : le serveur SMTP est simulé."""
import smtplib

import veille.deliver as d

BRIEF = "# Veille du 18/10\n\n## À retenir\n\n1. La BCE baisse ses taux.\n\n> **Pourquoi :** crédit"


class FakeSMTP:
    sent = []

    def __init__(self, host, port, context=None, timeout=None):
        assert (host, port) == ("smtp.gmail.com", 465)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def login(self, user, password):
        if password != "abcdefghijklmnop":
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_markdown_becomes_styled_html():
    html = d.markdown_to_html(BRIEF)
    assert "<h1>Veille du 18/10</h1>" in html and "<blockquote>" in html
    assert "<ol>" in html and "max-width:680px" in html


def test_send_email_to_self_by_default(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(d.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_SENDER", "moi@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")  # espaces tolérés
    monkeypatch.delenv("EMAIL_TO", raising=False)

    assert d.send_email(BRIEF, "Test") is True
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "moi@gmail.com" and msg["Bcc"] is None
    assert msg.get_body(("html",)).get_content().count("<h2>") == 1
    assert "La BCE baisse" in msg.get_body(("plain",)).get_content()


def test_several_recipients_go_in_bcc(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(d.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_SENDER", "moi@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("EMAIL_TO", "a@x.fr, b@y.fr; c@z.fr")

    assert d.send_email(BRIEF, "Test") is True
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "moi@gmail.com"
    assert msg["Bcc"] == "a@x.fr, b@y.fr, c@z.fr"


def test_missing_config_or_bad_password_never_crashes(monkeypatch):
    monkeypatch.setattr(d.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.delenv("EMAIL_SENDER", raising=False)
    monkeypatch.delenv("EMAIL_APP_PASSWORD", raising=False)
    assert d.send_email(BRIEF, "Test") is False          # non configuré : ignoré

    monkeypatch.setenv("EMAIL_SENDER", "moi@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "mauvais")
    assert d.send_email(BRIEF, "Test") is False          # refus Gmail : erreur claire, pas de crash
