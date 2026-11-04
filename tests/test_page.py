"""Tests du contenu de la page et du PDF d'une seule page A4, sans réseau."""
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pypdf import PdfReader

from veille import pdf as P
from veille.models import AnalyzedArticle, Article, DeepDive
from veille.page import build_page, limits, pick_focus, pick_reads
from veille.shorten import count_words

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 4, 7, 45, tzinfo=timezone.utc)
LIM = limits(CONFIG)


def item(i, theme="marches", score=8, url=None, source="Reuters"):
    art = Article(id=f"a{i}", title=f"Titre de l'article numéro {i}", url=url or f"https://media{i}.fr/{i}",
                  source=source, published=NOW)
    return AnalyzedArticle(article=art, theme=theme, relevance=score,
                           summary_fr=f"Résumé de l'article {i}. Deuxième phrase.", why_it_matters="Important.")


def selected_set(n=4):
    return {"marches": [item(i, "marches", 9 - i) for i in range(n)],
            "banques": [item(10 + i, "banques", 9 - i) for i in range(n)],
            "ia_tech": [item(20 + i, "ia_tech", 9 - i) for i in range(n)]}


def dive_for(it):
    figs = [("2,5 %", "taux de dépôt de la banque centrale européenne"), ("3,1 %", "inflation"),
            ("9 %", "chômage"), ("7 %", "quatrième chiffre")]
    return DeepDive(item=it, summary="Analyse enrichie. Seconde phrase. Troisième phrase. Quatrième phrase.",
                    key_figures=[{"value": v, "label": lab, "source_url": "https://x.fr/a"} for v, lab in figs],
                    sources_read=["https://www.lu.fr/article"], confidence="élevée")


class PageLLM:
    """Faux modèle : rend un résultat (trop long volontairement) ou lève une erreur."""
    def __init__(self, out=None, fail=None):
        self.out, self.fail, self.calls = out, fail, []

    def structured(self, system, prompt, tool_name, schema, **kw):
        self.calls.append((system, prompt, tool_name, schema))
        if self.fail:
            raise self.fail
        return self.out


LONG = " ".join(f"mot{i}" for i in range(80)) + "."


def verbose_output():
    return {"subtitle": LONG, "points": [LONG, LONG, LONG, LONG], "focus_title": LONG, "focus_summary": LONG,
            "reads": [{"id": i, "title": LONG, "blurb": LONG} for i in range(9)], "question": LONG}


# --- structure et limites -------------------------------------------------------------------

def test_limits_come_from_config():
    assert LIM["points"] == 3 and LIM["point_words"] == 25 and LIM["reads_per_theme"] == 3
    assert CONFIG["agent"]["deep_dive_count"] <= 2


def test_schema_and_prompt_carry_the_limits():
    llm = PageLLM(verbose_output())
    build_page(selected_set(), [], llm, CONFIG, NOW, 20)
    system, prompt, name, schema = llm.calls[0]
    assert name == "rediger_page"
    assert schema["properties"]["points"]["maxItems"] == 3
    assert schema["properties"]["points"]["items"]["maxLength"] == 25 * 10
    assert "25 mots" in system and "10 mots" in system and "20 mots" in system
    assert "[0]" in prompt and "FOCUS DU JOUR" in prompt


def test_every_text_respects_its_limit_even_if_the_model_overshoots():
    sel = selected_set()
    page = build_page(sel, [dive_for(sel["marches"][0])], PageLLM(verbose_output()), CONFIG, NOW, 20)
    assert len(page["points"]) == 3
    assert all(count_words(p) <= LIM["point_words"] + 1 for p in page["points"])     # +1 : le « … »
    assert count_words(page["subtitle"]) <= LIM["subtitle_words"] + 1
    assert count_words(page["question"]) <= LIM["question_words"] + 1
    assert count_words(page["focus"]["title"]) <= LIM["title_words"] + 1
    assert count_words(page["focus"]["summary"]) <= LIM["focus_words"] + 1
    for t in page["themes"]:
        assert len(t["reads"]) <= LIM["reads_per_theme"]
        for r in t["reads"]:
            assert count_words(r["title"]) <= LIM["title_words"] + 1
            assert count_words(r["blurb"]) <= LIM["blurb_words"] + 1


def test_focus_is_the_deep_dive_with_verified_figures_and_limits():
    sel = selected_set()
    out = {**verbose_output(), "focus_summary": ""}      # vide : le code reprend l'analyse de l'agent
    page = build_page(sel, [dive_for(sel["marches"][1])], PageLLM(out), CONFIG, NOW, 20)
    f = page["focus"]
    assert len(f["figures"]) == 3 and f["figures"][0]["value"] == "2,5 %"     # chiffres vérifiés de l'agent seulement
    assert count_words(f["figures"][0]["label"]) <= LIM["figure_label_words"] + 1
    assert f["summary"] == "Analyse enrichie. Seconde phrase. Troisième phrase."      # 3 phrases maximum
    assert f["url"] == "https://www.lu.fr/article" and f["source"] == "lu.fr"
    # le focus ne figure pas deux fois
    assert all(r["url"] != sel["marches"][1].article.url for t in page["themes"] for r in t["reads"])


def test_without_deep_dive_focus_falls_back_to_best_article_without_figures():
    out = {**verbose_output(), "focus_summary": ""}
    page = build_page(selected_set(), [], PageLLM(out), CONFIG, NOW, 20)
    assert page["focus"]["figures"] == []
    assert page["focus"]["summary"].startswith("Résumé de l'article")


def test_reads_prefer_direct_links_over_google_news_on_ties():
    g = item(1, url="https://news.google.com/rss/articles/XYZ", score=8)
    direct = item(2, url="https://lemonde.fr/x", score=8)
    assert pick_reads({"marches": [g, direct]}, None, 1)["marches"] == [direct]
    best, _ = pick_focus({"marches": [g, direct]}, [])
    assert best is direct


def test_model_failure_gives_a_complete_fallback_page():
    page = build_page(selected_set(), [], PageLLM(fail=RuntimeError("sortie invalide")), CONFIG, NOW, 20)
    assert len(page["points"]) == 3 and page["focus"]["title"] and all(t["reads"] for t in page["themes"])
    assert page["question"] == ""        # pas de question inventée : la ligne est simplement omise


def test_key_error_is_not_swallowed():
    class Err(Exception):
        status_code = 401

    with pytest.raises(Err):
        build_page(selected_set(), [], PageLLM(fail=Err("clé")), CONFIG, NOW, 20)


def test_placeholder_text_from_the_model_is_replaced():
    out = {**verbose_output(), "points": ["Le taux monte à X%.", "Point sain.", "Y% aussi"], "question": "Valeur à Y%?"}
    page = build_page(selected_set(), [], PageLLM(out), CONFIG, NOW, 20)
    assert "Point sain." in page["points"] and not any("X%" in p or "Y%" in p for p in page["points"])
    assert page["question"] == ""


def test_empty_selection_gives_an_empty_but_valid_page():
    page = build_page({"marches": [], "banques": [], "ia_tech": []}, [], PageLLM({}), CONFIG, NOW, 0)
    assert page["focus"] is None and page["themes"] == [] and page["points"] == []
    pdf, pages = P.render_pdf(page)
    assert pages == 1 and pdf.startswith(b"%PDF")


# --- PDF : une seule page, liens, réduction automatique -------------------------------------------------

def make_page(n_reads=3, text_words=20):
    words = " ".join(["mot"] * text_words)
    return {"date": "04/11/2026", "subtitle": "Sous-titre", "points": [words] * 3,
            "focus": {"title": "Focus", "summary": words, "figures": [{"value": "2,5 %", "label": "taux", "url": "u"}] * 3,
                      "source": "lu.fr", "url": "https://www.lu.fr/a"},
            "themes": [{"key": k, "label": k.title(), "reads": [
                {"title": f"Titre {k}{i}", "blurb": words, "source": "Media", "url": f"https://m.fr/{k}/{i}"}
                for i in range(n_reads)]} for k in ("marches", "banques", "ia_tech")],
            "question": "Pourquoi ?", "analyzed": 42}


def pdf_info(pdf: bytes):
    r = PdfReader(io.BytesIO(pdf))
    links = [a.get_object()["/A"]["/URI"] for pg in r.pages for a in (pg.get("/Annots") or [])]
    return len(r.pages), "\n".join(pg.extract_text() for pg in r.pages), links, r.pages[0].mediabox


def test_normal_page_is_one_a4_page_with_clickable_links():
    pdf, pages = P.render_pdf(make_page())
    n, text, links, box = pdf_info(pdf)
    assert pages == n == 1
    assert round(float(box.width)) == 595 and round(float(box.height)) == 842     # A4 en points
    assert "https://www.lu.fr/a" in links and "https://m.fr/marches/0" in links
    assert "Veille du 04/11/2026" in text and "42 articles analysés" in text
    assert "pertinence" not in text and "confiance" not in text.lower()


def test_overflowing_content_is_reduced_not_spilled_to_a_second_page():
    pdf, pages = P.render_pdf(make_page(n_reads=3, text_words=110))
    n, _, links, _ = pdf_info(pdf)
    assert n == 1
    assert sum(1 for u in links if u.startswith("https://m.fr/")) < 9      # moins d'articles qu'au départ


def test_impossible_page_raises_instead_of_producing_two_pages():
    huge = make_page(n_reads=3, text_words=110)
    huge["points"] = [" ".join(["mot"] * 400)] * 3
    huge["focus"]["summary"] = " ".join(["mot"] * 900)
    with pytest.raises(P.PageOverflow):
        P.render_pdf(huge)


def test_html_in_text_is_escaped():
    page = make_page()
    page["points"][0] = "<script>alert(1)</script> & <b>gras</b>"
    pdf, _ = P.render_pdf(page)
    _, text, _, _ = pdf_info(pdf)
    assert "<script>" in text and "<b>gras</b>" in text      # affiché tel quel, pas interprété
