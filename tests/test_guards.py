"""Tests des garde-fous contre les défauts des petits modèles (sans réseau)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

import veille.agent as agent
from veille import llm as L
from veille.analyze import analyze
from veille.brief import synthesize
from veille.guards import has_placeholder
from veille.models import Article

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("text", [
    "le taux du 10-year Treasury s'élève désormais à X%, et celui du 30-year à Y%",
    "(valeurs non précisées dans l'extrait)",
    "Hausse de Y points de base", "un prêt de $X", "montant : [à compléter]",
])
def test_placeholders_detected(text):
    assert has_placeholder(text)


@pytest.mark.parametrize("text", [
    "Le rendement de l'OAT dépasse 5 %, un niveau inédit depuis 2002.",
    "Apollo a prêté 350 milliards de dollars ; Xavier Niel (Iliad) réagit.",
    "L'indice S&P 500 gagne 1,2 %", "Le groupe X (ex-Twitter) licencie", "",
])
def test_normal_text_not_flagged(text):
    assert not has_placeholder(text)
    assert not has_placeholder(None)


def arts(n):
    return [Article(id=f"a{i}", title=f"Titre {i}", url=f"https://a.fr/{i}", source="s",
                    published=datetime(2026, 11, 4, tzinfo=timezone.utc)) for i in range(n)]


def item(i, summary="Résumé factuel."):
    return {"index": i, "theme": "marches", "relevance": 8, "summary_fr": summary,
            "why_it_matters": "Important.", "entities": []}


def test_batch_size_per_provider():
    assert L.batch_size_for(CONFIG) == CONFIG["llm"]["models"]["mistral"]["batch_size"] == 8
    claude = {"llm": {**CONFIG["llm"], "provider": "anthropic"}}
    assert L.batch_size_for(claude) == CONFIG["llm"]["batch_size"]


def test_invalid_batch_is_split_in_two_instead_of_lost():
    calls = []

    class Picky:
        def structured(self, system, prompt, tool_name, schema, **kw):
            n = prompt.count("Source :")        # nombre d articles du lot
            calls.append(n)
            if n > 2:
                raise RuntimeError("Le modèle n'a pas renvoyé de résultat structuré valide")
            return {"articles": [item(i) for i in range(n)]}

    out = analyze(arts(4), Picky(), {**CONFIG, "llm": {**CONFIG["llm"], "provider": "anthropic", "batch_size": 4}})
    assert len(out) == 4 and calls == [4, 2, 2]


def test_summary_with_placeholder_is_dropped():
    class Llm:
        def structured(self, *a, **k):
            return {"articles": [item(0, "Le taux s'élève à X%, celui du 30 ans à Y%."), item(1)]}

    out = analyze(arts(2), Llm(), CONFIG)
    assert [a.article.id for a in out] == ["a1"]


def test_fatal_error_still_stops_everything():
    class Err(Exception):
        status_code = 401

    class Llm:
        def structured(self, *a, **k):
            raise Err("clé")

    with pytest.raises(Err):
        analyze(arts(4), Llm(), CONFIG)


def test_agent_falls_back_to_v1_on_placeholder(monkeypatch):
    from test_agent import ARTICLE_TEXT, TODAY, FakeLLM, FakeMemory, event, reply, submit, use
    monkeypatch.setattr(agent.tools, "read_article", lambda url, max_chars: (url, ARTICLE_TEXT))
    llm = FakeLLM(reply(use("r", "read_article", url="https://exemple.fr/bce")),
                  reply(submit(summary="Le taux atteint X% (valeurs non précisées).")))
    dive, trace = agent.investigate(event(), llm, FakeMemory(), CONFIG, TODAY)
    assert dive is None and trace["status"] == "repli_v1" and "trous" in trace["reason"]


def test_synthesis_drops_placeholder_parts():
    from test_agent import event
    class Llm:
        def structured(self, *a, **k):
            return {"key_points": ["Taux à X%.", "La BCE maintient ses taux."],
                    "connection": "Lien vers Y%.", "interview_question": "Pourquoi ?"}

    out = synthesize({"marches": [event()], "banques": [], "ia_tech": []}, Llm(), CONFIG)
    assert out["key_points"] == ["La BCE maintient ses taux."]
    assert out["connection"] == "" and out["interview_question"] == "Pourquoi ?"


@pytest.mark.parametrize("text", [
    "le bénéfice net a plus que doublé, passant de X à plus de 2X",
    "un résultat de Y millions", "une hausse de 2X en un an",
])
def test_bare_variable_placeholders_detected(text):
    assert has_placeholder(text)


@pytest.mark.parametrize("text", ["Il y a de quoi s'inquiéter", "Passage de x à y dans la formule", "Le groupe X (ex-Twitter)", "iPhone 15 Pro Max 2x"])
def test_french_words_are_not_flagged(text):
    assert not has_placeholder(text)
