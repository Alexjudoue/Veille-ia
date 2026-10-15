"""Étape 3 : sélection des meilleurs articles et rédaction du brief."""
from __future__ import annotations

import logging
from datetime import datetime

from .models import AnalyzedArticle

log = logging.getLogger(__name__)

SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "key_points": {
            "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3,
            "description": "Les 3 informations à retenir aujourd'hui, une phrase chacune",
        },
        "connection": {
            "type": "string",
            "description": "Un lien ou une tendance qui relie plusieurs articles, "
                           "ou chaîne vide s'il n'y en a pas de réel",
        },
        "interview_question": {
            "type": "string",
            "description": "Une question qu'un recruteur pourrait poser sur l'actualité du jour",
        },
    },
    "required": ["key_points", "connection", "interview_question"],
}


CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "description": "Groupes d'articles qui traitent du MÊME événement. "
                           "N'inclus que les groupes d'au moins 2 articles.",
            "items": {
                "type": "object",
                "properties": {
                    "event": {"type": "string", "description": "L'événement en quelques mots"},
                    "indices": {"type": "array", "items": {"type": "integer"}, "minItems": 2},
                },
                "required": ["event", "indices"],
            },
        }
    },
    "required": ["groups"],
}


def cluster_events(items: list[AnalyzedArticle], llm) -> list[AnalyzedArticle]:
    """Regroupe les articles qui couvrent le même événement.

    La déduplication de la collecte compare les titres caractère par caractère :
    elle rate « CoreWeave chute » vs « L'Europe ouvre dans le rouge après CoreWeave ».
    Ici le LLM juge le sens. Pour chaque groupe, on garde l'article le mieux noté
    et on rattache les autres comme sources complémentaires.
    """
    if len(items) < 2:
        return items
    listing = "\n".join(f"[{i}] {it.article.title} ({it.article.source})" for i, it in enumerate(items))
    system = ("Tu regroupes des articles de presse par événement. Deux articles vont "
              "ensemble s'ils rapportent le même fait précis (même décision de justice, "
              "même opération financière, même mouvement de marché d'une entreprise), "
              "ou si l'un décrit la conséquence directe de l'autre le même jour. "
              "Deux articles sur le même sujet général mais des faits différents "
              "restent séparés.")
    try:
        out = llm.structured(system, listing, "regrouper_evenements", CLUSTER_SCHEMA, max_tokens=1500)
    except Exception as e:
        log.warning("Regroupement par événement ignoré : %s", e)
        return items

    absorbed: set[int] = set()
    for group in out.get("groups", []):
        idx = sorted({i for i in group.get("indices", [])
                      if isinstance(i, int) and 0 <= i < len(items) and i not in absorbed})
        if len(idx) < 2:
            continue
        # L'article principal : le mieux noté, puis celui au résumé le plus riche
        main = max(idx, key=lambda i: (items[i].relevance, len(items[i].summary_fr)))
        for i in idx:
            if i != main:
                a = items[i].article
                items[main].related.append({"title": a.title, "url": a.url, "source": a.source})
                absorbed.add(i)
    kept = [it for i, it in enumerate(items) if i not in absorbed]
    log.info("Regroupement : %d articles -> %d événements", len(items), len(kept))
    return kept


def select(items: list[AnalyzedArticle], config: dict, llm=None) -> dict[str, list[AnalyzedArticle]]:
    b = config["brief"]
    candidates = sorted((it for it in items if it.relevance >= b["min_relevance"]),
                        key=lambda x: (x.relevance, x.article.published), reverse=True)
    if llm is not None:
        candidates = cluster_events(candidates, llm)
    by_theme: dict[str, list[AnalyzedArticle]] = {k: [] for k in config["themes"]}
    for it in candidates:
        if len(by_theme[it.theme]) < b["max_items_per_theme"]:
            by_theme[it.theme].append(it)
    return by_theme


def synthesize(selected: dict[str, list[AnalyzedArticle]], llm, config: dict) -> dict:
    lines = []
    for theme, items in selected.items():
        for it in items:
            lines.append(f"- [{config['themes'][theme]['label']}] ({it.relevance}/10) "
                         f"{it.article.title} : {it.summary_fr}")
    if not lines:
        return {"key_points": [], "connection": "", "interview_question": ""}

    n = len(lines)
    max_points = min(3, n)
    system = (
        "Tu es rédacteur en chef d'une lettre de veille économique pour "
        f"des étudiants. Lecteur : {config['reader_profile'].strip()}\n"
        "Règles :\n"
        "- Tu t'appuies uniquement sur les articles fournis, sans ajouter de faits.\n"
        f"- Donne au maximum {max_points} point(s) à retenir, un par information "
        "réellement importante. Moins de points vaut mieux qu'un point creux.\n"
        "- N'écris jamais de point ou de phrase sur le manque d'information, "
        "le nombre d'articles ou les limites de l'analyse.\n"
        "- Le fil rouge doit relier au moins deux articles distincts ; sinon, "
        "laisse-le vide."
    )
    schema = {**SYNTHESIS_SCHEMA, "properties": {
        **SYNTHESIS_SCHEMA["properties"],
        "key_points": {**SYNTHESIS_SCHEMA["properties"]["key_points"], "maxItems": max_points},
    }}
    prompt = "Articles retenus aujourd'hui :\n" + "\n".join(lines)
    out = llm.structured(system, prompt, "rediger_synthese", schema,
                         max_tokens=1500, model=config["llm"]["synthesis_model"])
    # Garde-fous dans le code : on ne fait pas seulement confiance au prompt
    out["key_points"] = out.get("key_points", [])[:max_points]
    if n < 2:
        out["connection"] = ""
    return out


def render_markdown(selected, synthesis: dict, config: dict, date: datetime, stats: dict) -> str:
    md = [f"# Veille économique du {date:%d/%m/%Y}", ""]

    if synthesis.get("key_points"):
        md += ["## À retenir", ""]
        md += [f"{i}. {p}" for i, p in enumerate(synthesis["key_points"], 1)]
        md.append("")
        if synthesis.get("connection"):
            md += [f"**Le fil rouge :** {synthesis['connection']}", ""]
        if synthesis.get("interview_question"):
            md += [f"**Question d'entretien possible :** {synthesis['interview_question']}", ""]

    for theme, items in selected.items():
        if not items:
            continue
        md += [f"## {config['themes'][theme]['label']}", ""]
        for it in items:
            a = it.article
            md += [
                f"### [{a.title}]({a.url})",
                f"*{a.source} · {a.published:%d/%m %H:%M} UTC · pertinence {it.relevance}/10*",
                "",
                it.summary_fr,
                "",
                f"> **Pourquoi c'est important :** {it.why_it_matters}",
                "",
            ]
            if it.related:
                links = ", ".join(f"[{r['source']}]({r['url']})" for r in it.related)
                md += [f"*Aussi couvert par : {links}*", ""]

    def nb(x: int) -> str:  # 12345 -> "12 345"
        return f"{x:,}".replace(",", " ")

    def s(x: int) -> str:   # pluriel
        return "s" if x > 1 else ""

    md += [
        "---",
        f"*{nb(stats['collected'])} article{s(stats['collected'])} collecté{s(stats['collected'])}, "
        f"{nb(stats['analyzed'])} analysé{s(stats['analyzed'])}, "
        f"{nb(stats['selected'])} retenu{s(stats['selected'])}. "
        f"{stats['calls']} appel{s(stats['calls'])} au modèle, "
        f"{nb(stats['input_tokens'])} tokens en entrée et {nb(stats['output_tokens'])} en sortie.*",
        "",
        "*Résumés générés par IA à partir des titres et extraits : vérifie les chiffres dans l'article source.*",
    ]
    return "\n".join(md)


def render_telegram(selected, synthesis: dict, config: dict, date: datetime) -> str:
    """Version courte pour Telegram (limite de 4096 caractères, format HTML)."""
    import html as h
    out = [f"<b>Veille du {date:%d/%m}</b>", ""]
    for p in synthesis.get("key_points", []):
        out.append(f"• {h.escape(p)}")
    for theme, items in selected.items():
        if items:
            out += ["", f"<b>{h.escape(config['themes'][theme]['label'])}</b>"]
            out += [f'– <a href="{h.escape(it.article.url)}">{h.escape(it.article.title)}</a>'
                    for it in items[:4]]
    text = "\n".join(out)
    return text[:4000]
