"""Génération du PDF : gabarit HTML/CSS (Jinja2) converti par WeasyPrint.

Pourquoi WeasyPrint ? Il lit du HTML/CSS ordinaire (@page A4, flexbox, polices système), produit des
liens cliquables et dit combien de pages il a faites : c'est ce qui permet de GARANTIR une seule page.
Il tourne sur GitHub Actions (ubuntu-latest) avec les bibliothèques Pango (voir le workflow).

Une seule page : si le rendu déborde, on réduit par étapes (moins d'articles par thème, moins de
chiffres, police un peu plus petite) et on refait le rendu, plutôt que de passer à deux pages.
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)
for noisy in ("weasyprint", "fontTools"):          # ces bibliothèques bavardent en INFO
    logging.getLogger(noisy).setLevel(logging.WARNING)

TEMPLATES = Path(__file__).parent / "templates"

# Étapes de réduction : (articles max par thème, chiffres max, échelle de la police)
SHRINK_STEPS = [(3, 3, 1.00), (2, 3, 1.00), (2, 2, 1.00), (1, 2, 1.00),
                (1, 2, 0.95), (1, 1, 0.92), (0, 1, 0.92)]


class PageOverflow(RuntimeError):
    """Même réduite au maximum, la page ne tient pas sur une feuille."""


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]),
                       trim_blocks=True, lstrip_blocks=True)


def _reduced(page: dict, reads: int, figures: int) -> dict:
    """Copie de la page avec moins d'articles par thème et moins de chiffres."""
    p = copy.deepcopy(page)
    if p.get("focus"):
        p["focus"]["figures"] = p["focus"]["figures"][:figures]
    p["themes"] = [{**t, "reads": t["reads"][:reads]} for t in p["themes"] if t["reads"][:reads]]
    return p


def _load_weasyprint():
    """Import tardif : les tests qui n'ont pas besoin du PDF tournent sans Pango installé."""
    if os.name == "nt" and not os.environ.get("WEASYPRINT_DLL_DIRECTORIES"):
        # Windows : Pango vient de MSYS2 (voir README) ; on le cherche à l'emplacement habituel
        for candidate in (r"C:\msys64\mingw64\bin", r"C:\msys64\ucrt64\bin"):
            if Path(candidate).exists():
                os.environ["WEASYPRINT_DLL_DIRECTORIES"] = candidate
                break
    from weasyprint import HTML
    return HTML


def render_pdf(page: dict) -> tuple[bytes, int]:
    """Renvoie (octets du PDF, nombre de pages = 1). Lève PageOverflow si rien ne tient."""
    HTML = _load_weasyprint()
    template = _env().get_template("brief.html")
    for reads, figures, scale in SHRINK_STEPS:
        html = template.render(page=_reduced(page, reads, figures), scale=scale)
        document = HTML(string=html, base_url=str(TEMPLATES)).render()
        if len(document.pages) == 1:
            if (reads, figures, scale) != SHRINK_STEPS[0]:
                log.info("Page réduite pour tenir sur une feuille : %d article(s) par thème, "
                         "%d chiffre(s), police x%.2f", reads, figures, scale)
            return document.write_pdf(), 1
    raise PageOverflow("La page dépasse une feuille A4 même au format le plus réduit.")
