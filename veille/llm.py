"""Petite couche autour de l'API Claude.

On force le modèle à répondre via un « outil » au schéma JSON strict :
c'est la façon fiable d'obtenir des données structurées (pas de parsing
de texte libre fragile). On compte aussi les tokens pour suivre le coût.

Prompt caching : le prompt système (et les outils, qui le précèdent) est
identique d'un appel à l'autre ; l'historique d'un événement de l'agent
est relu à chaque tour. On marque ces blocs avec `cache_control` : relus
depuis le cache, ils coûtent environ 10 fois moins cher.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Prix en dollars par million de tokens : (entrée, sortie). Lecture du cache = 0,1 x
# l'entrée, écriture dans le cache = 1,25 x l'entrée (barème standard du cache 5 min).
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_WRITE_FACTOR, CACHE_READ_FACTOR = 1.25, 0.10

CACHE = {"type": "ephemeral"}


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
    """Coût estimé en dollars ; 0 si le modèle est inconnu."""
    if model not in PRICES:
        return 0.0
    p_in, p_out = PRICES[model]
    return (inp * p_in + out * p_out
            + cache_write * p_in * CACHE_WRITE_FACTOR
            + cache_read * p_in * CACHE_READ_FACTOR) / 1_000_000


def _record(usage: Usage, resp, model: str) -> None:
    """Compte les tokens d'une réponse, cache compris."""
    u = resp.usage
    usage.add(u.input_tokens, u.output_tokens,
              getattr(u, "cache_creation_input_tokens", 0) or 0,
              getattr(u, "cache_read_input_tokens", 0) or 0, model)


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


class LLM:
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
