"""Tests du client Mistral et du choix du fournisseur, sans réseau ni clé :
un faux SDK rejoue des réponses écrites à l'avance."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import veille.agent as agent
from veille import llm as L

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
SCHEMA = {"type": "object",
          "properties": {"n": {"type": "integer"}, "mot": {"type": "string"}},
          "required": ["n", "mot"], "additionalProperties": False}


# --- doubles de test ----------------------------------------------------------------------------

class ApiError(Exception):
    """Erreur de l'API : porte un status_code, comme celles du SDK."""
    def __init__(self, status, headers=None):
        super().__init__(f"HTTP {status}")
        self.status_code, self.headers, self.message = status, headers or {}, f"HTTP {status}"


def completion(args=None, name="enregistrer", call_id="c1", text="", finish="tool_calls", calls=None):
    """Réponse du SDK : un appel d'outil (arguments en chaîne JSON) et/ou du texte."""
    if calls is None:
        calls = [SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(
            name=name, arguments=args if isinstance(args, str) else json.dumps(args)))] if args is not None else None
    msg = SimpleNamespace(content=text, tool_calls=calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


class FakeClient:
    """`client.chat.complete(...)` rejoue des réponses (ou lève des exceptions) dans l'ordre."""
    def __init__(self, *items):
        self.items, self.calls = list(items), []
        self.chat = SimpleNamespace(complete=self._complete)

    def _complete(self, **kw):
        self.calls.append(kw)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make(*items, **kw):
    sleeps, now = [], [1_000.0]

    def sleep(s):               # fausse horloge : dormir fait avancer le temps, sans attendre
        sleeps.append(s)
        now[0] += s

    llm = L.MistralLLM("mistral-small-2603", client=FakeClient(*items), sleep=sleep,
                       clock=lambda: now[0], **kw)
    return llm, llm.client, sleeps


# --- choix du fournisseur -----------------------------------------------------------------------

def test_config_default_provider_and_models():
    assert L.provider_of(CONFIG) == "mistral"
    configured = CONFIG["llm"]["models"]["mistral"]          # le modèle se règle dans config.yaml
    assert L.model_for(CONFIG) == configured["model"] != ""
    assert L.model_for(CONFIG, "synthesis_model") == configured["synthesis_model"]


def test_one_line_switches_to_claude():
    cfg = {"llm": {**CONFIG["llm"], "provider": "anthropic"}}
    assert L.model_for(cfg) == "claude-haiku-5-5"


def test_make_llm_picks_the_right_client(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert isinstance(L.make_llm(CONFIG), L.MistralLLM)
    cfg = {"llm": {**CONFIG["llm"], "provider": "anthropic"}}
    assert isinstance(L.make_llm(cfg), L.AnthropicLLM)
    with pytest.raises(ValueError):
        L.make_llm({"llm": {**CONFIG["llm"], "provider": "inconnu"}})


def test_missing_key_stops_with_a_clear_message(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MISTRAL_API_KEY"):
        L.make_llm(CONFIG)


def test_old_flat_config_still_works():
    assert L.model_for({"llm": {"model": "m", "synthesis_model": "s"}}) == "m"
    assert L.model_for({"llm": {"model": "m", "synthesis_model": "s"}}, "synthesis_model") == "s"


# --- sortie structurée ----------------------------------------------------------------------------

def test_structured_forces_the_function_and_validates():
    llm, client, _ = make(completion({"n": 3, "mot": "ok"}))
    assert llm.structured("sys", "prompt", "enregistrer", SCHEMA) == {"n": 3, "mot": "ok"}
    kw = client.calls[0]
    assert kw["tool_choice"] == {"type": "function", "function": {"name": "enregistrer"}}
    assert kw["tools"][0]["function"]["parameters"] == SCHEMA
    assert kw["messages"][0] == {"role": "system", "content": "sys"}
    assert (llm.usage.calls, llm.usage.input_tokens, llm.usage.output_tokens, llm.usage.cost_usd) == (1, 100, 20, 0.0)


def test_structured_retries_once_on_invalid_result():
    llm, client, _ = make(completion({"n": "trois", "mot": "ok"}), completion({"n": 3, "mot": "ok"}))
    assert llm.structured("s", "p", "enregistrer", SCHEMA)["n"] == 3
    retry = client.calls[1]["messages"]
    assert retry[-1]["role"] == "tool" and "invalide" in retry[-1]["content"]
    assert retry[-2]["tool_calls"][0]["id"] == "c1"


def test_structured_gives_up_after_two_invalid_results():
    llm, _, _ = make(completion("{pas du json"), completion({"mot": "ok"}))
    with pytest.raises(RuntimeError, match="valide"):
        llm.structured("s", "p", "enregistrer", SCHEMA)


def test_structured_accepts_json_in_plain_text():
    llm, _, _ = make(completion(text='```json\n{"n": 1, "mot": "a"}\n```', finish="stop"))
    assert llm.structured("s", "p", "enregistrer", SCHEMA) == {"n": 1, "mot": "a"}


# --- traduction des messages de l'agent ---------------------------------------------------------------

def use(call_id, name, **args):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def test_messages_translated_to_mistral_format():
    history = [
        {"role": "user", "content": "Approfondis"},
        {"role": "assistant", "content": [SimpleNamespace(type="text", text="Je lis."),
                                          use("t1", "read_article", url="https://a.fr"),
                                          use("t2", "search_memory", query="BCE")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "page"},
                                     {"type": "tool_result", "tool_use_id": "t2", "content": "Échec : x", "is_error": True},
                                     {"type": "text", "text": "Budget épuisé."}]},
    ]
    out = L.messages_to_mistral("SYS", history)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "Approfondis"}
    asst = out[2]
    assert asst["content"] == "Je lis." and [c["id"] for c in asst["tool_calls"]] == ["t1", "t2"]
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"url": "https://a.fr"}
    # un message « tool » par résultat, avec l'id et le nom ; le texte est rattaché au dernier
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "name": "read_article", "content": "page"}
    assert out[4]["role"] == "tool" and out[4]["name"] == "search_memory"
    assert out[4]["content"].endswith("Budget épuisé.") and len(out) == 5


def test_chat_returns_anthropic_shaped_reply():
    llm, client, _ = make(completion({"url": "https://a.fr"}, name="read_article", call_id="t9", text="Je lis."))
    resp = llm.chat("sys", [{"role": "user", "content": "go"}], agent.TOOL_DEFS,
                    tool_choice=agent.FORCE_SUBMIT, effort="medium")
    kinds = [b.type for b in resp.content]
    assert kinds == ["text", "tool_use"] and resp.stop_reason == "tool_use"
    call = resp.content[1]
    assert (call.id, call.name, call.input) == ("t9", "read_article", {"url": "https://a.fr"})
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (100, 20)
    kw = client.calls[0]
    assert kw["tool_choice"] == {"type": "function", "function": {"name": "submit_analysis"}}
    assert "effort" not in kw and "strict" not in json.dumps(kw["tools"])      # propres à Anthropic
    assert llm.agent_usage.calls == 1 and llm.usage.calls == 0


def test_chat_survives_broken_arguments_and_thinking_chunks():
    chunks = [SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text="Réponse.")]
    llm, _, _ = make(completion("{cassé", name="read_article", text=chunks))
    resp = llm.chat("s", [{"role": "user", "content": "go"}], agent.TOOL_DEFS)
    assert resp.content[0].text == "Réponse." and resp.content[1].input == {}


# --- boucle d'agent complète sur un faux Mistral -------------------------------------------------------------

def test_full_agent_loop_on_mistral(monkeypatch):
    from test_agent import ARTICLE_TEXT, TODAY, URL2, FakeMemory, event
    monkeypatch.setattr(agent.tools, "read_article", lambda url, max_chars: (url, ARTICLE_TEXT))
    final = {"summary": "La BCE maintient ses taux.", "key_figures": [
        {"value": "2,5 %", "label": "taux", "source_url": URL2},
        {"value": "9,9 %", "label": "inventé", "source_url": URL2}],
        "context": "Contexte.", "what_changed": "", "confidence": "élevée", "sources_read": [URL2]}
    llm, client, _ = make(
        completion({"url": URL2}, name="read_article", call_id="r1"),
        completion({"query": "BCE"}, name="search_memory", call_id="m1"),
        completion(final, name="submit_analysis", call_id="s1"))
    dive, trace = agent.investigate(event(), llm, FakeMemory(), CONFIG, TODAY)
    assert trace["status"] == "ok" and dive.summary == "La BCE maintient ses taux."
    assert [f["value"] for f in dive.key_figures] == ["2,5 %"]          # le chiffre inventé est retiré
    assert trace["rejected_figures"][0]["value"] == "9,9 %"
    # le 2e appel contient le résultat de l'outil au format Mistral
    second = client.calls[1]["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "r1"
    assert "<document" in second[-1]["content"]


def test_forced_conclusion_when_budget_is_spent(monkeypatch):
    from test_agent import ARTICLE_TEXT, TODAY, FakeMemory, event
    monkeypatch.setattr(agent.tools, "read_article", lambda url, max_chars: (url, ARTICLE_TEXT))
    cfg = {**CONFIG, "agent": {**CONFIG["agent"], "max_tool_calls": 1}}
    final = {"summary": "Fin.", "key_figures": [], "context": "", "what_changed": "",
             "confidence": "moyenne", "sources_read": []}
    llm, client, _ = make(completion({"query": "BCE"}, name="search_memory", call_id="m1"),
                          completion(final, name="submit_analysis", call_id="s1"))
    dive, trace = agent.investigate(event(), llm, FakeMemory(), cfg, TODAY)
    assert client.calls[0].get("tool_choice") != {"type": "function", "function": {"name": "submit_analysis"}}
    assert client.calls[1]["tool_choice"] == {"type": "function", "function": {"name": "submit_analysis"}}
    assert trace["model_calls"] == 2


def test_tool_choice_rejected_falls_back_to_free_choice(monkeypatch):
    from test_agent import ARTICLE_TEXT, TODAY, FakeMemory, event
    err = ApiError(422)
    err.body = "tool_choice not supported"
    llm, client, _ = make(err, completion({"summary": "x"}, name="submit_analysis"))
    resp = agent._call_model(llm, "mistral-small-2603", "s", [{"role": "user", "content": "go"}], force=True)
    assert resp.content[0].name == "submit_analysis" and "tool_choice" not in client.calls[1]


# --- limiteur de débit et réessais ----------------------------------------------------------------------------

def test_rate_limiter_spaces_requests():
    now, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s

    rl = L.RateLimiter(1.0, clock=lambda: now[0], sleep=sleep)
    rl.wait()
    now[0] += 0.3          # 0,3 s de travail entre deux requêtes
    rl.wait()
    rl.wait()
    assert slept == [pytest.approx(0.7), pytest.approx(1.0)]


def test_retries_429_with_growing_wait_then_succeeds():
    llm, client, sleeps = make(ApiError(429), ApiError(429), completion({"n": 1, "mot": "a"}))
    assert llm.structured("s", "p", "enregistrer", SCHEMA)["n"] == 1
    assert len(client.calls) == 3
    assert [w for w in sleeps if w >= 2] == [2.0, 4.0]       # 2 s puis 4 s (en plus du limiteur)


def test_retry_after_header_is_respected_and_capped():
    llm, _, sleeps = make(ApiError(429, headers={"retry-after": "7"}),
                          ApiError(429, headers={"retry-after": "500"}), completion({"n": 1, "mot": "a"}))
    llm.structured("s", "p", "enregistrer", SCHEMA)
    assert 7.0 in sleeps and 60.0 in sleeps


def test_gives_up_after_max_retries_with_status_code():
    llm, client, _ = make(*[ApiError(429)] * 3, rate_limit={"max_retries": 2})
    with pytest.raises(ApiError) as e:
        llm.structured("s", "p", "enregistrer", SCHEMA)
    assert e.value.status_code == 429 and len(client.calls) == 3


@pytest.mark.parametrize("status", [401, 400, 403])
def test_key_error_is_never_retried(status):
    llm, client, sleeps = make(ApiError(status), completion({"n": 1, "mot": "a"}))
    with pytest.raises(ApiError) as e:
        llm.structured("s", "p", "enregistrer", SCHEMA)
    assert e.value.status_code == status and len(client.calls) == 1 and not [w for w in sleeps if w >= 2]


def test_401_stops_pipeline_and_agent_cleanly():
    from veille.analyze import analyze
    from test_agent import TODAY, FakeMemory, event
    from datetime import datetime, timezone
    from veille.models import Article

    llm, _, _ = make(ApiError(401))
    arts = [Article(id="a", title="t", url="https://a.fr", source="s",
                    published=datetime(2026, 10, 18, tzinfo=timezone.utc))]
    with pytest.raises(ApiError):                      # le pipeline s'arrête : pas de brief vide
        analyze(arts, llm, CONFIG)

    llm, _, _ = make(ApiError(401))
    dives, traces = agent.run_agent({"marches": [event()]}, llm, FakeMemory(), CONFIG, TODAY)
    assert dives == [] and traces[0]["reason"].startswith("API refusée")


# --- coût ------------------------------------------------------------------------------------------------------

def test_mistral_is_free_but_tokens_are_counted():
    assert L.estimate_cost("mistral-small-2603", 1_000_000, 1_000_000) == 0.0
    u = L.Usage()
    u.add(500, 50, model="mistral-small-2603")
    assert (u.input_tokens, u.output_tokens, u.cost_usd) == (500, 50, 0.0)


def test_brief_shows_free_tier_instead_of_cost():
    from datetime import datetime, timezone
    from veille.brief import render_markdown
    stats = {"collected": 1, "analyzed": 1, "selected": 0, "calls": 2, "input_tokens": 10,
             "output_tokens": 5, "provider": "mistral", "free_tier": True, "total_cost_usd": 0.0}
    md = render_markdown({"marches": [], "banques": [], "ia_tech": []}, {"key_points": []}, CONFIG,
                         datetime(2026, 10, 18, tzinfo=timezone.utc), stats)
    assert "Offre gratuite mistral" in md and "Coût estimé" not in md


def test_soft_length_limits_do_not_fail_validation_but_required_fields_do():
    """Un petit modèle dépasse souvent maxItems/maxLength : le code raccourcit ensuite ; seule une réponse
    inutilisable (champ obligatoire absent) déclenche un nouvel essai."""
    schema = {"type": "object", "required": ["points"], "properties": {
        "points": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 5}}}}
    too_long = {"points": ["un texte bien trop long", "b", "c", "d", "e"]}
    llm, client, _ = make(completion(too_long))
    assert llm.structured("s", "p", "enregistrer", schema) == too_long          # accepté en un seul appel
    assert len(client.calls) == 1
    assert client.calls[0]["tools"][0]["function"]["parameters"]["properties"]["points"]["maxItems"] == 3  # toujours demandé

    llm, client, _ = make(completion({"autre": 1}), completion({"points": ["a"]}))
    assert llm.structured("s", "p", "enregistrer", schema) == {"points": ["a"]}  # champ obligatoire manquant : on réessaie
    assert len(client.calls) == 2
