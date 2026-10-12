# Error Handling: LLM API and Database Connections

What happens when the LLM provider or the database fails, and what the customer sees. Built
2026-10-12 after real incidents in this project:
- Gemini's per-minute limit and daily quota showed the same generic error on every message.
- A MySQL crash (out of memory) produced raw 500 errors and empty pages.
- Starting the server before MySQL left the local model unloaded.

**Principles**
- Every wait has a limit: timeouts, a cap on connections, a retry budget.
- Retry only what's safe: reads, yes; writes, never automatically.
- When the LLM can't answer, degrade instead of failing (hybrid local answers still work).
- Users get a clear message; we get the details (error type, request ID) in the logs.

---

## 1. Database ([db.py](../db.py))

| Situation | Handling | What the user sees |
|---|---|---|
| MySQL hangs | Connect timeout 5 s; read and write timeouts 60 s (`MYSQL_CONNECT_TIMEOUT`, `MYSQL_READ_TIMEOUT`, `MYSQL_WRITE_TIMEOUT`). Setup scripts (`db.connect()`) have no read timeout, because bulk loads take minutes | The request ends instead of hanging |
| MySQL down or unreachable (error codes 2003, 2005, 1040…) | Raised as `DatabaseUnavailable`; the API returns **503** with `code: db_unavailable` and `Retry-After: 30` | *"The store is temporarily unavailable. Please try again in a minute."* plus a banner with **Retry** |
| Too many requests at once | Each pool caps open connections (app 15, agent 18, which is below the agent's MySQL limit of 20). A request waits up to 5 s (`MYSQL_POOL_WAIT`), then gets `DatabaseBusy` → 503 `db_busy` | *"The store is very busy right now. Please try again in a few seconds."* |
| Connection lost mid-way (2006, 2013, e.g. MySQL just restarted) | **Reads** (`query_df`, `query_one`) reconnect and retry once; **writes** are never retried | Usually nothing: the retry succeeds |
| Server starts before MySQL | Start-up steps (page cache, AI warm-up) retry with growing pauses (2 s, 4 s … 60 s, up to 15 min) instead of giving up | Pages work as soon as MySQL is up; the local model loads too |

Idle connections are checked with a ping before reuse; broken ones are replaced, and their slot
in the pool is freed.

## 2. LLM provider ([ai/resilience.py](../ai/resilience.py), [ai/agent.py](../ai/agent.py))

**Each error is sorted into a type, and each type is handled differently:**

| Type | How it's recognised | Handling | Message to the customer |
|---|---|---|---|
| `rate_limited` | 429 with a short wait (per-minute limit) | **One retry** after the provider's suggested wait (at most 8 s), only if nothing was shown yet and no tool ran (so "add to cart" can't run twice) | *"I'm getting a lot of questions right now. Please try again in a minute."* |
| `quota` | 429 for a daily limit, or a wait over 5 minutes | **Pause the provider at once** (until its reset, re-checked at least every 30 min) | *"The assistant has reached its usage limit for now and will be back later."* |
| `provider_error` | 5xx, network errors | Fallback model; the provider is paused after 3 failures in 2 minutes | *"The assistant is having trouble right now. Please try again in a few minutes."* |
| `timeout` | 20 s per model call, 45 s per message, or 408/504 | Counts toward the pause | *"That took too long to answer. Please try again, or ask a simpler question."* |
| `auth` | 401 or 403 (key problem) | Pause and log; the admin sees it on the health card | *"The assistant isn't available right now. Our team has been notified."* |
| `bad_request` | Other 4xx (our side) | Logged; doesn't pause the provider | *"Sorry, I couldn't process that request. Please try rephrasing it."* |

**When the customer's role is in Hybrid mode,** provider messages add *"Simple questions, like
where your order or refund is, or what's in your cart, still work."* Those questions are
answered locally before the LLM is involved, so the assistant keeps working for them during an
outage.

**Fallback model:** `ai_settings.fallback_model`, by default **Gemini 3.1 Flash Lite**: same
Google key, separate quota. When the main model fails (rate limit, quota, outage), the same
request goes to the fallback (Pydantic AI's `FallbackModel`). Only approved models are used. The
stored reply records which model actually answered. *Price table:* 3.1 Flash Lite has no price
entry yet, so its cost records as $0 until one is added.

**Circuit breaker (pause):**
- After repeated failures, the provider is skipped and customers get the right message
  immediately, instead of each one waiting for the same failure.
- After the pause, the next request checks the provider again, and a success ends the pause.
- The admin can end it at once with **"Try the LLM again now"** on the AI settings page, e.g.
  after enabling billing.

**Fair limits:** a message that fails, or is answered while the provider is paused, records its
tokens and cost but **does not count toward the customer's daily message limit**. It still counts
for the per-minute limit, which protects against retry storms.

## 3. API and frontend

- **Request IDs:** every response carries `X-Request-ID`; every error body has
  `{detail, code, request_id}`, and the logs carry the same ID, so a user's report can be traced.
- **No internals:** unexpected errors return 500 *"Something went wrong on our side. Please try
  again."* The stack trace goes only to the server log.
- **Banner:** a 503, or a network failure, shows *"temporarily unavailable"* with **Retry** in the
  store, the admin panel and the seller center. It disappears when a request succeeds.
- **Sign-in page:** if the test-login buttons can't load, it says why instead of hiding them.

## 4. Health

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness: the server process is up |
| `GET /api/ready` | Readiness: both database pools, LLM configured, LLM provider status, local model. **503 when the database is unreachable** |
| `GET /api/admin/system-health` | Admin card: database pools and limits, LLM status (ok / degraded / paused, time left, last error), fallback model, local model |
| `POST /api/admin/system-health/reset-llm` | Admin: end an LLM pause now |

## 5. Tests ([tests/test_errors.py](../tests/test_errors.py), 15 tests, simulated failures, no LLM calls; full suite 288/288)

- Classifying errors: quota vs per-minute limit, provider errors, key problems, bad requests,
  timeouts; with a fallback, the worst error decides.
- The circuit breaker opens after repeated failures, pauses at once on quota, and closes on
  success.
- An outage gives a specific message and doesn't count against the customer.
- A short rate limit is retried once and succeeds. **No retry after a tool has run.**
- While paused, the customer gets an answer without the model being called.
- The 45 s limit per message ends a hung turn.
- The fallback model answers when the main one fails.
- The pool reports busy at its ceiling; an unreachable database raises `DatabaseUnavailable`;
  reads retry once.
- The API turns a database outage into a 503 with a request ID; unexpected errors never show
  internals; `/api/health` and `/api/ready` work.

## 6. Still open
- **An error log table** (`ai_errors`) for trends over time. Today errors are in the server log and
  the health card shows the latest.
- **MySQL auto-restart** in Windows Services (*Recovery → Restart the service*): an operations
  setting, not code.
- **A price entry for the fallback model.**
- **Alerts** (email or chat) when the database is down or the quota runs out.
