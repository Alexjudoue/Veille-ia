"""Étape 2 : le LLM lit chaque article, le classe, le note et le résume."""
from __future__ import annotations

import logging

from .models import AnalyzedArticle, Article

log = logging.getLogger(__name__)

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Numéro de l'article dans la liste fournie"},
                    "theme": {"type": "string", "description": "Clé du thème, ou 'hors_sujet'"},
                    "relevance": {"type": "integer", "minimum": 0, "maximum": 10},
                    "summary_fr": {"type": "string", "description": "Résumé factuel en 2 phrases max"},
                    "why_it_matters": {"type": "string", "description": "Pourquoi c'est important pour le lecteur, 1 phrase"},
                    "entities": {"type": "array", "items": {"type": "string"},
                                 "description": "Entreprises, institutions, personnes clés (max 4)"},
                },
                "required": ["index", "theme", "relevance", "summary_fr", "why_it_matters", "entities"],
            },
        }
    },
    "required": ["articles"],
}


def build_system_prompt(config: dict) -> str:
    themes = "\n".join(
        f"- `{key}` ({t['label']}) : {t['description'].strip()}"
        for key, t in config["themes"].items()
    )
    return f"""Tu es analyste dans une cellule de veille économique.

Lecteur cible : {config['reader_profile'].strip()}

Thèmes suivis :
{themes}

Pour chaque article :
1. Attribue le thème le plus adapté, ou `hors_sujet` s'il ne correspond à aucun.
2. Note sa pertinence de 0 à 10 pour ce lecteur :
   9-10 : événement majeur qui fait bouger le secteur (décision de banque centrale,
          krach, méga-acquisition, sortie d'un modèle d'IA de rupture)
   6-8  : information utile à connaître pour un entretien ou une discussion
   3-5  : intérêt limité, anecdotique ou très local
   0-2  : bruit, publicité, contenu sans information
3. Résume en {config['brief']['language']}, de façon factuelle, en 2 phrases maximum.
   Garde les chiffres précis (montants, %, dates). N'invente rien : si le titre et
   l'extrait ne suffisent pas, reste général plutôt que de supposer.
   Résume ce que l'on sait ; ne commente jamais ce que l'article ne dit pas
   (pas de « le titre ne précise pas… », « l'extrait est trop court… »).
4. Explique en une phrase pourquoi c'est important pour le lecteur, avec la
   même règle : une affirmation utile, sans réserve sur la qualité de la source."""


def _format_batch(batch: list[Article]) -> str:
    lines = []
    for i, a in enumerate(batch):
        lines.append(
            f"[{i}] {a.title}\n"
            f"    Source : {a.source} | {a.published:%d/%m %H:%M} UTC\n"
            f"    Extrait : {a.summary or '(aucun)'}"
        )
    return "Analyse les articles suivants :\n\n" + "\n\n".join(lines)


def analyze(articles: list[Article], llm, config: dict) -> list[AnalyzedArticle]:
    system = build_system_prompt(config)
    valid_themes = set(config["themes"])
    size = config["llm"]["batch_size"]
    results: list[AnalyzedArticle] = []

    for start in range(0, len(articles), size):
        batch = articles[start:start + size]
        log.info("Analyse des articles %d à %d...", start + 1, start + len(batch))
        try:
            out = llm.structured(system, _format_batch(batch), "enregistrer_analyse", ANALYSIS_SCHEMA)
        except Exception as e:
            # Clé invalide, crédits épuisés, requête mal formée : inutile d'insister,
            # tous les lots échoueront de la même façon. On arrête tout.
            if getattr(e, "status_code", None) in (400, 401, 403):
                raise
            # Erreur passagère (réseau, surcharge) : on saute ce lot et on continue.
            log.error("Lot ignoré : %s", e)
            continue
        for item in out.get("articles", []):
            idx = item.get("index")
            if not isinstance(idx, int) or not 0 <= idx < len(batch):
                continue  # le modèle a halluciné un index : on ignore
            if item["theme"] not in valid_themes:
                continue
            results.append(AnalyzedArticle(
                article=batch[idx],
                theme=item["theme"],
                relevance=max(0, min(10, int(item["relevance"]))),
                summary_fr=item["summary_fr"].strip(),
                why_it_matters=item["why_it_matters"].strip(),
                entities=item.get("entities", [])[:4],
            ))
    return results
