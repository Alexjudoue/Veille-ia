"""Garde-fou de concision : raccourcit un texte proprement, dans le code.

Les petits modèles dépassent souvent les longueurs demandées. On ne fait donc pas seulement
confiance au prompt : toute phrase affichée sur la page passe par ces fonctions.

Règles : on coupe en priorité en fin de phrase ; sinon à la limite d'un mot, jamais au milieu
d'un mot ; un nombre n'est jamais séparé de son unité (« 2,5 % », « 350 Md$ ») ; la coupe ne
laisse pas un mot-outil orphelin (« de », « la »...) en fin de texte.
"""
from __future__ import annotations

import re

# Fin de phrase : ponctuation forte suivie d'une espace puis d'une majuscule, d'un chiffre ou d'un guillemet.
# Un point décimal (« 2.5 ») n'est pas suivi d'une espace : il ne coupe donc pas.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÖØ-Ý0-9«“\"'(])")
# Unités qui doivent rester collées au nombre qui les précède
_UNITS = {"%", "$", "€", "£", "‰", "pb", "bps", "pts", "md$", "m$", "md€", "m€", "k€", "k$",
          "milliard", "milliards", "million", "millions", "mille", "billion", "billions",
          "dollar", "dollars", "euro", "euros", "point", "points", "pourcent", "mds", "md", "bn", "mn", "trillion"}
# Mots-outils qu'on ne laisse pas en fin de texte coupé
_DANGLING = {"de", "du", "des", "la", "le", "les", "l'", "d'", "à", "au", "aux", "et", "ou", "en", "pour",
             "par", "sur", "avec", "un", "une", "dans", "que", "qui", "se", "ce", "son", "sa", "ses",
             "of", "the", "and", "to", "in", "for", "a", "an", "on", "with"}


def plain(text: str) -> str:
    """Retire le Markdown résiduel (*italique*, **gras**, `code`, # titres) : le PDF affiche du texte brut."""
    text = re.sub(r"[*`]{1,3}([^*`\n]+?)[*`]{1,3}", r"\1", text or "")
    text = re.sub(r"[*`]+", "", text)                      # astérisques orphelins (« **16 milliards »)
    return " ".join(re.sub(r"^\s*#+\s*", "", text).split())


def count_words(text: str) -> int:
    return len(text.split())


def _is_number(token: str) -> bool:
    return bool(re.search(r"\d", token))


def _cut_words(text: str, max_words: int) -> str:
    """Garde `max_words` mots au plus, en respectant nombres et unités."""
    tokens = text.split()
    if len(tokens) <= max_words:
        return text.strip()
    keep = max_words
    # « 350 » coupé juste avant « milliards » / « % » : on emporte l'unité avec le nombre
    while keep < len(tokens) and _is_number(tokens[keep - 1]) and tokens[keep].lower().strip(".,;") in _UNITS:
        keep += 1
    kept = tokens[:keep]
    # un nombre sans son contexte en bout de texte (« … à 4,6 ») reste acceptable, mais pas un mot-outil
    while kept and kept[-1].lower().strip(",;:") in _DANGLING:
        kept.pop()
    out = " ".join(kept).rstrip(",;:-–— ")
    return out + "…" if out and out[-1] not in ".!?…" else out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def fit_words(text: str, max_words: int) -> str:
    """Texte de `max_words` mots au plus : on garde les phrases entières tant qu'elles tiennent,
    sinon on coupe la première à la limite d'un mot."""
    text = " ".join((text or "").split())
    if count_words(text) <= max_words:
        return text
    kept: list[str] = []
    for s in sentences(text):
        if count_words(" ".join(kept + [s])) > max_words:
            break
        kept.append(s)
    if kept:
        return " ".join(kept)
    return _cut_words(text, max_words)


def fit_sentences(text: str, max_sentences: int, max_words: int | None = None) -> str:
    """Au plus `max_sentences` phrases (et `max_words` mots si précisé), jamais de phrase coupée
    sauf si la première est à elle seule trop longue."""
    text = " ".join((text or "").split())
    chosen = sentences(text)[:max_sentences]
    out = " ".join(chosen) if chosen else text
    return fit_words(out, max_words) if max_words else out
