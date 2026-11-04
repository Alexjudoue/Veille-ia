"""Tests de l'envoi par e-mail du PDF, sans réseau : le serveur SMTP est simulé."""
import smtplib
import sys
from pathlib import Path

import main as main_module
import veille.deliver as d

PDF = b"%PDF-1.7 faux pdf pour le test"
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


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


def test_mail_is_two_lines_with_pdf_attached(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(d.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_SENDER", "moi@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")  # espaces tolérés
    monkeypatch.delenv("EMAIL_TO", raising=False)

    assert d.send_email(PDF, "2026-11-04.pdf", "Veille du 04/11/2026") is True
    msg = FakeSMTP.sent[0]
    assert msg["Subject"] == "Veille du 04/11/2026"
    assert msg["To"] == "moi@gmail.com" and msg["Bcc"] is None
    body = msg.get_body(("plain",)).get_content()
    assert "en pièce jointe" in body and len(body.strip().splitlines()) <= 3   # court, sans le contenu du brief
    [att] = list(msg.iter_attachments())
    assert att.get_content_type() == "application/pdf" and att.get_filename() == "2026-11-04.pdf"
    assert att.get_content() == PDF


def test_several_recipients_go_in_bcc(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(d.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_SENDER", "moi@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("EMAIL_TO", "a@x.fr, b@y.fr; c@z.fr")

    assert d.send_email(PDF, "b.pdf", "Test") is True
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "moi@gmail.com"
    assert msg["Bcc"] == "a@x.fr, b@y.fr, c@z.fr"


def test_missing_config_or_bad_password_never_crashes(monkeypatch):
    monkeypatch.setattr(d.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.delenv("EMAIL_SENDER", raising=False)
    monkeypatch.delenv("EMAIL_APP_PASSWORD", raising=False)
    assert d.send_email(PDF, "b.pdf", "Test") is False          # non configuré : ignoré

    monkeypatch.setenv("EMAIL_SENDER", "moi@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "mauvais")
    assert d.send_email(PDF, "b.pdf", "Test") is False          # refus Gmail : erreur claire, pas de crash


def test_test_email_sends_the_latest_pdf(tmp_path, monkeypatch):
    (tmp_path / "briefs").mkdir()
    (tmp_path / "briefs" / "2026-11-03.pdf").write_bytes(b"ancien")
    (tmp_path / "briefs" / "2026-11-04.pdf").write_bytes(PDF)
    (tmp_path / "briefs" / "2026-11-05.json").write_text("{}")        # sans PDF : ignoré
    sent = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "send_email",
                        lambda pdf, name, subject: sent.append((pdf, name, subject)) or True)
    monkeypatch.setattr(sys, "argv", ["main.py", "--config", str(CONFIG_PATH), "--test-email"])
    try:
        main_module.main()
    except SystemExit as e:
        assert e.code == 0
    assert sent == [(PDF, "2026-11-04.pdf", "[Test] Veille du 04/11/2026")]
