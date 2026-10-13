"""Tables for AI conversations, usage, limits and settings, and for the job scheduler.

    python -m ai.schema        create missing tables and insert default limits/settings

Safe to re-run: tables are created only if missing, and defaults are inserted only where
no value exists yet (values changed later, e.g. from the admin page, are kept).

These tables are not reachable through agent tools. The chat engine and the API read and
write them on the user's behalf; the agent's database user may INSERT/UPDATE them but not
DELETE (retention purges run as the app user, see ai/scheduler.py).
"""

import json

import db

AI_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ai_conversations (
        conversation_id CHAR(32) PRIMARY KEY,
        owner_user_id   INT NULL,
        owner_role      VARCHAR(12) NOT NULL,
        session_id      CHAR(32) NULL,
        title           VARCHAR(120) NULL,
        status          VARCHAR(12) NOT NULL DEFAULT 'active',
        retention_class VARCHAR(16) NOT NULL DEFAULT 'standard',
        ticket_id       VARCHAR(36) NULL,
        rating          TINYINT NULL,
        model           VARCHAR(64) NULL,
        prompt_version  VARCHAR(32) NULL,
        created_at      DATETIME NOT NULL,
        updated_at      DATETIME NOT NULL,
        last_message_at DATETIME NULL,
        deleted_at      DATETIME NULL,
        retention_until DATETIME NULL,
        INDEX idx_ai_conv_owner (owner_user_id, last_message_at),
        INDEX idx_ai_conv_session (session_id),
        INDEX idx_ai_conv_retention (retention_class, last_message_at),
        CHECK (owner_role IN ('guest', 'customer', 'seller', 'admin')),
        CHECK (status IN ('active', 'closed', 'escalated')),
        CHECK (retention_class IN ('standard', 'ticket_linked', 'legal_hold')),
        CHECK (owner_user_id IS NOT NULL OR session_id IS NOT NULL)
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_messages (
        message_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
        conversation_id  CHAR(32) NOT NULL,
        sequence_no      INT NOT NULL,
        role             VARCHAR(12) NOT NULL,
        content          JSON NOT NULL,
        model            VARCHAR(64) NULL,
        prompt_version   VARCHAR(32) NULL,
        tool_set_version VARCHAR(32) NULL,
        input_tokens     INT NULL,
        output_tokens    INT NULL,
        cached_tokens    INT NULL,
        created_at       DATETIME NOT NULL,
        UNIQUE KEY uq_ai_msg_seq (conversation_id, sequence_no),
        CONSTRAINT fk_ai_msg_conv FOREIGN KEY (conversation_id)
            REFERENCES ai_conversations (conversation_id) ON DELETE CASCADE,
        CHECK (role IN ('user', 'assistant', 'tool', 'system'))
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_tool_calls (
        call_id              BIGINT AUTO_INCREMENT PRIMARY KEY,
        conversation_id      CHAR(32) NOT NULL,
        message_id           BIGINT NULL,
        tool_name            VARCHAR(64) NOT NULL,
        kind                 VARCHAR(16) NOT NULL,
        args                 JSON NULL,
        status               VARCHAR(20) NOT NULL,
        error_category       VARCHAR(40) NULL,
        duration_ms          INT NULL,
        confirmed_by_user_id INT NULL,
        created_at           DATETIME NOT NULL,
        INDEX idx_ai_tool_conv (conversation_id),
        INDEX idx_ai_tool_name (tool_name, created_at),
        CONSTRAINT fk_ai_tool_conv FOREIGN KEY (conversation_id)
            REFERENCES ai_conversations (conversation_id) ON DELETE CASCADE,
        CONSTRAINT fk_ai_tool_msg FOREIGN KEY (message_id)
            REFERENCES ai_messages (message_id) ON DELETE CASCADE,
        CHECK (status IN ('ok', 'error', 'denied', 'not_found', 'needs_confirmation', 'invalid_args'))
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_pending_actions (
        action_id       CHAR(32) PRIMARY KEY,
        conversation_id CHAR(32) NOT NULL,
        user_id         INT NOT NULL,
        tool_name       VARCHAR(64) NOT NULL,
        args            JSON NOT NULL,
        preview         JSON NOT NULL,
        status          VARCHAR(12) NOT NULL DEFAULT 'pending',
        idempotency_key CHAR(64) NOT NULL,
        result          JSON NULL,
        created_at      DATETIME NOT NULL,
        expires_at      DATETIME NOT NULL,
        decided_at      DATETIME NULL,
        UNIQUE KEY uq_ai_pending_idem (idempotency_key),
        INDEX idx_ai_pending_status (status, expires_at),
        CONSTRAINT fk_ai_pending_conv FOREIGN KEY (conversation_id)
            REFERENCES ai_conversations (conversation_id) ON DELETE CASCADE,
        CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired', 'executed', 'failed'))
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_usage (
        request_id      CHAR(32) PRIMARY KEY,
        subject         VARCHAR(48) NOT NULL,
        user_id         INT NULL,
        role            VARCHAR(12) NOT NULL,
        conversation_id CHAR(32) NULL,
        source          VARCHAR(8) NOT NULL DEFAULT 'chat',
        job_name        VARCHAR(64) NULL,
        model           VARCHAR(64) NOT NULL,
        input_tokens    INT NOT NULL DEFAULT 0,
        output_tokens   INT NOT NULL DEFAULT 0,
        cached_tokens   INT NOT NULL DEFAULT 0,
        cost            DECIMAL(12,6) NOT NULL DEFAULT 0,
        created_at      DATETIME NOT NULL,
        INDEX idx_ai_usage_subject (subject, created_at),
        INDEX idx_ai_usage_created (created_at),
        CHECK (source IN ('chat', 'job'))
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_usage_daily (
        usage_date    DATE NOT NULL,
        subject       VARCHAR(48) NOT NULL,
        role          VARCHAR(12) NOT NULL,
        requests      INT NOT NULL DEFAULT 0,
        input_tokens  BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        cached_tokens BIGINT NOT NULL DEFAULT 0,
        cost          DECIMAL(14,6) NOT NULL DEFAULT 0,
        PRIMARY KEY (usage_date, subject)
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_limits (
        scope                   VARCHAR(48) PRIMARY KEY,
        messages_per_day        INT NULL,
        input_tokens_per_day    BIGINT NULL,
        output_tokens_per_day   BIGINT NULL,
        max_reply_tokens        INT NULL,
        requests_per_minute     INT NULL,
        max_tool_calls_per_turn INT NULL,
        blocked                 TINYINT(1) NOT NULL DEFAULT 0,
        note                    VARCHAR(255) NULL,
        updated_by              VARCHAR(64) NULL,
        updated_at              DATETIME NOT NULL
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_settings (
        setting_key VARCHAR(64) PRIMARY KEY,
        value       JSON NOT NULL,
        description VARCHAR(255) NULL,
        updated_by  VARCHAR(64) NULL,
        updated_at  DATETIME NOT NULL
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_feedback (
        feedback_id INT AUTO_INCREMENT PRIMARY KEY,
        message_id  BIGINT NOT NULL,
        user_id     INT NULL,
        rating      VARCHAR(4) NOT NULL,
        reason      VARCHAR(255) NULL,
        created_at  DATETIME NOT NULL,
        UNIQUE KEY uq_ai_feedback (message_id, user_id),
        CONSTRAINT fk_ai_feedback_msg FOREIGN KEY (message_id)
            REFERENCES ai_messages (message_id) ON DELETE CASCADE,
        CHECK (rating IN ('up', 'down'))
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_admin_access_log (
        log_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        admin_user_id   INT NOT NULL,
        conversation_id CHAR(32) NOT NULL,
        action          VARCHAR(12) NOT NULL,
        reason          VARCHAR(255) NOT NULL,
        created_at      DATETIME NOT NULL,
        INDEX idx_ai_access_conv (conversation_id, created_at),
        INDEX idx_ai_access_admin (admin_user_id, created_at),
        CHECK (action IN ('view', 'unmask', 'export'))
    ) ENGINE=InnoDB
    """,
]

SCHEDULER_DDL = [
    """
    CREATE TABLE IF NOT EXISTS scheduler_jobs (
        job_name        VARCHAR(64) PRIMARY KEY,
        description     VARCHAR(255) NOT NULL,
        schedule        VARCHAR(32) NOT NULL,
        enabled         TINYINT(1) NOT NULL DEFAULT 1,
        timeout_s       INT NOT NULL DEFAULT 600,
        max_retries     INT NOT NULL DEFAULT 2,
        next_run_at     DATETIME NULL,
        last_run_id     BIGINT NULL,
        last_success_at DATETIME NULL,
        updated_by      VARCHAR(64) NULL,
        updated_at      DATETIME NOT NULL
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduler_runs (
        run_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        job_name        VARCHAR(64) NOT NULL,
        triggered_by    VARCHAR(12) NOT NULL,
        scheduled_for   DATETIME NULL,
        started_at      DATETIME NOT NULL,
        finished_at     DATETIME NULL,
        status          VARCHAR(12) NOT NULL,
        attempt_no      INT NOT NULL DEFAULT 1,
        retry_of_run_id BIGINT NULL,
        worker_id       VARCHAR(80) NOT NULL,
        rows_processed  BIGINT NULL,
        tokens_used     BIGINT NULL,
        cost            DECIMAL(12,6) NULL,
        duration_ms     INT NULL,
        output_summary  VARCHAR(1000) NULL,
        error_code      VARCHAR(64) NULL,
        error_summary   VARCHAR(1000) NULL,
        INDEX idx_sched_runs_job (job_name, started_at),
        INDEX idx_sched_runs_status (status, started_at),
        CONSTRAINT fk_sched_runs_job FOREIGN KEY (job_name) REFERENCES scheduler_jobs (job_name),
        CHECK (triggered_by IN ('schedule', 'manual', 'retry', 'backfill')),
        CHECK (status IN ('running', 'success', 'failed', 'timeout', 'skipped'))
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduler_state (
        job_name   VARCHAR(64) PRIMARY KEY,
        watermark  JSON NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT fk_sched_state_job FOREIGN KEY (job_name) REFERENCES scheduler_jobs (job_name)
    ) ENGINE=InnoDB
    """,
]

# Starting limits from docs/AI_PLAN.md section 8.2 (editable later from the admin page).
DEFAULT_LIMITS = {
    #               msgs/day  input/day   output/day  max reply  req/min  tools/turn
    "role:guest":    (10,       30_000,      5_000,     500,       3,       4),
    "role:customer": (50,      300_000,     40_000,   1_000,       6,       8),
    "role:seller":   (100,     800_000,    100_000,   2_000,      10,       8),
    "role:admin":    (300,   3_000_000,    300_000,   4_000,      20,      10),
}

DEFAULT_SETTINGS = {
    "ai_enabled": (True, "Global kill switch for all AI assistants"),
    "roles_enabled": ({"guest": True, "customer": True, "seller": True, "admin": True},
                      "Turn the assistant on or off per role"),
    "write_actions_enabled": ({"customer": True, "seller": True, "admin": True},
                              "Whether each role's agent may propose changes (cart, address, tickets, product edits)"),
    "model_by_role": ({"guest": None, "customer": None, "seller": None, "admin": None},
                      "Model per role; empty until a provider is chosen"),
    "effort_by_role": ({"guest": "low", "customer": "medium", "seller": "medium", "admin": "high"},
                       "Reasoning effort per role (for models that support it)"),
    "monthly_budget_usd": (100, "Platform-wide monthly AI budget; alerts at 80% and 100%"),
    "prompt_version": ("v1", "Current system prompt version, stored on every message"),
    "conversation_retention_days": (90, "Standard conversations are purged after this many days of inactivity"),
    "deleted_conversation_grace_days": (30, "User-deleted conversations are hard-deleted after this many days"),
    "pending_action_ttl_minutes": (15, "Unconfirmed actions expire after this many minutes"),
    "answer_mode_by_role": ({"guest": "llm", "customer": "llm", "seller": "llm", "admin": "llm"},
                            "How each role's assistant answers: llm (every question to the LLM) or hybrid (simple "
                            "questions answered on our server without the LLM; see docs/HYBRID_MODEL.md). Set from "
                            "the admin panel's AI Settings page"),
    "fallback_model": ("google:gemini-3.1-flash-lite",
                       "Model tried when the main one fails (rate limit, quota, outage); must be an approved model "
                       "(ai/answer_config.py). Empty = no fallback"),
    "temperature_by_role": ({"guest": None, "customer": None, "seller": None},
                            "Sampling temperature per role for models that accept it (OpenAI 0-2, Claude 0-1); "
                            "null = the model's default (1.0). Gemini 3.5 ignores it. Set from the AI Settings page"),
    "max_questions_per_chat": ({"guest": None, "customer": None, "seller": None},
                               "Most questions one chat may have, per role; null = unlimited. When reached, the "
                               "customer is asked to start a new chat. Set from the admin panel's AI Settings page"),
    "rag_mode": ("off", "RAG routing: off (send all tools and prompt sections), shadow (route and log only), "
                        "on (send only the routed tool packs and sections)"),
    # Published Gemini API rates in $ per 1M tokens (checked 2026-10-11 via pricing aggregators;
    # confirm against the Google Cloud billing page). Cached input is billed at the cached rate.
    "model_prices": ({
        "google:gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cached_input": 0.03},
        "openai:gpt-4.1-mini": {"input": 0.40, "output": 1.60, "cached_input": 0.10},
        "anthropic:claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00, "cached_input": 0.10},
        "google:gemini-embedding-001": {"input": 0.15, "output": 0.0},
    }, "Model prices in $ per 1M tokens, used to record the cost of every AI message"),
}


# Columns added to the existing support_tickets table for AI escalation (docs/AI_PLAN.md 7.1).
SUPPORT_TICKET_COLUMNS = {
    "priority": "VARCHAR(8) NOT NULL DEFAULT 'normal'",
    "source": "VARCHAR(12) NOT NULL DEFAULT 'customer'",
    "conversation_id": "CHAR(32) NULL",
    "ai_summary": "TEXT NULL",
    "updated_at": "DATETIME NULL",
}


def migrate_support_tickets() -> list[str]:
    existing = set(db.query_df(
        "SELECT column_name AS c FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'support_tickets'"
    ).c)
    added = []
    for column, definition in SUPPORT_TICKET_COLUMNS.items():
        if column not in existing:
            db.execute(f"ALTER TABLE support_tickets ADD COLUMN {column} {definition}")
            added.append(column)
    if "conversation_id" in added:
        db.execute("ALTER TABLE support_tickets ADD INDEX idx_tickets_conversation (conversation_id)")
    return added


# Columns added after the first release (long-chat memory, docs/CONTEXT_PLAN.md).
AI_COLUMNS: dict[str, dict[str, str]] = {
    "ai_conversations": {"memory": "JSON NULL"},        # key figures from earlier turns (ai/memory.py)
    "ai_messages": {"embedding": "BLOB NULL"},          # question embedding for finding related turns
}


def migrate_ai_columns() -> list[str]:
    added = []
    for table, columns in AI_COLUMNS.items():
        existing = set(db.query_df(
            "SELECT column_name AS c FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s", (table,)
        ).c)
        for column, definition in columns.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                added.append(f"{table}.{column}")
    return added


def create_all() -> None:
    for stmt in AI_DDL + SCHEDULER_DDL:
        db.execute(stmt)
    migrate_support_tickets()
    migrate_ai_columns()


def insert_defaults() -> dict[str, int]:
    limits = 0
    for scope, (msgs, inp, out, reply, rpm, tools) in DEFAULT_LIMITS.items():
        limits += db.execute(
            """
            INSERT IGNORE INTO ai_limits (scope, messages_per_day, input_tokens_per_day, output_tokens_per_day,
                                          max_reply_tokens, requests_per_minute, max_tool_calls_per_turn,
                                          note, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'default from AI_PLAN 8.2', 'setup', NOW())
            """,
            (scope, msgs, inp, out, reply, rpm, tools),
        )
    settings = 0
    for key, (value, description) in DEFAULT_SETTINGS.items():
        settings += db.execute(
            "INSERT IGNORE INTO ai_settings (setting_key, value, description, updated_by, updated_at) "
            "VALUES (%s, %s, %s, 'setup', NOW())",
            (key, json.dumps(value), description),
        )
    return {"limits": limits, "settings": settings}


if __name__ == "__main__":
    create_all()
    added = insert_defaults()
    print(f"AI and scheduler tables ready; defaults added: {added['limits']} limits, {added['settings']} settings")
