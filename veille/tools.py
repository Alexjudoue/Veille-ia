"""Outils de l'agent (V2) : ce que le modèle peut « faire » pendant sa boucle.

Chaque outil est une fonction Python ordinaire, testable sans LLM. En cas
d'échec prévisible (site payant, 403, délai dépassé), il lève `ToolError` :
l'agent renvoie alors le message au modèle, qui peut essayer autre chose.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

import feedparser
import requests

from .collect import _clean, _parse_date, _source_name

log = logging.getLogger(__name__)

# Beaucoup de sites refusent les robots : on se présente comme un navigateur.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}

MIN_TEXT_CHARS = 300   # en dessous : page vide, mur de consentement ou paywall


class ToolError(Exception):
    """Échec attendu d'un outil. Le message est renvoyé tel quel au modèle."""


# --- Liens Google News -------------------------------------------------------

def is_google_news(url: str) -> bool:
    return "news.google.com/" in url and "/articles/" in url


def decode_google_news(url: str, timeout: int = 10) -> str:
    """Retrouve l'adresse réelle d'un article derrière un lien Google News.

    Les liens des flux RSS Google News (news.google.com/rss/articles/CBMi...)
    ne contiennent plus l'adresse en clair. On reproduit ce que fait le
    navigateur :
      1. on ouvre la page Google News de l'article, qui contient une
         signature (data-n-a-sg) et un horodatage (data-n-a-ts) ;
      2. on les envoie au service interne « batchexecute » de Google,
         qui répond avec l'adresse réelle.
    Ce service n'est pas documenté : s'il change, on lève ToolError et
    l'agent se rabat sur un autre lien ou sur search_news.
    """
    article_id = url.split("/articles/")[1].split("?")[0]
    try:
        page = requests.get(f"https://news.google.com/rss/articles/{article_id}",
                            headers=HEADERS, timeout=timeout).text
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not (sg and ts):
            raise ToolError("lien Google News non résolu (signature absente)")

        inner = json.dumps([
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
              None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            article_id, int(ts.group(1)), sg.group(1),
        ])
        body = "f.req=" + quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
        resp = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=body, timeout=timeout,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
    except requests.RequestException as e:
        raise ToolError(f"lien Google News non résolu ({type(e).__name__})") from e

    # Réponse : une ligne de garde « )]}' », une ligne vide, puis du JSON où
    # l'adresse est elle-même encodée en JSON : ["garturlres", "<url>", 1]
    try:
        entries = json.loads(resp.text.split("\n\n", 1)[1])
        for entry in entries:
            if entry[0] == "wrb.fr" and entry[1] == "Fbv4je":
                real = json.loads(entry[2])[1]
                if real.startswith("http"):
                    return real
    except (IndexError, TypeError, ValueError):
        pass
    raise ToolError("lien Google News non résolu (réponse illisible)")


# --- Outil read_article --------------------------------------------------------

def read_article(url: str, max_chars: int = 6000, timeout: int = 15) -> tuple[str, str]:
    """Télécharge une page et en extrait le texte principal.

    Renvoie (adresse réelle, texte tronqué à max_chars).
    """
    real = decode_google_news(url) if is_google_news(url) else url
    try:
        resp = requests.get(real, headers=HEADERS, timeout=timeout)
    except requests.Timeout as e:
        raise ToolError("délai dépassé") from e
    except requests.RequestException as e:
        raise ToolError(f"connexion impossible ({type(e).__name__})") from e

    if resp.status_code in (401, 402, 403):
        raise ToolError(f"accès refusé (HTTP {resp.status_code}) : site protégé ou payant")
    if not resp.ok:
        raise ToolError(f"page indisponible (HTTP {resp.status_code})")

    import trafilatura  # import ici : la bibliothèque est lourde à charger

    text = trafilatura.extract(resp.text, include_comments=False, include_tables=False) or ""
    if len(text) < MIN_TEXT_CHARS:
        raise ToolError("texte trop court : page vide, mur de consentement ou paywall")
    return real, text[:max_chars]


# --- Outil search_news ---------------------------------------------------------

def search_news(query: str, lang: str = "fr", limit: int = 8, timeout: int = 15) -> list[dict]:
    """Recherche complémentaire via le flux RSS de recherche Google News
    (7 derniers jours, aucune clé API nécessaire)."""
    edition = "hl=fr&gl=FR&ceid=FR:fr" if lang == "fr" else "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={quote(query + ' when:7d')}&{edition}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ToolError(f"recherche impossible ({type(e).__name__})") from e

    results = []
    for entry in feedparser.parse(resp.content).entries[:limit]:
        link, title = entry.get("link"), _clean(entry.get("title", ""))
        if not link or not title:
            continue
        source = _source_name(entry, link)
        if title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        published = _parse_date(entry)
        results.append({"title": title, "source": source, "url": link,
                        "date": f"{published:%d/%m/%Y}" if published else ""})
    return results
