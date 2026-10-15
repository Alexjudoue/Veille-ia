"""Étape 1 : collecte des articles depuis les flux RSS, puis nettoyage."""
from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

import feedparser
import requests

from .models import Article

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (veille-ia; projet étudiant)"


def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _article_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def _source_name(entry, link: str) -> str:
    # Google News met le vrai média dans entry.source
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return src["title"]
    # Sinon, le domaine de l'article (« bfmtv.com ») : plus fiable que le titre
    # du flux, souvent peu parlant (« Homepage Economie - actualités »).
    return urlparse(link).netloc.removeprefix("www.")


def fetch_feed(url: str, theme: str, timeout: int, max_items: int) -> list[Article]:
    """Télécharge un flux. Un flux en panne ne doit jamais casser toute la veille."""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Flux ignoré (%s) : %s", url, e)
        return []

    feed = feedparser.parse(resp.content)
    articles = []
    for entry in feed.entries[:max_items]:
        link = entry.get("link")
        title = _clean(entry.get("title", ""))
        published = _parse_date(entry)
        if not link or not title or not published:
            continue
        # Google News ajoute " - Nom du média" à la fin du titre
        source = _source_name(entry, link)
        if title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        articles.append(
            Article(
                id=_article_id(link),
                title=title,
                url=link,
                source=source,
                published=published,
                summary=_clean(entry.get("summary", ""))[:600],
                feed_theme=theme,
            )
        )
    log.info("%3d articles  <- %s", len(articles), url[:80])
    return articles


def filter_recent(articles: list[Article], hours: int, now: datetime | None = None) -> list[Article]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    return [a for a in articles if a.published >= cutoff]


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9àâäéèêëîïôöùûüç ]", "", t.lower())


def deduplicate(articles: list[Article], threshold: float = 0.85) -> list[Article]:
    """Supprime les doublons : même URL, ou titres quasi identiques
    (la même dépêche reprise par plusieurs médias)."""
    kept: list[Article] = []
    seen_ids: set[str] = set()
    for a in sorted(articles, key=lambda x: x.published, reverse=True):
        if a.id in seen_ids:
            continue
        nt = _norm_title(a.title)
        if any(SequenceMatcher(None, nt, _norm_title(k.title)).ratio() >= threshold for k in kept):
            continue
        kept.append(a)
        seen_ids.add(a.id)
    return kept


def collect(config: dict) -> list[Article]:
    c = config["collect"]
    all_articles: list[Article] = []
    for theme, spec in config["themes"].items():
        for url in spec["feeds"]:
            all_articles += fetch_feed(url, theme, c["timeout_seconds"], c["max_per_feed"])
    recent = filter_recent(all_articles, c["lookback_hours"])
    unique = deduplicate(recent)
    log.info("Collecte : %d bruts -> %d récents -> %d uniques",
             len(all_articles), len(recent), len(unique))
    return unique
