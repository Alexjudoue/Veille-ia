"""Boucle agentique réduite à l'essentiel : sans réseau, sans clé API, bibliothèque standard seule.

PIPELINE ou AGENT ?
- Pipeline : le CODE décide de l'ordre des étapes (collecte, analyse, envoi), toujours les mêmes.
- Agent : le MODÈLE choisit sa prochaine action (lire, chercher, conclure) selon ce qu'il découvre.
- Le code, lui, exécute les outils et CONTRÔLE : budget, échecs, vérification des chiffres.
- En une phrase : le modèle propose, le code dispose.
Version réelle : veille/agent.py, fonction investigate().
Lancer : python docs/boucle_agentique_simplifiee.py [--budget 2]
"""
import argparse
import sys

TEXTE_LU = "La Banque d'Angleterre relève son taux directeur à 4,25 %."
SCENARIO = [                                                    # décisions écrites à l'avance
    ("read_article", {"url": "lemonde.fr"}),                    # site payant : échec
    ("search_news", {"query": "Banque d'Angleterre taux"}),     # il cherche une autre source
    ("read_article", {"url": "reuters.com"}),                   # succès
    ("submit_analysis", {"key_figures": ["4,25 %", "7 %"]}),    # « 7 % » est inventé
]


class ToolError(Exception):
    """Échec attendu d'un outil : on le raconte au modèle, on ne plante pas."""


def faux_modele(tour, force):
    """Remplace le vrai LLM. Si le code force la conclusion, il n'a plus le choix."""
    return SCENARIO[-1] if force else SCENARIO[tour - 1]


def read_article(args, lus):
    if args["url"] == "lemonde.fr":
        raise ToolError("site payant")
    lus[args["url"]] = TEXTE_LU                 # on retient ce qui a RÉELLEMENT été lu
    return TEXTE_LU


def search_news(args, lus):
    return "reuters.com : La BoE relève ses taux"


OUTILS = {"read_article": read_article, "search_news": search_news}


def conclure(analyse, lus):
    """Garde-fou dans le code : on ne fait pas confiance au modèle sur les chiffres."""
    lu = " ".join(lus.values())
    gardes = [c for c in analyse["key_figures"] if c in lu]
    return {"statut": "ok" if lus else "repli_v1", "gardes": gardes,
            "retires": [c for c in analyse["key_figures"] if c not in gardes]}


def investiguer(budget=5):
    lus, appels = {}, 0
    for tour in range(1, budget + 3):           # plafond absolu : la boucle se termine toujours
        force = appels >= budget                # (4) budget atteint : conclusion forcée
        nom, args = faux_modele(tour, force)    # (1) le modèle choisit sa prochaine action
        print(f"Tour {tour} · le modèle demande : {nom}({args.get('url') or args.get('query', '')})"
              + (" [forcé]" if force else ""))
        if nom == "submit_analysis":            # (2) il conclut : on vérifie, puis on sort
            bilan = conclure(args, lus)
            print("Conclusion acceptée : "
                  f"{len(bilan['gardes'])} chiffre gardé, {len(bilan['retires'])} chiffre retiré"
                  if lus else "Conclusion rejetée : aucun article lu, on garde le résumé v1")
            return bilan
        appels += 1                             # (3) sinon on exécute l'outil
        try:
            print(f"→ Résultat : {OUTILS[nom](args, lus)}")
        except ToolError as e:                  # un échec est une INFORMATION pour le modèle
            print(f"→ Échec renvoyé au modèle : {e}")
    return conclure({"key_figures": []}, lus)   # filet de sécurité


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--budget", type=int, default=5, help="nombre max d'appels d'outils")
    investiguer(p.parse_args().budget)
