"""Tests des outils de l'agent, sans réseau : on simule les réponses HTTP."""
from datetime import datetime, timezone

import pytest

import veille.tools as t
from veille.memory import Memory
from veille.models import AnalyzedArticle, Article


class Resp:
    def __init__(self, text="", status=200, content=b""):
        self.text, self.status_code, self.content = text, status, content
        self.ok = status < 400

    def raise_for_status(self):
        if not self.ok:
            raise t.requests.HTTPError(f"HTTP {self.status_code}")


GN_URL = "https://news.google.com/rss/articles/CBMiABC123?oc=5"
GN_PAGE = '<div jscontroller="x" data-n-a-id="CBMiABC123" data-n-a-ts="1792101178" data-n-a-sg="SIG_42">'
# Réponse réelle de batchexecute, enregistrée le 15/10/2026 (adresse raccourcie)
GN_RESP = (')]}\'\n\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://www.exemple.fr/article\\",1]",'
           'null,null,null,"generic"],["di",18],["af.httprm",17,"-759",41]]')


def test_decode_google_news(monkeypatch):
    sent = {}
    monkeypatch.setattr(t.requests, "get", lambda url, **k: Resp(GN_PAGE))

    def fake_post(url, data, **k):
        sent["body"] = data
        return Resp(GN_RESP)

    monkeypatch.setattr(t.requests, "post", fake_post)
    assert t.decode_google_news(GN_URL) == "https://www.exemple.fr/article"
    assert "SIG_42" in sent["body"] and "CBMiABC123" in sent["body"]  # signature bien transmise


def test_decode_google_news_without_signature(monkeypatch):
    monkeypatch.setattr(t.requests, "get", lambda url, **k: Resp("<html>consentement</html>"))
    with pytest.raises(t.ToolError, match="non résolu"):
        t.decode_google_news(GN_URL)


def test_read_article_paywall(monkeypatch):
    monkeypatch.setattr(t.requests, "get", lambda url, **k: Resp("", status=403))
    with pytest.raises(t.ToolError, match="403"):
        t.read_article("https://www.lesechos.fr/x")


def test_read_article_extracts_and_truncates(monkeypatch):
    para = "La Banque centrale européenne a laissé ses taux inchangés à 2 %. " * 40
    html = f"<html><body><nav>Menu</nav><article><h1>BCE</h1><p>{para}</p></article></body></html>"
    monkeypatch.setattr(t.requests, "get", lambda url, **k: Resp(html))
    real, text = t.read_article("https://www.exemple.fr/bce", max_chars=500)
    assert real == "https://www.exemple.fr/bce"
    assert len(text) == 500 and "taux inchangés" in text


def test_read_article_too_short(monkeypatch):
    monkeypatch.setattr(t.requests, "get", lambda url, **k: Resp("<html><p>Acceptez les cookies</p></html>"))
    with pytest.raises(t.ToolError, match="trop court"):
        t.read_article("https://www.exemple.fr/x")


def test_search_news(monkeypatch):
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Lagarde vers Davos - Les Echos</title><link>https://news.google.com/rss/articles/A</link>
    <pubDate>Wed, 15 Oct 2026 07:00:00 GMT</pubDate><source url="https://lesechos.fr">Les Echos</source></item>
    </channel></rss>"""
    seen = {}

    def fake_get(url, **k):
        seen["url"] = url
        return Resp(content=rss)

    monkeypatch.setattr(t.requests, "get", fake_get)
    [r] = t.search_news("Lagarde Davos", lang="en")
    assert r == {"title": "Lagarde vers Davos", "source": "Les Echos",
                 "url": "https://news.google.com/rss/articles/A", "date": "15/10/2026"}
    assert "when%3A7d" in seen["url"] and "ceid=US:en" in seen["url"]


def test_memory_search_ignores_today(tmp_path):
    m = Memory(tmp_path / "t.db")

    def an(i, title, entities):
        a = Article(id=f"id{i}", title=title, url=f"https://ex.com/{i}", source="Test",
                    published=datetime(2026, 10, 1, tzinfo=timezone.utc))
        return AnalyzedArticle(article=a, theme="marches", relevance=8,
                               summary_fr="résumé", why_it_matters="w", entities=entities)

    m.save_analyzed([an(1, "La BCE baisse ses taux", ["BCE"])], "2026-10-14")
    m.save_analyzed([an(2, "Nvidia publie ses résultats", ["Nvidia"])], "2026-10-14")
    m.save_analyzed([an(3, "La BCE maintient ses taux", ["BCE", "Lagarde"])], "2026-10-15")

    results = m.search("BCE Lagarde taux", before_date="2026-10-15")
    assert [r["title"] for r in results] == ["La BCE baisse ses taux"]  # pas l'article du jour
    assert m.search("de la", before_date="2026-10-15") == []          # mots trop courts
