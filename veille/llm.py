"""Couche d'accès au modèle de langage : un client par fournisseur, une interface commune.

Le reste du programme (analyse, brief, agent) ne connaît que deux méthodes :

  - `structured(system, prompt, tool_name, schema, ...)` : renvoie un dict JSON conforme
    au schéma (le modèle répond via un « outil » forcé, pas en texte libre) ;
  - `chat(system, messages, tools, tool_choice, ...)` : un tour de la boucle de l'agent.
    La réponse a toujours la forme `.content` (blocs `text` / `tool_use`), `.stop_reason`
    et `.usage` ; l'historique des messages est au format « Anthropic »
    (`tool_use` / `tool_result`). Chaque client le traduit pour son API.

Fournisseurs (`llm.provider` dans config.yaml, une seule ligne à changer) :

  - `mistral`   : API Mistral (offre gratuite « Experiment »), SDK officiel `mistralai`.
                  Pas de prompt caching ni d'effort de réflexion ; débit limité (~1 requête/s),
                  d'où le limiteur de débit et les réessais sur les erreurs 429.
  - `anthropic` : API Claude, avec prompt caching et effort de réflexion.

Le prompt caching d'Anthropic : le prompt système (et les outils, qui le précèdent) est
identique d'un appel à l'autre ; l'historique d'un événement de l'agent est relu à chaque
tour. On marque ces blocs avec `cache_control` : relus depuis le cache, ils coûtent environ
10 fois moins cher.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace

log = logging.getLogger(__name__)

# Prix en dollars par million de tokens : (entrée, sortie). Lecture du cache = 0,1 x
# l'entrée, écriture dans le cache = 1,25 x l'entrée (barème standard du cache 5 min).
# Les modèles Mistral de l'offre gratuite n'ont volontairement aucun prix : coût = 0 $,
# mais les tokens restent comptés.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_WRITE_FACTOR, CACHE_READ_FACTOR = 1.25, 0.10

CACHE = {"type": "ephemeral"}

# Fournisseurs gratuits : le brief affiche « offre gratuite » au lieu d'un coût en dollars
FREE_PROVIDERS = {"mistral"}
DEFAULT_PROVIDER = "anthropic"


@dataclass
class Usage:
    input_tokens: int = 0        # tokens d'entrée facturés plein tarif (hors cache)
    output_tokens: int = 0
    calls: int = 0
    cache_write_tokens: int = 0  # tokens écrits dans le cache
    cache_read_tokens: int = 0   # tokens relus depuis le cache
    cost_usd: float = 0.0        # coût estimé, calculé à chaque appel selon le modèle

    def add(self, inp: int, out: int, cache_write: int = 0, cache_read: int = 0,
            model: str = "") -> None:
        self.input_tokens += inp
        self.output_tokens += out
        self.calls += 1
        self.cache_write_tokens += cache_write
        self.cache_read_tokens += cache_read
        self.cost_usd += estimate_cost(model, inp, out, cache_write, cache_read)

    @property
    def total_input(self) -> int:
        """Tout ce que le modèle a lu, cache compris (comparable avant/après caching)."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


def estimate_cost(model: str, inp: int, out: int, cache_write: int = 0, cache_read: int = 0) -> float:
    """Coût estimé en dollars ; 0 si le modèle est inconnu (dont les modèles Mistral gratuits)."""
    if model not in PRICES:
        return 0.0
    p_in, p_out = PRICES[model]
    return (inp * p_in + out * p_out
            + cache_write * p_in * CACHE_WRITE_FACTOR
            + cache_read * p_in * CACHE_READ_FACTOR) / 1_000_000


def _record(usage: Usage, resp, model: str) -> None:
    """Compte les tokens d'une réponse Anthropic, cache compris."""
    u = resp.usage
    usage.add(u.input_tokens, u.output_tokens,
              getattr(u, "cache_creation_input_tokens", 0) or 0,
              getattr(u, "cache_read_input_tokens", 0) or 0, model)


# --- Choix du fournisseur et des modèles (config.yaml) --------------------------------------------

def provider_of(config: dict) -> str:
    """Fournisseur choisi dans config.yaml (`llm.provider`)."""
    return (config.get("llm", {}).get("provider") or DEFAULT_PROVIDER).strip().lower()


def model_for(config: dict, kind: str = "model") -> str:
    """Nom du modèle du fournisseur actif. `kind` : « model » (tri, agent) ou « synthesis_model ».

    Les modèles sont rangés par fournisseur dans `llm.models.<fournisseur>` ; les anciennes clés
    à plat (`llm.model`, `llm.synthesis_model`) restent acceptées en repli.
    """
    llm_cfg = config["llm"]
    section = (llm_cfg.get("models") or {}).get(provider_of(config)) or {}
    name = section.get(kind) or (section.get("model") if kind != "model" else None)
    return name or llm_cfg.get(kind) or llm_cfg["model"]


# --- Client Anthropic (Claude) ----------------------------------------------------------------------

def _cached_system(system: str) -> list[dict]:
    """Le prompt système sous forme de bloc marqué pour le cache. Le marqueur couvre
    aussi les outils, qui sont placés avant le système dans le prompt."""
    return [{"type": "text", "text": system, "cache_control": CACHE}]


def with_cache_breakpoint(messages: list) -> list:
    """Copie de la conversation avec un marqueur de cache sur le dernier bloc.

    Au tour suivant, tout ce qui précède (y compris ce tour) est relu depuis le
    cache. On copie sans modifier `messages` : l'historique reste intact, et il n'y
    a ainsi jamais plus de 2 marqueurs dans la requête (système + dernier bloc).
    """
    last = messages[-1]
    content = last["content"]
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content, "cache_control": CACHE}]
    elif content and isinstance(content[-1], dict):
        blocks = [*content[:-1], {**content[-1], "cache_control": CACHE}]
    else:
        return messages     # dernier bloc inattendu : on n'insiste pas
    return [*messages[:-1], {**last, "content": blocks}]


class AnthropicLLM:
    provider = "anthropic"

    def __init__(self, model: str, effort: str | None = None):
        import anthropic  # import ici pour que les tests tournent sans clé

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY manquante. Copie .env.example en .env et "
                "renseigne ta clé, ou lance avec --dry-run."
            )
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort          # effort de réflexion du pipeline (None = défaut du modèle)
        self.usage = Usage()          # coût du pipeline (analyse, regroupement, synthèse)
        self.agent_usage = Usage()    # coût de l'agent (V2), compté à part

    def structured(self, system: str, prompt: str, tool_name: str,
                   schema: dict, max_tokens: int = 4000, model: str | None = None) -> dict:
        """Appelle le modèle et renvoie directement le dict JSON validé par le schéma."""
        model = model or self.model
        kwargs = {"output_config": {"effort": self.effort}} if self.effort else {}
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_cached_system(system),
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "name": tool_name,
                "description": "Enregistre le résultat de l'analyse.",
                "input_schema": schema,
            }],
            tool_choice={"type": "tool", "name": tool_name},
            **kwargs,
        )
        _record(self.usage, resp, model)
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("Le modèle n'a pas renvoyé de résultat structuré.")

    def chat(self, system: str, messages: list, tools: list,
             tool_choice: dict | None = None, max_tokens: int = 4000,
             model: str | None = None, effort: str | None = None):
        """Un tour de conversation avec outils, pour la boucle de l'agent (V2).

        Contrairement à `structured`, on renvoie la réponse complète : c'est la
        boucle de l'agent qui lit les `tool_use`, exécute les outils et renvoie
        les résultats. Les tokens sont comptés dans `agent_usage`.
        """
        model = model or self.model
        kwargs = dict(model=model, max_tokens=max_tokens, system=_cached_system(system),
                      messages=with_cache_breakpoint(messages), tools=tools)
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if effort:
            kwargs["output_config"] = {"effort": effort}
        resp = self.client.messages.create(**kwargs)
        _record(self.agent_usage, resp, model)
        return resp


LLM = AnthropicLLM   # ancien nom, conservé pour ne rien casser


# --- Client Mistral -------------------------------------------------------------------------------

class RateLimiter:
    """Espace les requêtes d'au moins `1 / per_second` secondes (offre gratuite : ~1 requête/s).

    `clock` et `sleep` sont injectables pour que les tests n'attendent pas vraiment.
    """

    def __init__(self, per_second: float = 1.0, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / per_second if per_second and per_second > 0 else 0.0
        self._clock, self._sleep = clock, sleep
        self._next_ok = 0.0     # instant avant lequel on ne peut pas repartir

    def wait(self) -> None:
        now = self._clock()
        if now < self._next_ok:
            self._sleep(self._next_ok - now)
            now = self._next_ok
        self._next_ok = now + self.interval


def _retry_after(err) -> float | None:
    """Attente demandée par le serveur (en-tête Retry-After, en secondes), si elle est fournie."""
    headers = getattr(err, "headers", None)
    try:
        value = headers.get("retry-after") if headers is not None else None
        return float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _is_retryable(err) -> bool:
    """429 (limite de débit), 5xx (surcharge) et coupures réseau : on peut réessayer.
    401/400/403/422 : requête ou clé en cause, réessayer ne servirait à rien."""
    status = getattr(err, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return type(err).__name__ in ("NoResponseError", "ConnectError", "ReadTimeout", "ConnectTimeout",
                                  "RemoteProtocolError", "ReadError", "WriteError", "PoolTimeout")


def _tool_to_mistral(tool: dict) -> dict:
    """Outil au format Anthropic -> format Mistral. Le champ `strict` (propre à Anthropic) est ignoré."""
    return {"type": "function",
            "function": {"name": tool["name"], "description": tool.get("description", ""),
                         "parameters": tool["input_schema"]}}


def _forced_choice(tool_choice: dict | None):
    """`tool_choice` Anthropic -> Mistral. {"type":"tool","name":X} impose l'outil X."""
    if not tool_choice:
        return None
    if tool_choice.get("type") == "tool":
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return "any" if tool_choice.get("type") == "any" else "auto"


def _text_of(content) -> str:
    """Texte d'un message Mistral. `content` peut être une chaîne ou une liste de morceaux
    (texte, réflexion...) : on ne garde que le texte."""
    if isinstance(content, str):
        return content
    parts = []
    for chunk in content or []:
        kind = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", "")
        text = chunk.get("text") if isinstance(chunk, dict) else getattr(chunk, "text", None)
        if kind == "text" and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _args_of(raw) -> dict:
    """Arguments d'un appel d'outil : chaîne JSON (cas courant) ou dict. Lève ValueError sinon."""
    if isinstance(raw, dict):
        return raw
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise ValueError("les arguments de l'outil ne sont pas un objet JSON")
    return data


def messages_to_mistral(system: str, messages: list) -> list[dict]:
    """Traduit l'historique de l'agent (format Anthropic) vers le format Mistral.

    - assistant : blocs `text` -> `content`, blocs `tool_use` -> `tool_calls` (arguments en chaîne JSON) ;
    - user avec des `tool_result` -> un message de rôle « tool » par résultat (avec `tool_call_id`
      et le nom de l'outil) ; un éventuel bloc `text` du même message est ajouté au dernier résultat,
      car Mistral n'aime pas un message « user » juste après un message « tool ».
    """
    out: list[dict] = [{"role": "system", "content": system}]
    names: dict[str, str] = {}      # id d'appel -> nom de l'outil (le message « tool » le demande)
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            blocks = [content] if isinstance(content, str) else list(content)
            text = "".join(b if isinstance(b, str) else b.text for b in blocks
                           if isinstance(b, str) or getattr(b, "type", "") == "text")
            calls = []
            for b in blocks:
                if not isinstance(b, str) and getattr(b, "type", "") == "tool_use":
                    names[b.id] = b.name
                    calls.append({"id": b.id, "type": "function",
                                  "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)}})
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif isinstance(content, str):
            out.append({"role": "user", "content": content})
        else:
            results = [b for b in content if b.get("type") == "tool_result"]
            texts = [b["text"] for b in content if b.get("type") == "text"]
            for b in results:
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                            "name": names.get(b["tool_use_id"], "outil"), "content": str(b["content"])})
            if texts and results:
                out[-1]["content"] += "\n\n" + "\n".join(texts)
            elif texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


class MistralLLM:
    provider = "mistral"

    def __init__(self, model: str, effort: str | None = None, rate_limit: dict | None = None,
                 client=None, sleep=time.sleep, clock=time.monotonic):
        # `effort` est accepté pour garder la même signature qu'AnthropicLLM, mais Mistral n'a pas
        # d'équivalent : il est ignoré.
        if client is None:
            if not os.environ.get("MISTRAL_API_KEY"):
                raise RuntimeError(
                    "MISTRAL_API_KEY manquante. Copie .env.example en .env et "
                    "renseigne ta clé, ou lance avec --dry-run."
                )
            try:
                from mistralai.client import Mistral      # SDK >= 2
            except ImportError:
                from mistralai import Mistral             # SDK 1.x
            client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
        cfg = rate_limit or {}
        self.client = client
        self.model = model
        self.usage = Usage()
        self.agent_usage = Usage()
        self.max_retries = int(cfg.get("max_retries", 5))
        self.base_wait = float(cfg.get("base_wait_s", 2.0))
        self.max_wait = float(cfg.get("max_wait_s", 60.0))
        self._sleep = sleep
        self.limiter = RateLimiter(float(cfg.get("requests_per_second", 1.0)), clock=clock, sleep=sleep)

    # -- appel brut, avec limiteur de débit et réessais -------------------------------------------

    def _complete(self, usage: Usage, model: str, **kwargs):
        """Un appel à l'API. Espacé par le limiteur ; sur 429 / 5xx / coupure réseau, on réessaie avec
        une attente qui double (2 s, 4 s, 8 s...) ou celle que demande le serveur. Une autre erreur
        (clé invalide 401, requête refusée 400...) remonte tout de suite, avec son `status_code`."""
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                resp = self.client.chat.complete(model=model, **kwargs)
                break
            except Exception as e:
                if not _is_retryable(e) or attempt == self.max_retries:
                    raise
                wait = _retry_after(e)
                wait = min(self.max_wait, wait if wait is not None else self.base_wait * 2 ** attempt)
                log.warning("Mistral : %s (essai %d/%d), nouvel essai dans %.0f s",
                            getattr(e, "status_code", type(e).__name__), attempt + 1,
                            self.max_retries, wait)
                self._sleep(wait)
        u = resp.usage
        usage.add(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0, model=model)
        return resp

    # -- sortie structurée ---------------------------------------------------------------------------

    def structured(self, system: str, prompt: str, tool_name: str,
                   schema: dict, max_tokens: int = 4000, model: str | None = None) -> dict:
        """Appel de fonction forcé, puis validation du résultat contre le schéma.

        Si le résultat est invalide (JSON cassé, champ manquant, mauvais type), on renvoie l'erreur
        au modèle et on réessaie une fois ; au second échec, on lève une erreur (le lot est ignoré).
        """
        import jsonschema

        model = model or self.model
        tool = _tool_to_mistral({"name": tool_name, "description": "Enregistre le résultat de l'analyse.",
                                 "input_schema": schema})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        problem = ""
        for _ in range(2):
            resp = self._complete(self.usage, model, messages=messages, tools=[tool],
                                  tool_choice=_forced_choice({"type": "tool", "name": tool_name}),
                                  max_tokens=max_tokens)
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
            try:
                if calls:
                    data = _args_of(calls[0].function.arguments)
                else:   # pas d'appel d'outil : parfois le JSON est dans le texte
                    data = _args_of(_text_of(msg.content).strip().removeprefix("```json").removesuffix("```"))
                jsonschema.validate(data, schema)
                return data
            except (ValueError, jsonschema.ValidationError) as e:
                problem = getattr(e, "message", str(e))
                log.warning("Sortie structurée invalide (%s) : nouvel essai", problem[:120])
                if calls:
                    messages += [
                        {"role": "assistant", "content": "",
                         "tool_calls": [{"id": calls[0].id, "type": "function",
                                         "function": {"name": tool_name, "arguments": str(calls[0].function.arguments)}}]},
                        {"role": "tool", "tool_call_id": calls[0].id, "name": tool_name,
                         "content": f"Résultat invalide : {problem}. Appelle de nouveau l'outil avec un résultat conforme."},
                    ]
        raise RuntimeError(f"Le modèle n'a pas renvoyé de résultat structuré valide ({problem[:200]}).")

    # -- tour de la boucle de l'agent ------------------------------------------------------------------

    def chat(self, system: str, messages: list, tools: list,
             tool_choice: dict | None = None, max_tokens: int = 4000,
             model: str | None = None, effort: str | None = None):
        """Un tour avec outils. `effort` est ignoré (pas d'équivalent chez Mistral).

        Renvoie un objet de même forme que la réponse d'Anthropic (`content`, `stop_reason`, `usage`),
        pour que la boucle de l'agent ne change pas. Un appel d'outil dont les arguments ne sont pas
        du JSON valide est transmis avec des arguments vides : l'outil échouera proprement et le
        modèle pourra corriger.
        """
        model = model or self.model
        kwargs = dict(messages=messages_to_mistral(system, messages),
                      tools=[_tool_to_mistral(t) for t in tools], max_tokens=max_tokens)
        choice = _forced_choice(tool_choice)
        if choice:
            kwargs["tool_choice"] = choice
        resp = self._complete(self.agent_usage, model, **kwargs)
        choice0 = resp.choices[0]
        msg = choice0.message
        blocks = []
        text = _text_of(msg.content)
        if text.strip():
            blocks.append(SimpleNamespace(type="text", text=text))
        for c in getattr(msg, "tool_calls", None) or []:
            try:
                args = _args_of(c.function.arguments)
            except ValueError:
                args = {}
            blocks.append(SimpleNamespace(type="tool_use", id=c.id, name=c.function.name, input=args))
        stop = {"tool_calls": "tool_use", "length": "max_tokens"}.get(str(choice0.finish_reason), "end_turn")
        u = resp.usage
        return SimpleNamespace(content=blocks, stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                                                     output_tokens=getattr(u, "completion_tokens", 0) or 0))


def make_llm(config: dict):
    """Crée le client du fournisseur choisi dans config.yaml (`llm.provider`)."""
    provider = provider_of(config)
    if provider not in ("mistral", "anthropic"):
        raise ValueError(f"Fournisseur inconnu : « {provider} » (choisis « mistral » ou « anthropic »).")
    model = model_for(config)
    if provider == "mistral":
        return MistralLLM(model, rate_limit=config["llm"].get("rate_limit"))
    return AnthropicLLM(model, effort=config["llm"].get("effort") or None)
