from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class Article:
    """Un article brut issu d'un flux RSS."""

    id: str                 # hash stable de l'URL
    title: str
    url: str
    source: str
    published: datetime
    summary: str = ""
    feed_theme: str = ""    # thème du flux d'origine (indice, le LLM tranche)


@dataclass
class AnalyzedArticle:
    """Un article après passage dans le LLM."""

    article: Article
    theme: str
    relevance: int          # 0 à 10
    summary_fr: str
    why_it_matters: str
    entities: list[str] = field(default_factory=list)
    # Autres articles sur le même événement : [{"title", "url", "source"}]
    related: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["article"]["published"] = self.article.published.isoformat()
        return d


@dataclass
class DeepDive:
    """Analyse approfondie d'un événement par l'agent (V2)."""

    item: AnalyzedArticle            # l'événement d'origine (résumé v1 conservé)
    summary: str                     # résumé enrichi, fondé sur les articles lus
    key_figures: list[dict] = field(default_factory=list)   # [{value, label, source_url}]
    context: str = ""
    what_changed: str = ""           # ce qui change depuis les jours précédents
    confidence: str = "moyenne"      # élevée | moyenne | faible
    sources_read: list[str] = field(default_factory=list)   # adresses réellement lues
    # Activité de l'agent : articles lus, recherches, appels au modèle, tokens
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "article_id": self.item.article.id,
            "title": self.item.article.title,
            "summary": self.summary,
            "key_figures": self.key_figures,
            "context": self.context,
            "what_changed": self.what_changed,
            "confidence": self.confidence,
            "sources_read": self.sources_read,
            "stats": self.stats,
        }
