"""Test de la version pédagogique de la boucle agentique (docs/boucle_agentique_simplifiee.py)."""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "boucle_pedagogique", Path(__file__).parent.parent / "docs" / "boucle_agentique_simplifiee.py")
boucle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boucle)


def test_le_chiffre_invente_est_retire(capsys):
    bilan = boucle.investiguer()
    assert bilan["statut"] == "ok"
    assert bilan["gardes"] == ["4,25 %"]          # présent dans le texte lu
    assert bilan["retires"] == ["7 %"]            # inventé par le modèle
    assert "1 chiffre gardé, 1 chiffre retiré" in capsys.readouterr().out


def test_budget_trop_petit_force_la_conclusion(capsys):
    bilan = boucle.investiguer(budget=2)
    assert bilan["statut"] == "repli_v1"          # rien n'a été lu : aucun chiffre ne passe
    assert bilan["gardes"] == []
    assert "[forcé]" in capsys.readouterr().out
