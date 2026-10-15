"""Point d'entrée : collecte -> analyse -> brief -> envoi.

Exemples :
    python main.py --dry-run     # collecte seule, sans appel au LLM (gratuit)
    python main.py               # brief complet dans briefs/AAAA-MM-JJ.md
    python main.py --send        # + envoi sur Telegram
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from veille import __version__
from veille.analyze import analyze
from veille.brief import render_markdown, render_telegram, select, synthesize
from veille.collect import collect
from veille.deliver import send_telegram
from veille.memory import Memory


def main() -> None:
    p = argparse.ArgumentParser(description="Agent de veille économique")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--dry-run", action="store_true", help="collecte seulement, sans LLM")
    p.add_argument("--no-memory", action="store_true", help="ignore les articles déjà vus")
    p.add_argument("--send", action="store_true", help="envoie le brief sur Telegram")
    p.add_argument("--limit", type=int, default=0, help="nombre max d'articles analysés (tests)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    load_dotenv()
    logging.info("Agent de veille %s", __version__)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)

    # 1. Collecte
    articles = collect(config)
    memory = Memory()
    if not args.no_memory:
        articles = memory.filter_new(articles)
        logging.info("%d articles jamais vus", len(articles))
    if args.limit:
        articles = articles[: args.limit]

    if args.dry_run:
        for a in articles:
            print(f"[{a.feed_theme:8}] {a.published:%d/%m %H:%M}  {a.source[:20]:20}  {a.title[:90]}")
        print(f"\n{len(articles)} articles prêts à être analysés (dry-run : aucun appel au LLM).")
        return

    if not articles:
        logging.warning("Aucun nouvel article : pas de brief aujourd'hui.")
        return

    # 2. Analyse
    from veille.llm import LLM
    llm = LLM(config["llm"]["model"])
    try:
        analyzed = analyze(articles, llm, config)
    except Exception as e:
        logging.error("Arrêt : l'API a refusé la requête.\n  %s", getattr(e, "message", e))
        logging.error("Aucun brief écrit, aucun article marqué comme lu.")
        raise SystemExit(1)
    if not analyzed:
        logging.error("Aucun article n'a pu être analysé : pas de brief, mémoire inchangée.")
        raise SystemExit(1)

    # 3. Sélection et synthèse
    selected = select(analyzed, config, llm)
    synthesis = synthesize(selected, llm, config)

    stats = {
        "collected": len(articles),
        "analyzed": len(analyzed),
        "selected": sum(len(v) for v in selected.values()),
        "calls": llm.usage.calls,
        "input_tokens": llm.usage.input_tokens,
        "output_tokens": llm.usage.output_tokens,
    }
    md = render_markdown(selected, synthesis, config, now, stats)

    out_dir = Path("briefs")
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / f"{now:%Y-%m-%d}.md"
    md_path.write_text(md, encoding="utf-8")
    # Les données brutes servent à évaluer la qualité de l'agent (V3)
    (out_dir / f"{now:%Y-%m-%d}.json").write_text(
        json.dumps({"stats": stats, "synthesis": synthesis,
                    "analyzed": [a.to_dict() for a in analyzed]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logging.info("Brief écrit : %s", md_path)
    logging.info("Usage : %s", stats)

    # 4. Mémoire et envoi
    memory.mark_seen(articles)
    memory.save_analyzed(analyzed, f"{now:%Y-%m-%d}")
    if args.send:
        send_telegram(render_telegram(selected, synthesis, config, now))


if __name__ == "__main__":
    main()
