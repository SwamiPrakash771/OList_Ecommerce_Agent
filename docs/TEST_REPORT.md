# Automated test report

Run: 2026-10-11 22:54 · commit `04d1d04+changes` · **271/271 passed** · 47.5 s · LLM calls: 0 (scripted models) · cost $0

Regenerate with `python -m ai.test_report`. History of every run: `docs/test_history.csv`.

| Test file | What it covers | Tests | Passed | Failed | Time |
|---|---|---|---|---|---|
| `tests/test_agent.py` | The support agent engine (ai/agent.py), run against scripted models (no API key needed) | 47 | 47 | - | 9.2 s |
| `tests/test_customer_tools.py` | Customer support tools: correctness on the demo scenarios, and permission guarantees | 19 | 19 | - | 0.8 s |
| `tests/test_permissions.py` | Systematic permission tests for every registered agent tool (docs/AI_PLAN.md section 13) | 143 | 143 | - | 20.5 s |
| `tests/test_rag.py` | RAG groundwork (phase 1): the prompt split into sections and the tools into packs | 7 | 7 | - | 0.3 s |
| `tests/test_security.py` | Security foundation tests: identity, tool registry rules, database grants, catalog privacy | 32 | 32 | - | 0.2 s |
| `tests/test_storage_and_jobs.py` | Storage (ai/store.py) and scheduler (ai/scheduler.py) tests | 23 | 23 | - | 7.3 s |

## Slowest tests

| Time | Test |
|---|---|
| 1.37 s | `tests/test_agent.py::test_api_placeholder_when_no_model` |
| 1.16 s | `tests/test_storage_and_jobs.py::test_job_never_runs_twice_at_once` |
| 0.98 s | `tests/test_permissions.py::test_tool_touches_only_declared_tables[product_details]` |
| 0.91 s | `tests/test_permissions.py::test_tool_touches_only_declared_tables[list_categories]` |
| 0.90 s | `tests/test_permissions.py::test_tool_touches_only_declared_tables[my_cart]` |
| 0.88 s | `tests/test_permissions.py::test_tool_touches_only_declared_tables[my_orders]` |
| 0.85 s | `tests/test_permissions.py::test_role_matrix[customer-list_categories]` |
| 0.84 s | `tests/test_storage_and_jobs.py::test_agent_cannot_rewrite_or_delete_ai_records[UPDATE ai_messages SET content = '{}' WHERE message_id = -1]` |
| 0.80 s | `tests/test_permissions.py::test_tool_touches_only_declared_tables[open_ticket]` |
| 0.80 s | `tests/test_agent.py::test_full_turn_streams_stores_and_counts_usage` |

## Recent runs

| Date | Commit | Tests | Passed | Failed | Time |
|---|---|---|---|---|---|
| 2026-10-11 22:54 | 04d1d04+changes | 271 | 271 | 0 | 47.5 s |
| 2026-10-11 22:53 | 04d1d04+changes | 271 | 271 | 0 | 60.2 s |
