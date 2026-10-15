"""Petite couche autour de l'API Claude.

On force le modèle à répondre via un « outil » au schéma JSON strict :
c'est la façon fiable d'obtenir des données structurées (pas de parsing
de texte libre fragile). On compte aussi les tokens pour suivre le coût.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, inp: int, out: int) -> None:
        self.input_tokens += inp
        self.output_tokens += out
        self.calls += 1


class LLM:
    def __init__(self, model: str):
        import anthropic  # import ici pour que les tests tournent sans clé

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY manquante. Copie .env.example en .env et "
                "renseigne ta clé, ou lance avec --dry-run."
            )
        self.client = anthropic.Anthropic()
        self.model = model
        self.usage = Usage()

    def structured(self, system: str, prompt: str, tool_name: str,
                   schema: dict, max_tokens: int = 4000, model: str | None = None) -> dict:
        """Appelle le modèle et renvoie directement le dict JSON validé par le schéma."""
        resp = self.client.messages.create(
            model=model or self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "name": tool_name,
                "description": "Enregistre le résultat de l'analyse.",
                "input_schema": schema,
            }],
            tool_choice={"type": "tool", "name": tool_name},
        )
        self.usage.add(resp.usage.input_tokens, resp.usage.output_tokens)
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("Le modèle n'a pas renvoyé de résultat structuré.")
