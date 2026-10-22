"""Tests de l'intégration V2 : rendu du brief et exécution de main.py de bout en bout,
sans réseau ni clé (faux LLM, collecte et lecture d'article simulées)."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import main as main_module
import veille.agent as agent
import veille.llm as llm_module
from veille.brief import render_markdown, synthesize
from veille.models import AnalyzedArticle, Article, DeepDive

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
CONFIG = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
URL = "https://www.exemple.fr/bce"
TEXT = "La BCE a laissé ses taux à 2,5 % ; l'inflation atteint 3,1 % en zone euro. " * 5


def article(i, title, url=None):
    return Article(id=f"id{i}", title=title, url=url or f"https://exemple.fr/{i}", source="Test",
                   published=NOW, summary="extrait", feed_theme="marches")


def analyzed(i, title, rel, url=None):
    return AnalyzedArticle(article=article(i, title, url), theme="marches", relevance=rel,
                           summary_fr=f"Résumé v1 de {title}", why_it_matters="Important.")


STATS = dict(collected=2, analyzed=2, selected=2, calls=3, input_tokens=1000, output_tokens=200)


def make_dive(item):
    return DeepDive(item=item, summary="Résumé enrichi.", confidence="élevée",
                    key_figures=[{"value": "2,5 %", "label": "taux", "source_url": URL}],
                    context="Contexte.", what_changed="Hausse depuis hier.", sources_read=[URL],
                    stats={"articles_read": 1, "memory_searches": 1, "news_searches": 0,
                           "calls": 3, "input_tokens": 500, "output_tokens": 50})


def test_render_puts_deep_dives_first_without_duplicates():
    a, b = analyzed(1, "BCE", rel=9), analyzed(2, "Nvidia", rel=7)
    selected = {"marches": [a, b], "banques": [], "ia_tech": []}
    stats = {**STATS, "agent": dict(events=1, deep_dives=1, calls=3, input_tokens=500, output_tokens=50)}
    md = render_markdown(selected, {"key_points": []}, CONFIG, NOW, stats, [make_dive(a)])

    assert md.index("## Analyses approfondies") < md.index("## Marchés financiers")
    assert "Résumé enrichi." in md and "**2,5 %** : taux" in md
    assert "**Ce qui change depuis les jours précédents :** Hausse depuis hier." in md
    assert "*Sources lues : [exemple.fr](https://www.exemple.fr/bce)*" in md
    assert "*Agent : 1 article lu, 1 recherche en mémoire, 3 appels au modèle*" in md
    assert md.count("### [BCE]") == 1 and "### [Nvidia]" in md       # BCE n'est plus dans sa section
    assert "Dont agent d'approfondissement : 1 analyse sur 1 événement, 3 appels au modèle" in md
    assert "3 appels au modèle, 1 000 tokens en entrée" in md          # coût du pipeline inchangé


def test_render_without_dives_is_unchanged():
    selected = {"marches": [analyzed(1, "BCE", 9)], "banques": [], "ia_tech": []}
    md = render_markdown(selected, {"key_points": []}, CONFIG, NOW, STATS)
    assert "Analyses approfondies" not in md and "Agent" not in md


def test_synthesis_uses_enriched_summary():
    a = analyzed(1, "BCE", 9)
    seen = {}

    class LLM:
        def structured(self, system, prompt, *a, **k):
            seen["prompt"] = prompt
            return {"key_points": ["x"], "connection": "", "interview_question": "q"}

    synthesize({"marches": [a], "banques": [], "ia_tech": []}, LLM(), CONFIG, [make_dive(a)])
    assert "Résumé enrichi." in seen["prompt"] and "Résumé v1" not in seen["prompt"]


# --- main.py de bout en bout ---------------------------------------------------------

class StubLLM:
    """Remplace veille.llm.LLM : répond à l'analyse, au regroupement, à la synthèse et à l'agent."""
    def __init__(self, model, effort=None):
        self.usage, self.agent_usage = llm_module.Usage(), llm_module.Usage()
        self.agent_steps = [
            [SimpleNamespace(type="tool_use", id="a", name="read_article", input={"url": "https://exemple.fr/0"})],
            [SimpleNamespace(type="tool_use", id="b", name="submit_analysis", input=dict(
                summary="Analyse approfondie.", key_figures=[{"value": "2,5 %", "label": "taux", "source_url": URL}],
                context="Contexte.", what_changed="", confidence="élevée", sources_read=[URL]))],
        ]

    def structured(self, system, prompt, tool_name, schema, **kw):
        self.usage.add(100, 20)
        if tool_name == "enregistrer_analyse":
            return {"articles": [
                {"index": 0, "theme": "marches", "relevance": 9, "summary_fr": "BCE v1",
                 "why_it_matters": "Important.", "entities": ["BCE"]},
                {"index": 1, "theme": "marches", "relevance": 4, "summary_fr": "Autre v1",   # sous le seuil
                 "why_it_matters": "Utile.", "entities": []}]}
        if tool_name == "regrouper_evenements":
            return {"groups": []}
        return {"key_points": ["La BCE maintient ses taux."], "connection": "", "interview_question": "Pourquoi ?"}

    def chat(self, system, messages, tools, tool_choice=None, **kw):
        self.agent_usage.add(300, 30)
        return SimpleNamespace(content=self.agent_steps.pop(0), stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=300, output_tokens=30))


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                     # briefs/ et data/ sont créés ici, pas dans le dépôt
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(main_module, "collect", lambda config: [
        article(0, "La BCE maintient ses taux", "https://exemple.fr/0"), article(1, "Autre sujet")])
    monkeypatch.setattr(llm_module, "LLM", StubLLM)
    monkeypatch.setattr(agent.tools, "read_article", lambda url, max_chars: (URL, TEXT))

    def run(*flags):
        monkeypatch.setattr(sys, "argv", ["main.py", "--config", str(CONFIG_PATH), *flags])
        main_module.main()
        day = f"{datetime.now(timezone.utc):%Y-%m-%d}"
        return tmp_path / "briefs", day
    return run


def test_main_runs_agent_and_writes_trace(run_main):
    briefs, day = run_main()
    md = (briefs / f"{day}.md").read_text(encoding="utf-8")
    assert "## Analyses approfondies" in md and "Analyse approfondie." in md
    assert "Agent : 1 article lu, 2 appels au modèle" in md

    trace = json.loads((briefs / f"{day}.trace.json").read_text(encoding="utf-8"))
    [event] = trace["events"]
    assert event["status"] == "ok" and event["steps"][0]["tool"] == "read_article"
    assert event["model_calls"] == 2 and event["input_tokens"] == 600

    data = json.loads((briefs / f"{day}.json").read_text(encoding="utf-8"))
    assert data["stats"]["agent"]["calls"] == 2 and data["stats"]["calls"] == 2   # le pipeline (analyse + synthèse) est compté à part
    assert len(data["deep_dives"]) == 1


def test_main_no_agent_restores_v1_behaviour(run_main):
    briefs, day = run_main("--no-agent")
    md = (briefs / f"{day}.md").read_text(encoding="utf-8")
    assert "Analyses approfondies" not in md and "BCE v1" in md
    assert not (briefs / f"{day}.trace.json").exists()
    assert "agent" not in json.loads((briefs / f"{day}.json").read_text(encoding="utf-8"))["stats"]
