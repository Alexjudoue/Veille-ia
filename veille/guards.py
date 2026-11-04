"""Garde-fous contre les défauts des petits modèles : texte à trous laissé dans un résumé.

Les petits modèles (Mistral Nemo) écrivent parfois « le taux s'élève à X%, celui du 30 ans à Y% »
ou « (valeurs non précisées dans l'extrait) » au lieu de s'abstenir. Un tel texte ne doit jamais
arriver dans le brief : on le détecte dans le code, sans faire confiance au prompt.
"""
from __future__ import annotations

import re

# Lettre-variable collée à une unité (« X% », « Y milliards », « $X ») ; mentions de valeurs absentes ;
# crochets à compléter ; mots de remplissage classiques.
_PLACEHOLDER = re.compile(
    r"(?<![\w])[XYZ]\s?(?:%|\bmilliards?\b|\bmillions?\b|\bpoints? de base\b|\bpb\b|\bbps\b|\bdollars?\b|\beuros?\b)"
    r"|[$€]\s?[XYZ]\b"
    r"|\bvaleurs?\s+(?:exactes?\s+)?(?:non|pas)\s+(?:précis|communiqu|disponib|fourni)"
    r"|\bnon\s+précis[ée]e?s?\s+dans\s+l[’']extrait"
    r"|\[[^\]]*(?:à compléter|insérer|\.\.\.|…)[^\]]*\]"
    # « passant de X à plus de 2X » : lettre-variable seule, majuscule obligatoire (« y » est un mot français)
    r"|\b(?:de|à|par|environ|vers|jusqu'à|jusqu’à)\s+(?-i:[XYZ])(?![\w'’-])"
    r"|(?<![\w])\d+(?-i:[XYZ])(?![\w])"
    r"|\b(?:TBD|lorem ipsum)\b",
    re.IGNORECASE,
)


def has_placeholder(*texts) -> bool:
    """Vrai si l'un des textes contient un trou (« X% », « valeurs non précisées »...)."""
    return any(isinstance(t, str) and _PLACEHOLDER.search(t) for t in texts)
