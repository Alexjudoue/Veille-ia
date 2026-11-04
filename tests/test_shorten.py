"""Tests de la troncature : en fin de phrase, jamais au milieu d'un mot ni d'un chiffre."""
import pytest

from veille.shorten import count_words, fit_sentences, fit_words


def test_short_text_is_untouched():
    assert fit_words("Une phrase courte.", 10) == "Une phrase courte."


def test_cuts_at_sentence_end_when_possible():
    text = "La BCE maintient ses taux. Les marchés réagissent calmement. Fin de séance."
    assert fit_words(text, 8) == "La BCE maintient ses taux."
    assert fit_words(text, 9) == "La BCE maintient ses taux. Les marchés réagissent calmement."


def test_never_cuts_a_word():
    original = "Les investisseurs anticipent une décision spectaculaire demain matin à Francfort"
    out = fit_words(original, 4)
    assert out.endswith("…")
    assert all(w in original.split() for w in out.rstrip("…").split())
    assert count_words(out.rstrip("…")) <= 4


@pytest.mark.parametrize("text,limit,expected_end", [
    ("Le rendement grimpe de 4,2 % en séance après les élections", 5, "4,2 %…"),
    ("Apollo a atteint 350 milliards de dollars de prêts sur un an", 4, "350 milliards…"),
    ("Hausse de 25 points de base annoncée par la banque centrale", 4, "25 points…"),
])
def test_number_stays_with_its_unit(text, limit, expected_end):
    assert fit_words(text, limit).endswith(expected_end)


def test_no_dangling_function_word():
    out = fit_words("Les taux de la zone euro baissent nettement", 3)      # coupe après « de la »
    assert out.rstrip("…").split()[-1] not in {"de", "la", "les", "du"}


def test_decimal_point_is_not_a_sentence_end():
    out = fit_words("Le taux passe à 2.5 pour cent aujourd'hui. Ensuite il baisse.", 9)
    assert out == "Le taux passe à 2.5 pour cent aujourd'hui."


def test_fit_sentences_limits_count_and_words():
    text = "Un fait. Deux faits ici. Trois faits là. Quatre faits."
    assert fit_sentences(text, 3) == "Un fait. Deux faits ici. Trois faits là."
    assert count_words(fit_sentences(text, 3, 4)) <= 4
    assert fit_sentences("", 3) == ""
