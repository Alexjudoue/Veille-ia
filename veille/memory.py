"""Mémoire SQLite : retient ce qui a déjà été envoyé pour ne pas répéter
les mêmes articles d'un jour à l'autre. C'est aussi la base de la V2,
où l'agent suivra l'évolution d'un sujet dans le temps."""
from __future__ import annotations

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
