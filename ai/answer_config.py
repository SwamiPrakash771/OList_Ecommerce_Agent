"""How each role's assistant answers: the admin's choice of answer mode and LLM model.

- answer mode: "llm" (every question goes to the LLM) or "hybrid" (simple questions are answered
  on our server without the LLM, the rest by the LLM; see docs/HYBRID_MODEL.md).
- model: one of APPROVED_MODELS, or None for the server default (AI_MODEL in .env).

Stored in ai_settings (answer_mode_by_role, model_by_role) and changed from the admin panel's
AI Settings page. Only approved models can be chosen, so an admin can't point the assistant at an
untested model by mistake.
"""

from __future__ import annotations

import os

import db
from ai import store

LOCAL_EMBEDDINGS_ENABLED = os.environ.get("AI_LOCAL_EMBEDDINGS_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
ANSWER_MODES = ("llm", "hybrid") if LOCAL_EMBEDDINGS_ENABLED else ("llm",)
CONFIGURABLE_ROLES = ("customer", "seller", "admin")

# Prompt caching. Gemini and OpenAI cache an identical start of the request by themselves (implicit), so
# the system prompt and tool definitions are kept identical and first. Claude caches only what is marked:
# these mark the system prompt, the tool definitions and the conversation so far, reused for 5 minutes
# (writing the cache costs 1.25x once, reading it 0.1x; every turn makes 2-3 calls, so it pays off at once).
# Other providers ignore anthropic_* keys, so they are always sent: a Claude fallback model gets them too.
PROMPT_CACHE_SETTINGS: dict[str, bool] = {
    "anthropic_cache_instructions": True,
    "anthropic_cache_tool_definitions": True,
    "anthropic_cache": True,
}

# Models tested with this project's evaluations (docs/MODEL_COMPARISON.md): the best-tested model of
# each provider, plus the fallback. Notes are shown to the admin.
APPROVED_MODELS: dict[str, str] = {
    "google:gemini-3.5-flash-lite": "Google Gemini 3.5 Flash Lite: fully tested (37/37 questions, 5/5 tricky, "
                                    "red-team passed); about $2 per 1,000 questions",
    "openai:gpt-4.1-mini": "OpenAI GPT-4.1 mini: 37/37 questions and 5/5 tricky at temperature 0-1; red-team not "
                           "run yet; about $1-2 per 1,000 questions",
    "anthropic:claude-haiku-4-5-20251001": "Anthropic Claude Haiku 4.5: 37/37 questions and 5/5 tricky at "
                                           "temperature 0-0.5; red-team not run yet; about $11 per 1,000 "
                                           "questions (no prompt caching yet)",
    "google:gemini-3.1-flash-lite": "Google Gemini 3.1 Flash Lite: smoke-tested only (one question); run the "
                                    "evaluations before relying on it",
}

# Temperature per provider (docs/TEMPERATURE_TEST.md). None = the model ignores it (Gemini 3.5+).
# "caution": values at or above it gave wrong answers in our tests.
TEMPERATURE: dict[str, dict | None] = {
    "google": None,
    "openai": {"min": 0.0, "max": 2.0, "model_default": 1.0, "recommended": 0.2, "caution": 1.5,
               "caution_note": "In tests, 1.5 offered actions the assistant can't do and 2.0 added gibberish."},
    "anthropic": {"min": 0.0, "max": 1.0, "model_default": 1.0, "recommended": 0.2, "caution": 1.0,
                  "caution_note": "In tests, 1.0 gave one wrong total (a dropped date filter)."},
}


def provider(model: str | None) -> str:
    return (model or "").split(":", 1)[0]


def temperature_range(model: str | None) -> dict | None:
    """The temperature range of a model's provider, or None if the model ignores temperature."""
    return TEMPERATURE.get(provider(model))

ROLE_STATUS = {
    "customer": {"assistant_live": True, "hybrid_supported": True,
                 "note": "Customer support assistant is live."},
    "seller": {"assistant_live": True, "hybrid_supported": False,
               "note": "Seller assistant is live (a separate agent: its own tools, engine and database user; "
                       "docs/seller/). Not yet evaluated or red-teamed. Hybrid has no seller local answers, "
                       "so it behaves like LLM only for sellers."},
    "admin": {"assistant_live": True, "hybrid_supported": False,
              "note": "Admin assistant is live (a separate, read-only agent: its own tools, engine and database "
                      "user; docs/admin/). Not yet evaluated or red-teamed. Hybrid has no admin local answers, "
                      "so it behaves like LLM only for admins. Admin chats are kept 1 year as an audit trail."},
}


MAX_QUESTIONS_LIMIT = 10_000   # highest number an admin can enter (None = unlimited)
KEEP = object()                # set_config: leave this setting as it is
# History sent with each question (docs/CONTEXT_PLAN.md): highest numbers an admin can enter.
MAX_FULL_TURNS = 50
MAX_QA_TURNS = 1_000


def _check_count(value, label: str, low: int, high: int) -> None:
    if value is KEEP or value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{label} must be a whole number from {low} to {high:,}, or unlimited.")


class ConfigError(ValueError):
    """An invalid choice; the message is shown to the admin."""


# Evaluation runs only (python -m ai.eval_natural --mode ...): force a mode in this process without
# changing the live setting. Never set by the API.
EVAL_OVERRIDE: dict[str, str] = {}


def answer_mode(role: str) -> str:
    """'llm' or 'hybrid' for a role (defaults to 'llm')."""
    if role in EVAL_OVERRIDE:
        return EVAL_OVERRIDE[role]
    s = store.settings()
    mode = (s.get("answer_mode_by_role") or {}).get(role)
    return mode if mode in ANSWER_MODES else "llm"


def guests_allowed() -> bool:
    """Whether visitors who aren't signed in may use the assistant (ai_settings.roles_enabled.guest)."""
    return bool((store.settings().get("roles_enabled") or {}).get("guest", False))


def set_guest_access(enabled: bool, admin_username: str) -> bool:
    roles = {**(store.settings(refresh=True).get("roles_enabled") or {}), "guest": bool(enabled)}
    _save("roles_enabled", roles, admin_username)
    store.settings(refresh=True)
    return guests_allowed()


def default_model() -> str | None:
    return os.environ.get("AI_MODEL") or None


def role_config(role: str) -> dict:
    s = store.settings()
    chosen = (s.get("model_by_role") or {}).get(role)
    return {
        "role": role,
        "answer_mode": answer_mode(role),
        "model": chosen,                                  # None = server default
        "effective_model": chosen or default_model(),
        "max_questions_per_chat": (s.get("max_questions_per_chat") or {}).get(role),   # None = unlimited
        "temperature": (s.get("temperature_by_role") or {}).get(role),                 # None = model default
        "temperature_range": temperature_range(chosen or default_model()),
        **dict(zip(("history_full_turns", "history_qa_turns"), _window(role))),      # None = unlimited
        **{**ROLE_STATUS[role], "hybrid_supported": ROLE_STATUS[role]["hybrid_supported"] and
           LOCAL_EMBEDDINGS_ENABLED},
    }


def get_config() -> dict:
    from ai import local_embeddings

    return {
        "roles": [role_config(r) for r in CONFIGURABLE_ROLES],
        "options": {
            "answer_modes": [
                {"value": "llm", "label": "LLM only",
                 "description": "Every question is answered by the LLM."},
            ] + ([{"value": "hybrid", "label": "Hybrid (local + LLM)",
                 "description": "Simple questions (order and refund status, cart, address, policies, polite "
                                "declines) are answered on our server without the LLM: free and ~0.1 s. Everything "
                                "else goes to the LLM. Measured: 37/37 correct, 27% cheaper."}]
                if LOCAL_EMBEDDINGS_ENABLED else []),
            "models": [{"value": None, "label": f"Server default ({default_model() or 'not set'})",
                        "provider": provider(default_model()), "temperature": temperature_range(default_model())}]
                      + [{"value": m, "label": note, "provider": provider(m), "temperature": temperature_range(m)}
                         for m, note in APPROVED_MODELS.items()],
        },
        "local_model_enabled": LOCAL_EMBEDDINGS_ENABLED,
        "local_model_loaded": local_embeddings.is_ready(),
        "guests_allowed": guests_allowed(),
    }


def _window(role: str) -> tuple[int | None, int | None]:
    from ai import agent

    return agent.history_window(role)


def set_config(role: str, answer_mode_value: str, model: str | None, admin_username: str,
               max_questions_per_chat=KEEP, history_full_turns=KEEP, history_qa_turns=KEEP,
               temperature=KEEP) -> dict:
    """max_questions_per_chat: questions one chat may have. history_full_turns / history_qa_turns: earlier
    turns sent in full / as question + answer with each new question. None = unlimited; KEEP = unchanged."""
    if role not in CONFIGURABLE_ROLES:
        raise ConfigError(f"Unknown role '{role}'.")
    if answer_mode_value not in ANSWER_MODES:
        raise ConfigError(f"Answer mode must be one of: {', '.join(ANSWER_MODES)}.")
    if model is not None and model not in APPROVED_MODELS:
        raise ConfigError("That model isn't on the approved list.")
    _check_count(max_questions_per_chat, "Questions per chat", 1, MAX_QUESTIONS_LIMIT)
    _check_count(history_full_turns, "Full-detail turns", 0, MAX_FULL_TURNS)
    _check_count(history_qa_turns, "Question + answer turns", 0, MAX_QA_TURNS)
    temp_range = temperature_range(model or default_model())
    if temperature is not KEEP and temperature is not None:
        if temp_range is None:
            raise ConfigError("This model ignores temperature; leave it on the model default.")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not (
                temp_range["min"] <= temperature <= temp_range["max"]):
            raise ConfigError(f"Temperature for this model must be from {temp_range['min']} to {temp_range['max']}.")
    s = store.settings(refresh=True)
    # Re-checked on every save: a model that ignores temperature never keeps one, and switching
    # model drops a value outside the new model's range.
    current = (s.get("temperature_by_role") or {}).get(role)
    value = current if temperature is KEEP else temperature
    if value is not None and (temp_range is None or not temp_range["min"] <= value <= temp_range["max"]):
        value = None
    _save("temperature_by_role", {**(s.get("temperature_by_role") or {}), role: value}, admin_username)
    if history_full_turns is not KEEP or history_qa_turns is not KEEP:
        full, qa = _window(role)
        window = {"full_turns": full if history_full_turns is KEEP else history_full_turns,
                  "qa_turns": qa if history_qa_turns is KEEP else history_qa_turns}
        _save("history_window_by_role", {**(s.get("history_window_by_role") or {}), role: window}, admin_username)
    if max_questions_per_chat is not KEEP:
        _save("max_questions_per_chat", {**(s.get("max_questions_per_chat") or {}), role: max_questions_per_chat},
              admin_username)
    modes = {**(s.get("answer_mode_by_role") or {}), role: answer_mode_value}
    models = {**(s.get("model_by_role") or {}), role: model}
    _save("answer_mode_by_role", modes, admin_username)
    _save("model_by_role", models, admin_username)
    store.settings(refresh=True)
    if answer_mode_value == "hybrid" and ROLE_STATUS[role]["hybrid_supported"]:
        _load_local_model_in_background()
    return role_config(role)


def _save(key: str, value, admin_username: str) -> None:
    import json

    db.execute(
        "INSERT INTO ai_settings (setting_key, value, description, updated_by, updated_at) VALUES (%s, %s, '', %s, NOW()) "
        "ON DUPLICATE KEY UPDATE value = VALUES(value), updated_by = VALUES(updated_by), updated_at = NOW()",
        (key, json.dumps(value), f"admin:{admin_username}"[:64]),
    )


def _load_local_model_in_background() -> None:
    """Switching to hybrid at runtime: load the local model without blocking the admin's request.
    Until it's loaded, questions go to the LLM."""
    if not LOCAL_EMBEDDINGS_ENABLED:
        return
    from ai import fast_path, local_embeddings

    local_embeddings.load_in_background(fast_path.warm_up)   # once, even if saved several times


def usage_last_days(days: int = 7) -> list[dict]:
    """Per role: assistant replies, how many were answered locally, and LLM cost (for the admin page)."""
    rows = db.query_df(
        """
        SELECT c.owner_role AS role, COUNT(*) AS replies,
               SUM(m.model = 'local:fast-path') AS local_replies,
               COALESCE(SUM(m.input_tokens), 0) AS input_tokens, COALESCE(SUM(m.output_tokens), 0) AS output_tokens
        FROM ai_messages m JOIN ai_conversations c USING (conversation_id)
        WHERE m.role = 'assistant' AND m.created_at >= NOW() - INTERVAL %s DAY
        GROUP BY c.owner_role
        """,
        (days,),
    ).to_dict("records")
    cost = {r["role"]: float(r["cost"]) for r in db.query_df(
        "SELECT role, COALESCE(SUM(cost), 0) AS cost FROM ai_usage WHERE created_at >= NOW() - INTERVAL %s DAY "
        "GROUP BY role", (days,)).to_dict("records")}
    return [{"role": r["role"], "replies": int(r["replies"]), "local_replies": int(r["local_replies"] or 0),
             "input_tokens": int(r["input_tokens"]), "output_tokens": int(r["output_tokens"]),
             "cost_usd": round(cost.get(r["role"], 0.0), 4)} for r in rows]
