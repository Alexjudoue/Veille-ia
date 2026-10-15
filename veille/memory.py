"""Mémoire SQLite : retient ce qui a déjà été envoyé pour ne pas répéter
les mêmes articles d'un jour à l'autre. C'est aussi la base de la V2,
où l'agent suivra l'évolution d'un sujet dans le temps."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import AnalyzedArticle, Article

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    id TEXT PRIMARY KEY,
    url TEXT,
    title TEXT,
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS analyzed (
    id TEXT PRIMARY KEY,
    date TEXT,
    theme TEXT,
    relevance INTEGER,
    title TEXT,
    url TEXT,
    source TEXT,
    summary_fr TEXT,
    why_it_matters TEXT,
    entities TEXT
);
"""


class Memory:
    def __init__(self, path: str | Path = "data/veille.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def filter_new(self, articles: list[Article]) -> list[Article]:
        if not articles:
            return []
        ids = [a.id for a in articles]
        q = f"SELECT id FROM seen WHERE id IN ({','.join('?' * len(ids))})"
        known = {row[0] for row in self.db.execute(q, ids)}
        return [a for a in articles if a.id not in known]

    def mark_seen(self, articles: list[Article]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.executemany(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)",
            [(a.id, a.url, a.title, now) for a in articles],
        )
        self.db.commit()

    def search(self, query: str, before_date: str, limit: int = 8) -> list[dict]:
        """Cherche les articles analysés AVANT `before_date` (AAAA-MM-JJ)
        qui parlent du même sujet. Utilisé par l'outil search_memory de l'agent.

        Méthode volontairement simple : on découpe la requête en mots-clés et
        on compte combien apparaissent dans le titre, le résumé ou les entités.
        Les articles qui en contiennent le plus remontent en premier.
        (LIKE ignore la casse pour les lettres sans accent seulement.)
        """
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 3][:8]
        if not words:
            return []
        haystack = "(title || ' ' || summary_fr || ' ' || entities)"
        score = " + ".join(f"({haystack} LIKE ?)" for _ in words)
        q = f"""
            SELECT date, title, source, summary_fr, why_it_matters, score FROM (
                SELECT *, ({score}) AS score FROM analyzed WHERE date < ?
            ) WHERE score > 0
            ORDER BY score DESC, date DESC
            LIMIT ?
        """
        params = [f"%{w}%" for w in words] + [before_date, limit]
        cols = ("date", "title", "source", "summary_fr", "why_it_matters", "score")
        return [dict(zip(cols, row)) for row in self.db.execute(q, params)]

    def save_analyzed(self, items: list[AnalyzedArticle], date: str) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO analyzed VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (i.article.id, date, i.theme, i.relevance, i.article.title,
                 i.article.url, i.article.source, i.summary_fr,
                 i.why_it_matters, ", ".join(i.entities))
                for i in items
            ],
        )
        self.db.commit()
