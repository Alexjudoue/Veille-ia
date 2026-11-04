"""Point d'entrée : collecte -> analyse -> brief -> envoi.

Exemples :
    python main.py --dry-run     # collecte seule, sans appel au LLM (gratuit)
    python main.py               # brief complet : PDF d'une page dans briefs/AAAA-MM-JJ.pdf
    python main.py --send        # + envoi sur Telegram
    python main.py --email       # + envoi par e-mail
    python main.py --test-email  # renvoie le dernier PDF par e-mail (aucun appel au LLM)
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
from veille.agent import run_agent
from veille.analyze import analyze
from veille.brief import render_telegram, select
from veille.page import build_page
from veille.pdf import render_pdf
from veille.collect import collect
from veille.deliver import send_email, send_telegram
from veille.memory import Memory


def main() -> None:
    p = argparse.ArgumentParser(description="Agent de veille économique")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--dry-run", action="store_true", help="collecte seulement, sans LLM")
    p.add_argument("--no-memory", action="store_true", help="ignore les articles déjà vus")
    p.add_argument("--no-agent", action="store_true", help="désactive l'agent d'approfondissement (comportement V1)")
    p.add_argument("--send", action="store_true", help="envoie le brief sur Telegram")
    p.add_argument("--email", action="store_true", help="envoie le PDF du brief par e-mail")
    p.add_argument("--test-email", action="store_true",
                   help="envoie le dernier PDF existant par e-mail, sans collecte ni LLM (gratuit)")
    p.add_argument("--limit", type=int, default=0, help="nombre max d'articles analysés (tests)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    load_dotenv()
    logging.info("Agent de veille %s", __version__)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)

    if args.test_email:
        # Test d'envoi : on réutilise le brief le plus récent, sans rien régénérer
        briefs = sorted(Path("briefs").glob("????-??-??.pdf"))
        if not briefs:
            logging.error("Aucun PDF dans briefs/ : lance d'abord python main.py.")
            raise SystemExit(1)
        last = briefs[-1]
        d = datetime.strptime(last.stem, "%Y-%m-%d")
        ok = send_email(last.read_bytes(), last.name, f"[Test] Veille du {d:%d/%m/%Y}")
        raise SystemExit(0 if ok else 1)

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
    from veille.llm import FREE_PROVIDERS, make_llm, model_for, provider_of
    try:
        llm = make_llm(config)       # fournisseur choisi dans config.yaml (llm.provider)
    except (RuntimeError, ValueError) as e:     # clé manquante, fournisseur inconnu
        logging.error("%s", e)
        raise SystemExit(1)
    logging.info("Fournisseur : %s, modèle : %s", provider_of(config), model_for(config))
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

    # 3 bis. Agent (V2) : approfondit les événements les mieux notés
    today = f"{now:%Y-%m-%d}"
    deep_dives, traces = [], []
    if config.get("agent", {}).get("enabled") and not args.no_agent:
        deep_dives, traces = run_agent(selected, llm, memory, config, today)
    # 3 ter. Une seule page : rédaction courte (limites appliquées par le code), puis PDF A4
    try:
        page = build_page(selected, deep_dives, llm, config, now, len(analyzed))
    except Exception as e:
        logging.error("Arrêt : la page n'a pas pu être rédigée.\n  %s", getattr(e, "message", e))
        raise SystemExit(1)

    stats = {
        "collected": len(articles),
        "analyzed": len(analyzed),
        "selected": sum(len(v) for v in selected.values()),
        "calls": llm.usage.calls,
        "input_tokens": llm.usage.input_tokens,
        "output_tokens": llm.usage.output_tokens,
        "cache_write_tokens": llm.usage.cache_write_tokens,
        "cache_read_tokens": llm.usage.cache_read_tokens,
        "cost_usd": round(llm.usage.cost_usd, 5),
        "provider": provider_of(config),
        "model": model_for(config),
        "free_tier": provider_of(config) in FREE_PROVIDERS,
    }
    if traces:   # coût de l'agent, séparé de celui du pipeline
        stats["agent"] = {"events": len(traces), "deep_dives": len(deep_dives),
                          "calls": llm.agent_usage.calls,
                          "input_tokens": llm.agent_usage.input_tokens,
                          "output_tokens": llm.agent_usage.output_tokens,
                          "cache_write_tokens": llm.agent_usage.cache_write_tokens,
                          "cache_read_tokens": llm.agent_usage.cache_read_tokens,
                          "articles_read": sum(t.get("articles_read", 0) for t in traces),
                          "cost_usd": round(llm.agent_usage.cost_usd, 5)}
    stats["total_cost_usd"] = round(llm.usage.cost_usd + llm.agent_usage.cost_usd, 5)
    stats["duration_s"] = round((datetime.now(timezone.utc) - now).total_seconds())
    try:
        pdf, pages = render_pdf(page)          # une seule page A4, réduite automatiquement si besoin
    except Exception as e:
        logging.error("Arrêt : le PDF n'a pas pu être généré (%s: %s).", type(e).__name__, e)
        logging.error("Aucun brief écrit, aucun article marqué comme lu.")
        raise SystemExit(1)
    stats["pages"] = pages

    out_dir = Path("briefs")
    out_dir.mkdir(exist_ok=True)
    pdf_path = out_dir / f"{now:%Y-%m-%d}.pdf"
    pdf_path.write_bytes(pdf)
    # Les données brutes servent à évaluer la qualité de l'agent (V3)
    (out_dir / f"{now:%Y-%m-%d}.json").write_text(
        json.dumps({"stats": stats, "page": page,
                    "deep_dives": [d.to_dict() for d in deep_dives],
                    "analyzed": [a.to_dict() for a in analyzed]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if traces:
        # Trace de chaque boucle : outils appelés, arguments, durées, tokens
        (out_dir / f"{now:%Y-%m-%d}.trace.json").write_text(
            json.dumps({"date": today, "events": traces}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    logging.info("Brief écrit : %s (%d page)", pdf_path, pages)
    logging.info("Usage : %s", stats)

    # 4. Mémoire et envoi
    memory.mark_seen(articles)
    memory.save_analyzed(analyzed, f"{now:%Y-%m-%d}")
    if args.send:
        send_telegram(render_telegram(selected, {"key_points": page["points"]}, config, now))
    if args.email and not send_email(pdf, pdf_path.name, f"Veille du {now:%d/%m/%Y}"):
        # Le brief est écrit et la mémoire sauvegardée ; mais un e-mail perdu ne doit pas
        # passer inaperçu (GitHub Actions marque alors l'exécution en échec et vous prévient).
        raise SystemExit(1)


if __name__ == "__main__":
    main()
