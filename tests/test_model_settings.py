"""Model choice and temperature per provider (admin AI settings). No LLM calls; settings are faked."""

import pytest

from ai import agent, answer_config

GEMINI, OPENAI, CLAUDE = "google:gemini-3.5-flash-lite", "openai:gpt-4.1-mini", "anthropic:claude-haiku-4-5-20251001"


def test_one_best_tested_model_per_provider_is_approved():
    assert {GEMINI, OPENAI, CLAUDE} <= set(answer_config.APPROVED_MODELS)
    assert "openai:gpt-4.1-nano" not in answer_config.APPROVED_MODELS      # wrong totals in tests
    assert "anthropic:claude-opus-4-7" not in answer_config.APPROVED_MODELS  # rejects temperature


def test_temperature_range_follows_the_provider():
    assert answer_config.temperature_range(GEMINI) is None                  # Gemini 3.5 ignores it
    assert (answer_config.temperature_range(OPENAI)["min"], answer_config.temperature_range(OPENAI)["max"]) == (0.0, 2.0)
    assert (answer_config.temperature_range(CLAUDE)["min"], answer_config.temperature_range(CLAUDE)["max"]) == (0.0, 1.0)


@pytest.mark.parametrize("model,value", [(GEMINI, 0.2), (OPENAI, 2.5), (OPENAI, -0.1), (CLAUDE, 1.5), (CLAUDE, True)])
def test_temperature_outside_the_providers_range_is_refused(model, value):
    with pytest.raises(answer_config.ConfigError):
        answer_config.set_config("customer", "llm", model, "admin", temperature=value)


def _saving(monkeypatch, saved_temperature):
    """Fake settings store: returns what set_config saved."""
    saved = {}
    settings = {"temperature_by_role": {"customer": saved_temperature}}
    monkeypatch.setattr(answer_config.store, "settings", lambda refresh=False: settings)
    monkeypatch.setattr(answer_config, "_save", lambda key, value, admin: saved.__setitem__(key, value))
    monkeypatch.setattr(answer_config, "role_config", lambda role: {})
    return saved


def test_switching_to_gemini_clears_the_temperature(monkeypatch):
    saved = _saving(monkeypatch, 0.2)
    answer_config.set_config("customer", "llm", GEMINI, "admin")
    assert saved["temperature_by_role"]["customer"] is None


def test_switching_provider_drops_a_value_outside_the_new_range(monkeypatch):
    saved = _saving(monkeypatch, 1.8)                                        # fine for OpenAI, too high for Claude
    answer_config.set_config("customer", "llm", CLAUDE, "admin")
    assert saved["temperature_by_role"]["customer"] is None


def test_a_valid_temperature_is_saved(monkeypatch):
    saved = _saving(monkeypatch, None)
    answer_config.set_config("customer", "llm", OPENAI, "admin", temperature=0.2)
    assert saved["temperature_by_role"]["customer"] == 0.2


@pytest.mark.parametrize("model,stored,sent", [
    (OPENAI, 0.2, {"temperature": 0.2}),
    (CLAUDE, 0.2, {"temperature": 0.2}),    # Pydantic AI moves it into Claude's raw request field
    (GEMINI, 0.2, {}),                      # never sent to a model that ignores it
    (CLAUDE, 1.5, {}),                      # out of range is never sent
    (OPENAI, None, {}),                     # model default
])
def test_agent_sends_temperature_only_where_it_applies(monkeypatch, model, stored, sent):
    monkeypatch.setattr(agent.store, "settings", lambda refresh=False: {"temperature_by_role": {"customer": stored}})
    assert agent.temperature_for("customer", model) == sent
