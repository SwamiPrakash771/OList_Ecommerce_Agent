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

ANSWER_MODES = ("llm", "hybrid")
CONFIGURABLE_ROLES = ("customer", "seller")

# Models tested with this project's evaluations. Notes are shown to the admin.
APPROVED_MODELS: dict[str, str] = {
    "google:gemini-3.5-flash-lite": "Gemini 3.5 Flash Lite: fully tested (37/37 questions, red-team passed)",
    "google:gemini-3.1-flash-lite": "Gemini 3.1 Flash Lite: smoke-tested only (one question); run the "
                                    "evaluations before relying on it",
}

ROLE_STATUS = {
    "customer": {"assistant_live": True, "hybrid_supported": True,
                 "note": "Customer support assistant is live."},
    "seller": {"assistant_live": False, "hybrid_supported": False,
               "note": "The seller assistant isn't built yet (sellers get a placeholder reply). These choices are "
                       "saved and apply when it launches. Hybrid needs seller-specific local answers, which "
                       "don't exist yet, so until then hybrid behaves like LLM only for sellers."},
}


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
        **ROLE_STATUS[role],
    }


def get_config() -> dict:
    from ai import local_embeddings

    return {
        "roles": [role_config(r) for r in CONFIGURABLE_ROLES],
        "options": {
            "answer_modes": [
                {"value": "llm", "label": "LLM only",
                 "description": "Every question is answered by the LLM."},
                {"value": "hybrid", "label": "Hybrid (local + LLM)",
                 "description": "Simple questions (order and refund status, cart, address, policies, polite "
                                "declines) are answered on our server without the LLM: free and ~0.1 s. Everything "
                                "else goes to the LLM. Measured: 37/37 correct, 27% cheaper."},
            ],
            "models": [{"value": None, "label": f"Server default ({default_model() or 'not set'})"}]
                      + [{"value": m, "label": note} for m, note in APPROVED_MODELS.items()],
        },
        "local_model_loaded": local_embeddings.is_ready(),
        "guests_allowed": guests_allowed(),
    }


def set_config(role: str, answer_mode_value: str, model: str | None, admin_username: str) -> dict:
    if role not in CONFIGURABLE_ROLES:
        raise ConfigError(f"Unknown role '{role}'.")
    if answer_mode_value not in ANSWER_MODES:
        raise ConfigError(f"Answer mode must be one of: {', '.join(ANSWER_MODES)}.")
    if model is not None and model not in APPROVED_MODELS:
        raise ConfigError("That model isn't on the approved list.")
    s = store.settings(refresh=True)
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
