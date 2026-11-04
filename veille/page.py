"""Contenu de la page unique du brief (PDF d'une feuille A4).

Une seule étape de rédaction (`rediger_page`) produit : sous-titre, 3 points, résumé du focus,
et pour chaque article « À lire » un titre court et une phrase. Ensuite le CODE applique les
limites de longueur (voir shorten.py), écarte les textes à trous (guards.py) et, si le modèle a
omis ou raté quelque chose, retombe sur un texte déterministe tiré des résumés déjà validés :
la page sort toujours, même avec un petit modèle.

Le focus est la meilleure analyse approfondie de l'agent. Sans analyse réussie, on prend le
meilleur article avec son résumé v1 et SANS chiffres : jamais de chiffre non vérifié.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from .guards import has_placeholder
from .llm import model_for
from .models import AnalyzedArticle, DeepDive
from .shorten import fit_sentences, fit_words, plain

log = logging.getLogger(__name__)

CHARS_PER_WORD = 10     # marge généreuse pour maxLength : le vrai plafond en mots est dans le code


def limits(config: dict) -> dict:
    """Limites de la page (config.yaml > brief.page), avec des valeurs par défaut."""
    base = dict(subtitle_words=14, points=3, point_words=25, focus_sentences=3, focus_words=60,
                figures=3, figure_label_words=5, reads_per_theme=3, title_words=10,
                blurb_words=20, question_words=25)
    return {**base, **(config.get("brief", {}).get("page") or {})}


def page_schema(lim: dict, n_candidates: int) -> dict:
    """Schéma JSON demandé au modèle. Les maxLength sont en caractères (marge large) ;
    la coupe exacte en mots est faite ensuite par le code."""
    def text(words: int, desc: str) -> dict:
        return {"type": "string", "maxLength": words * CHARS_PER_WORD, "description": desc}

    return {
        "type": "object",
        "properties": {
            "subtitle": text(lim["subtitle_words"], f"Sous-titre de la page, {lim['subtitle_words']} mots maximum"),
            "points": {"type": "array", "minItems": 1, "maxItems": lim["points"],
                       "items": text(lim["point_words"],
                                     f"Une information à retenir, UNE phrase de {lim['point_words']} mots maximum")},
            "focus_title": text(lim["title_words"], f"Titre du focus, {lim['title_words']} mots maximum"),
            "focus_summary": text(lim["focus_words"],
                                  f"Le focus en {lim['focus_sentences']} phrases maximum, {lim['focus_words']} mots maximum"),
            "reads": {"type": "array", "maxItems": n_candidates,
                      "items": {"type": "object",
                                "properties": {
                                    "id": {"type": "integer", "description": "Numéro de l'article dans la liste"},
                                    "title": text(lim["title_words"],
                                                  f"Titre reformulé, {lim['title_words']} mots maximum"),
                                    "blurb": text(lim["blurb_words"],
                                                  f"Une phrase d'explication, {lim['blurb_words']} mots maximum")},
                                "required": ["id", "title", "blurb"]}},
            "question": text(lim["question_words"],
                             f"Une question d'entretien sur l'actualité du jour, {lim['question_words']} mots maximum"),
        },
        # Seuls les points sont obligatoires : un petit modèle qui oublie un champ facultatif ne doit pas
        # faire échouer toute la rédaction ; le code remplace chaque champ manquant par un repli.
        "required": ["points"],
    }


def _domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _is_google_news(url: str) -> bool:
    return "news.google.com" in url


def _read_score(it: AnalyzedArticle) -> tuple:
    """Classement « À lire » : score le plus haut d'abord ; à égalité, lien direct (lisible) avant
    un lien Google News (souvent derrière un accès refusé), puis l'événement le plus couvert."""
    return (it.relevance, not _is_google_news(it.article.url), len(it.related), it.article.published)


def pick_focus(selected: dict[str, list[AnalyzedArticle]], deep_dives: list[DeepDive]):
    """Renvoie (article du focus, analyse approfondie ou None). None si aucun article."""
    if deep_dives:
        best = max(deep_dives, key=lambda d: (d.item.relevance, len(d.item.related)))
        return best.item, best
    items = [it for group in selected.values() for it in group]
    if not items:
        return None, None
    return max(items, key=_read_score), None


def pick_reads(selected: dict[str, list[AnalyzedArticle]], focus: AnalyzedArticle | None,
               per_theme: int) -> dict[str, list[AnalyzedArticle]]:
    """Par thème, les `per_theme` articles les plus intéressants (hors focus)."""
    return {theme: sorted((it for it in items if focus is None or it.article.id != focus.article.id),
                          key=_read_score, reverse=True)[:per_theme]
            for theme, items in selected.items()}


def _clean(text: str, fallback: str, words: int) -> str:
    """Texte du modèle raccourci ; s'il est vide ou à trous, on prend le repli."""
    text = plain(text)
    if not text or has_placeholder(text):
        text = plain(fallback)
    return fit_words(text, words)


def _first_sentence(text: str, words: int) -> str:
    return fit_sentences(plain(text), 1, words)


def _prompt(focus, dive, reads_by_theme, candidates, config) -> str:
    lines = ["FOCUS DU JOUR (à résumer en priorité) :"]
    if focus is not None:
        text = dive.summary if dive else focus.summary_fr
        lines.append(f"- {focus.article.title} ({focus.article.source}) : {text}")
        if dive and dive.key_figures:
            lines.append("  Chiffres vérifiés : " + "; ".join(f"{f['value']} ({f['label']})" for f in dive.key_figures))
    lines += ["", "ARTICLES À PRÉSENTER (reprends leur numéro dans « reads ») :"]
    for i, (theme, it) in enumerate(candidates):
        lines.append(f"[{i}] ({config['themes'][theme]['label']}) {it.article.title} "
                     f"({it.article.source}) : {it.summary_fr}")
    return "\n".join(lines)


def build_page(selected: dict[str, list[AnalyzedArticle]], deep_dives: list[DeepDive], llm,
               config: dict, date, analyzed_count: int) -> dict:
    """Compose la page : appel au modèle, puis limites, garde-fous et replis appliqués par le code."""
    lim = limits(config)
    focus, dive = pick_focus(selected, deep_dives)
    reads_by_theme = pick_reads(selected, focus, lim["reads_per_theme"])
    candidates = [(theme, it) for theme, items in reads_by_theme.items() for it in items]

    out: dict = {}
    if focus is not None:
        system = (
            "Tu es rédacteur en chef d'une page de veille économique d'UNE feuille A4 pour des étudiants. "
            f"Lecteur : {config['reader_profile'].strip()}\n"
            "Règles impératives :\n"
            "- Tu t'appuies uniquement sur les textes fournis ; tu n'inventes aucun fait ni chiffre.\n"
            f"- Écris en {config['brief']['language']}, phrases courtes, sans jargon.\n"
            f"- Respecte STRICTEMENT les limites de mots : points {lim['point_words']} mots, "
            f"titres {lim['title_words']} mots, phrases d'explication {lim['blurb_words']} mots, "
            f"focus {lim['focus_sentences']} phrases.\n"
            "- Jamais de « X% », de valeur manquante ou de commentaire sur le manque d'information : "
            "si une valeur n'est pas dans les textes, ne la mentionne pas.\n"
            "- Chaque titre reformule l'article (pas de copier-coller) et reste factuel.")
        try:
            out = llm.structured(system, _prompt(focus, dive, reads_by_theme, candidates, config),
                                 "rediger_page", page_schema(lim, len(candidates)),
                                 max_tokens=2000, model=model_for(config, "synthesis_model"))
        except Exception as e:
            if getattr(e, "status_code", None) in (400, 401, 403):
                raise                       # clé invalide : inutile d'insister
            log.warning("Rédaction de la page ignorée, texte de repli utilisé : %s", e)

    # --- L'essentiel : 3 points (repli : première phrase des meilleurs résumés)
    top_texts = [(dive.summary if (dive and it is focus) else it.summary_fr)
                 for it in ([focus] if focus else []) + [c[1] for c in candidates]]
    points = [p for p in (_clean(p, "", lim["point_words"]) for p in out.get("points", []) if isinstance(p, str)) if p]
    for text in top_texts:
        if len(points) >= lim["points"]:
            break
        fallback = _first_sentence(text, lim["point_words"])
        if fallback and not has_placeholder(fallback) and fallback not in points:
            points.append(fallback)
    points = points[: lim["points"]]

    # --- Focus : titre, résumé, chiffres vérifiés, lien
    focus_block = None
    if focus is not None:
        base = dive.summary if dive else focus.summary_fr
        figures = []
        for f in (dive.key_figures if dive else [])[: lim["figures"]]:
            if not has_placeholder(f.get("value"), f.get("label")):
                figures.append({"value": plain(f["value"]), "url": f.get("source_url") or focus.article.url,
                                "label": fit_words(plain(f["label"]), lim["figure_label_words"])})
        link = (dive.sources_read[0] if dive and dive.sources_read else focus.article.url)
        focus_block = {
            "title": _clean(out.get("focus_title", ""), focus.article.title, lim["title_words"]),
            "summary": fit_sentences(_clean(out.get("focus_summary", ""), base, 10_000),
                                     lim["focus_sentences"], lim["focus_words"]),
            "figures": figures,
            "source": focus.article.source if _is_google_news(link) else _domain(link),
            "url": link,
        }

    # --- À lire : l'id renvoyé par le modèle retrouve l'article ; sans réponse, repli sur le titre et le résumé
    by_id = {r.get("id"): r for r in out.get("reads", []) if isinstance(r, dict)}
    themes = []
    index = 0
    for theme, items in reads_by_theme.items():
        reads = []
        for it in items:
            r = by_id.get(index, {})
            index += 1
            reads.append({
                "title": _clean(r.get("title", ""), it.article.title, lim["title_words"]),
                "blurb": _clean(r.get("blurb", ""), _first_sentence(it.summary_fr, lim["blurb_words"]),
                                lim["blurb_words"]),
                "source": it.article.source, "url": it.article.url,
            })
        if reads:
            themes.append({"key": theme, "label": config["themes"][theme]["label"], "reads": reads})

    question = _clean(out.get("question", ""), "", lim["question_words"])
    subtitle = _clean(out.get("subtitle", ""), "L'essentiel de l'actualité économique, bancaire et IA",
                      lim["subtitle_words"])
    return {"date": f"{date:%d/%m/%Y}", "subtitle": subtitle, "points": points, "focus": focus_block,
            "themes": themes, "question": question, "analyzed": analyzed_count}
