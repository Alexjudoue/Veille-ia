"""Tests sans réseau ni clé API : on simule le LLM."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from veille.analyze import analyze
from veille.brief import render_markdown, select, synthesize
from veille.collect import deduplicate, filter_recent
from veille.memory import Memory
from veille.models import Article

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)


def art(i, title, hours_ago=1, theme="marches"):
    return Article(id=f"id{i}", title=title, url=f"https://ex.com/{i}", source="Test",
                   published=NOW - timedelta(hours=hours_ago), summary="extrait", feed_theme=theme)


class FakeLLM:
    """Renvoie des réponses prévisibles, y compris un index halluciné à ignorer."""
    def structured(self, system, prompt, tool_name, schema, **kw):
        if tool_name == "enregistrer_analyse":
            return {"articles": [
                {"index": 0, "theme": "marches", "relevance": 9, "summary_fr": "La BCE baisse ses taux.",
                 "why_it_matters": "Impact sur le crédit.", "entities": ["BCE"]},
                {"index": 1, "theme": "hors_sujet", "relevance": 1, "summary_fr": "x",
                 "why_it_matters": "x", "entities": []},
                {"index": 2, "theme": "ia_tech", "relevance": 4, "summary_fr": "Mineur.",
                 "why_it_matters": "Peu.", "entities": []},
                {"index": 42, "theme": "marches", "relevance": 10, "summary_fr": "Halluciné",
                 "why_it_matters": "x", "entities": []},
            ]}
        return {"key_points": ["La BCE baisse ses taux."], "connection": "", "interview_question": "Pourquoi ?"}


def test_filter_recent():
    arts = [art(1, "récent", 2), art(2, "vieux", 50)]
    assert [a.title for a in filter_recent(arts, 30, now=NOW)] == ["récent"]


def test_deduplicate_similar_titles():
    arts = [art(1, "La BCE baisse ses taux de 0,25 point"),
            art(2, "La BCE baisse ses taux de 0,25 point !"),
            art(3, "Nvidia publie ses résultats trimestriels")]
    assert len(deduplicate(arts)) == 2


def test_analyze_ignores_bad_output_and_brief_renders():
    arts = [art(0, "BCE"), art(1, "Pub"), art(2, "Startup")]
    analyzed = analyze(arts, FakeLLM(), CONFIG)
    assert [a.article.id for a in analyzed] == ["id0", "id2"]  # hors_sujet et index 42 écartés

    selected = select(analyzed, CONFIG)
    assert len(selected["marches"]) == 1 and selected["ia_tech"] == []  # 4/10 < seuil

    stats = dict(collected=3, analyzed=2, selected=1, calls=2, input_tokens=1000, output_tokens=200)
    md = render_markdown(selected, synthesize(selected, FakeLLM(), CONFIG), CONFIG, NOW, stats)
    assert "## À retenir" in md and "La BCE baisse ses taux." in md


def test_memory(tmp_path):
    m = Memory(tmp_path / "t.db")
    a, b = art(1, "a"), art(2, "b")
    m.mark_seen([a])
    assert m.filter_new([a, b]) == [b]


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title>
<item><title>La BCE maintient ses taux - Les Echos</title><link>https://ex.com/bce</link>
<pubDate>Wed, 15 Oct 2026 07:00:00 GMT</pubDate><source url="https://lesechos.fr">Les Echos</source>
<description>&lt;a href="x"&gt;La BCE&lt;/a&gt; laisse ses taux inchang&#233;s</description></item>
</channel></rss>"""


def test_fetch_feed_parses_google_news(monkeypatch):
    import veille.collect as c

    class Resp:
        content = RSS
        def raise_for_status(self): pass

    monkeypatch.setattr(c.requests, "get", lambda *a, **k: Resp())
    [a] = c.fetch_feed("https://news.google.com/rss", "marches", 5, 10)
    assert a.title == "La BCE maintient ses taux"      # suffixe " - Les Echos" retiré
    assert a.source == "Les Echos"
    assert a.summary == "La BCE laisse ses taux inchangés"  # HTML nettoyé


def test_billing_error_stops_everything():
    import pytest

    class BillingError(Exception):
        status_code = 400

    class BrokeLLM:
        def structured(self, *a, **k):
            raise BillingError("Your credit balance is too low")

    with pytest.raises(BillingError):
        analyze([art(0, "BCE")], BrokeLLM(), CONFIG)


def test_single_article_synthesis_is_not_padded():
    class ChattyLLM:
        def structured(self, system, prompt, tool_name, schema, **kw):
            assert schema["properties"]["key_points"]["maxItems"] == 1
            return {"key_points": ["Vrai point", "Remplissage", "Encore du remplissage"],
                    "connection": "Un seul article, pas de lien.", "interview_question": "Q ?"}

    analyzed = analyze([art(0, "BCE")], FakeLLM(), CONFIG)
    selected = select(analyzed, CONFIG)
    syn = synthesize(selected, ChattyLLM(), CONFIG)
    assert syn["key_points"] == ["Vrai point"] and syn["connection"] == ""

    stats = dict(collected=1, analyzed=1, selected=1, calls=2, input_tokens=4687, output_tokens=2875)
    md = render_markdown(selected, syn, CONFIG, NOW, stats)
    assert "*1 article collecté, 1 analysé, 1 retenu. 2 appels au modèle, 4 687 tokens" in md
    assert "fil rouge" not in md


def test_cluster_events_merges_same_story():
    from veille.brief import cluster_events
    from veille.models import AnalyzedArticle

    def an(i, title, rel):
        return AnalyzedArticle(article=art(i, title), theme="ia_tech", relevance=rel,
                               summary_fr="r" * rel, why_it_matters="w")

    items = [an(0, "CoreWeave chute", 8), an(1, "L'Europe ouvre en baisse après CoreWeave", 8),
             an(2, "Mistral rachète une startup", 7), an(3, "CoreWeave : achetez la baisse", 7)]

    class ClusterLLM:
        def structured(self, system, prompt, tool_name, schema, **kw):
            return {"groups": [{"event": "Chute de CoreWeave", "indices": [3, 0, 1, 99]}]}

    kept = cluster_events(items, ClusterLLM())
    assert [k.article.id for k in kept] == ["id0", "id2"]
    assert [r["url"] for r in kept[0].related] == ["https://ex.com/1", "https://ex.com/3"]

    md = render_markdown({"ia_tech": kept[:1], "marches": [], "banques": []},
                         {"key_points": []}, CONFIG, NOW,
                         dict(collected=4, analyzed=4, selected=2, calls=3, input_tokens=1, output_tokens=1))
    assert "*Aussi couvert par : [Test](https://ex.com/1), [Test](https://ex.com/3)*" in md
