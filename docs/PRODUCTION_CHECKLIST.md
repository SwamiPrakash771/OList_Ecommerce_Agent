# Production Checklist (to fix later)

Status: **open**. Recorded 2026-10-11, after the customer support agent's red-team, cost and
latency work (see [CUSTOMER_AGENT.md](CUSTOMER_AGENT.md)). Nothing here has been fixed yet.

Tick items off as they're done and note the commit or PR.

---

## 1. Gaps found in our code (fix before going live)

Priority: **P1** = blocks launch, **P2** = fix soon after.

| # | Pri | Gap | Where | Risk | Fix |
|---|---|---|---|---|---|
| 1 | P1 | **Guests can bypass their limits.** Guest limits are tied to the `olist_guest` cookie; a script that drops the cookie gets a new "guest" with a fresh quota every message | `api/main.py` (`chat`, `guest_session`), `ai/store.py` (`subject_for`) | Unlimited free AI usage: cost and abuse | Also limit guests by IP address, plus a global cap on total guest usage |
| 2 | P1 | **Gemini free tier with customer data.** On Google's free tier, prompts may be used to improve their products. Our prompts contain customers' orders and addresses | `.env` (`AI_MODEL`, `GOOGLE_API_KEY`) | Breach of LGPD (Brazil's data protection law) | **Partly done (2026-10-11):** billing enabled, so the paid-tier terms apply. Still to do: confirm the data processing terms and data location for LGPD, and set a spending cap in Google Cloud |
| 3 | P1 | **`JWT_SECRET` is not set**, so a random one is generated at start-up | `api/main.py` (`JWT_SECRET`), `.env` | Every restart logs everyone out. With several server processes, a login only works on the process that created it | **Done for local (2026-10-11):** a fixed secret is in `.env`. Production still needs its own secret in the server's secret store |
| 4 | P1 | **No time limit on model calls**, no retry with back-off, no fallback model | `ai/agent.py` (`Turn.run`, `build_agent`) | A slow or failing provider call hangs the chat or shows a generic error (rate-limit errors were seen in testing) | **Done (2026-10-12):** 20 s per call, 45 s per message, a specific message per error type, fallback model, circuit breaker (docs/ERROR_HANDLING.md) |
| 5 | P1 | **The monthly budget isn't enforced.** `monthly_budget_usd` is only reported by a background job | `ai/agent.py` (`check_limits`), `ai/scheduler.py` | No hard cost ceiling during a traffic spike or abuse | Check platform spend before each message; switch off or degrade at 100%, with an alert at 80% |
| 6 | P1 | **Cookies aren't marked "secure"** (`olist_session`, `olist_guest`) | `api/main.py` (`set_cookie`) | They could be sent over plain HTTP | `secure=True`, served only over HTTPS |
| 7 | P2 | **The chat shows external links** from the model's text | `web/src/components/Chat.tsx` (`inline`) | Injected text could get a phishing link shown in a trusted chat bubble | Allow only links to our own pages, or always show the full address |
| 8 | P1 | **The app connects to MySQL with the user from `.env`** (root in development) | `.env` (`MYSQL_USER`), `db.py` | A bug in the app has full database power | A restricted app user with only the grants it needs (like `olist_agent`) |
| 9 | P1 | **`DEMO_LOGIN=true`** | `.env` | One-click login to demo accounts | Off in production |

---

## 2. Reliability

- [ ] Lock the model version (aliases like "flash-lite" can change); re-run the evaluations before
      any model change.
- [ ] Allow only one chat message at a time per user. The per-minute rate check can be beaten by
      sending many messages at once.
- [ ] Health and readiness checks: report "ready" only after the AI warm-up has finished.
- [ ] Clean shutdown that lets replies already streaming finish.
- [ ] If a proxy sits in front of the server: turn off buffering for `/api/chat` and allow long
      time limits for streamed replies. We already send the header asking proxies not to buffer.

## 3. Quality monitoring

- [ ] **Tracing:** record every model call, tool call and token count per conversation (Langfuse
      or Logfire, per the plan).
- [ ] **Dashboards and alerts:**
  - response time (median and the slowest 5%)
  - error rate
  - how often the fact check rejects a reply, or falls back to the safe reply
  - tool errors
  - hand-off rate and thumbs-down rate
  - cost per conversation
- [ ] **A bigger evaluation set:** 100+ graded conversations (today `ai/cost_bench.py` has 10). Run
      it with the red-team set (`ai/redteam.py`) on every prompt or model change.
- [ ] **New prompt versions:** shadow mode first, then a gradual rollout.

## 4. Security and privacy

- [ ] Protection against forged requests (CSRF) on the endpoints that change data. Cookies already
      use `SameSite=lax`, which helps.
- [ ] Security headers: CSP, HSTS, X-Frame-Options, Referrer-Policy.
- [ ] **Personal data minimisation:** limit what goes into the model, and check what is stored in
      `ai_messages`. The full tool results are saved, including addresses.
- [ ] Encrypt stored data and backups.
- [ ] Tell customers they're talking to an AI.
- [ ] Let users export and delete their data (delete endpoint and retention clean-up job already
      exist).
- [ ] Monitor continuously for prompt-injection attempts, and review flagged conversations.
- [ ] Fact check: amounts the customer types count as evidence. Decide whether to accept them
      only when quoted or attributed.

## 5. Operations

- [ ] **Remove the demo-account limit overrides:** the 5 demo customers have raised AI limits for local
      testing (`ai_limits` rows with note "Demo account: raised for local testing"). Delete them:
      `DELETE FROM ai_limits WHERE note LIKE 'Demo account: raised%';`
- [ ] **Hybrid memory:** the local model makes each API process use about 1.8 GB. Each worker loads
      its own copy. Size servers accordingly, or move the local model to one shared service.

- [ ] Versioned database migrations instead of ad-hoc scripts (`seed_users.py`, `ai/schema.py`).
- [ ] Run the tests on every change before deploying.
- [ ] Load tests with many users chatting at once. Size the connection pools, the agent user's
      20-connection limit, and the provider's requests per minute (free tier: 15).
- [ ] A screen for support agents to work hand-off tickets, showing the "SYSTEM-VERIFIED FACTS"
      and "ASSISTANT SUMMARY - NOT VERIFIED" sections in clearly different styles.
- [ ] Build the `refund_sla_monitor` job (find stuck refunds before the customer asks).
- [ ] Build the admin AI Controls page (limits, switches, model per role, budget).
- [ ] Backups, and a tested restore.

## 6. Business

- [ ] Incident playbooks: model down, budget exceeded, data leak (kill switches already exist).
- [ ] Agree success targets with the business (resolved without a human, hand-off rate,
      satisfaction) and track them.
