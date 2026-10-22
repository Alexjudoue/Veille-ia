"""Tests du prompt caching et du calcul de coût, sans réseau ni clé."""
from types import SimpleNamespace

import pytest

from veille import llm as L


def test_cost_estimate_with_cache():
    # 1 M de tokens : 0,10 $ plein tarif, 0,01 $ relus du cache, 0,125 $ écrits ; 0,50 $ en sortie
    assert L.estimate_cost("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    assert L.estimate_cost("claude-haiku-5-5", 0, 0, cache_read=1_000_000) == pytest.approx(0.01)
    assert L.estimate_cost("claude-haiku-5-5", 0, 0, cache_write=1_000_000) == pytest.approx(0.125)
    assert L.estimate_cost("claude-haiku-5-5", 0, 1_000_000) == pytest.approx(0.50)
    assert L.estimate_cost("modele-inconnu", 1000, 1000) == 0.0


def test_usage_counts_cache_tokens():
    u = L.Usage()
    resp = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_creation_input_tokens=400, cache_read_input_tokens=900))
    L._record(u, resp, "claude-haiku-5-5")
    assert (u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.calls) == (100, 400, 900, 1)
    assert u.total_input == 1400 and u.cost_usd > 0
    # un double de test sans champs de cache (ancienne forme) reste accepté
    L._record(u, SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=5)), "")
    assert u.calls == 2 and u.input_tokens == 110


def test_breakpoint_on_last_block_without_mutating_history():
    history = [{"role": "user", "content": "Bonjour"},
               {"role": "assistant", "content": [SimpleNamespace(type="text", text="ok")]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"},
                                            {"type": "text", "text": "Budget épuisé"}]}]
    out = L.with_cache_breakpoint(history)
    assert out[-1]["content"][-1]["cache_control"] == L.CACHE
    assert "cache_control" not in out[-1]["content"][0]          # un seul marqueur
    assert "cache_control" not in history[-1]["content"][-1]     # l'historique n'est pas modifié

    first = L.with_cache_breakpoint(history[:1])                 # message de départ : texte simple
    assert first[0]["content"][0]["cache_control"] == L.CACHE
    assert history[0]["content"] == "Bonjour"


def test_system_prompt_is_marked_for_cache():
    block, = L._cached_system("Tu es analyste.")
    assert block["cache_control"] == L.CACHE and block["text"] == "Tu es analyste."
