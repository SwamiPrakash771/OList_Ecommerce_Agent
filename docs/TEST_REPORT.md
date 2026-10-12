# Automated test report

Run: 2026-10-12 00:33 · commit `3603719+changes` · **288/288 passed** · 90.2 s · LLM calls: 0 (scripted models) · cost $0

Regenerate with `python -m ai.test_report`. History of every run: `docs/test_history.csv`.

| Test file | What it covers | Tests | Passed | Failed | Time |
|---|---|---|---|---|---|
| `tests/test_agent.py` | The support agent engine (ai/agent.py), run against scripted models (no API key needed) | 49 | 49 | - | 17.7 s |
| `tests/test_customer_tools.py` | Customer support tools: correctness on the demo scenarios, and permission guarantees | 19 | 19 | - | 1.2 s |
| `tests/test_errors.py` | Error handling: LLM provider failures and database connection problems (docs/ERROR_HANDLING.md) | 15 | 15 | - | 13.2 s |
| `tests/test_permissions.py` | Systematic permission tests for every registered agent tool (docs/AI_PLAN.md section 13) | 143 | 143 | - | 18.3 s |
| `tests/test_rag.py` | RAG groundwork (phase 1): the prompt split into sections and the tools into packs | 7 | 7 | - | 0.5 s |
| `tests/test_security.py` | Security foundation tests: identity, tool registry rules, database grants, catalog privacy | 32 | 32 | - | 0.5 s |
| `tests/test_storage_and_jobs.py` | Storage (ai/store.py) and scheduler (ai/scheduler.py) tests | 23 | 23 | - | 14.4 s |

## Slowest tests

| Time | Test |
|---|---|
| 8.69 s | `tests/test_errors.py::test_unreachable_database_is_reported_as_unavailable` |
| 4.80 s | `tests/test_storage_and_jobs.py::test_conversations_are_private_to_their_owner` |
| 2.19 s | `tests/test_errors.py::test_short_rate_limit_is_retried_once` |
| 2.11 s | `tests/test_agent.py::test_second_turn_sees_the_first_turns_tool_results` |
| 1.58 s | `tests/test_agent.py::test_api_placeholder_when_no_model` |
| 1.47 s | `tests/test_agent.py::test_full_turn_streams_stores_and_counts_usage` |
| 1.31 s | `tests/test_storage_and_jobs.py::test_retention_purge_respects_classes` |
| 1.29 s | `tests/test_storage_and_jobs.py::test_job_never_runs_twice_at_once` |
| 1.24 s | `tests/test_storage_and_jobs.py::test_agent_cannot_rewrite_or_delete_ai_records[UPDATE ai_messages SET content = '{}' WHERE message_id = -1]` |
| 0.98 s | `tests/test_agent.py::test_older_turns_are_resent_without_tool_results` |

## Recent runs

| Date | Commit | Tests | Passed | Failed | Time |
|---|---|---|---|---|---|
| 2026-10-12 00:33 | 3603719+changes | 288 | 288 | 0 | 90.2 s |
| 2026-10-12 00:31 | 3603719+changes | 288 | 271 | 17 | 77.5 s |
| 2026-10-11 22:54 | 04d1d04+changes | 271 | 271 | 0 | 47.5 s |
| 2026-10-11 22:53 | 04d1d04+changes | 271 | 271 | 0 | 60.2 s |
