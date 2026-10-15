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
