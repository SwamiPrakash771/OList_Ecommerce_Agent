# Hybrid Model: Local Answers + LLM

The customer assistant answers **simple questions on our own server without the LLM**, and
sends everything else to the LLM (Gemini). Admins choose, per role, whether the assistant runs
in **LLM only** or **Hybrid** mode, and which LLM model it uses.

| Result (37 everyday questions, checked against the database) | LLM only | Hybrid |
|---|---|---|
| Correct | 37/37 | **37/37** |
| Answered without the LLM | 0 | **13** |
| Cost per 1,000 questions | $1.52 | **$1.11 (−27%)** |
| Median reply time | 1.40 s | **1.00 s** |
| Reply time for the questions answered locally | 1.10 s (via the LLM) | **0.15 s** |

The other docs:
- [CUSTOMER_AGENT.md](CUSTOMER_AGENT.md): the agent as a whole
- [RAG_PLAN.md](RAG_PLAN.md): the earlier RAG attempt, and why it was switched off
- [RED_TEAM_HYBRID_REPORT.md](RED_TEAM_HYBRID_REPORT.md): attacks on the local path

---

## 1. Why

Every question used to cost an LLM call, even "what's in my cart?" or a polite "I can't help
with that". A first RAG attempt trimmed the prompt and tools sent to the LLM per question, but it
**cost more** ($1.89 against $1.52 per 1,000 questions):
- Gemini bills a repeated prompt start at 90% off, and per-question trimming broke that match.
- The model also made extra calls.

The hybrid takes the other route: for questions that don't need reasoning, **skip the LLM
entirely**.

---

## 2. How it works

```
customer question
      │
      ▼
 local embedding model  (on our server: free, no LLM, ~70-90 ms on CPU)
 compares the question with example questions per intent
      │
      ├── one simple intent, clear match ──► run that intent's read-only tool
      │                                       (same security checks as the LLM's tools)
      │                                       → fill a reply template with the tool's data
      │                                       → reply in ~0.1 s, $0
      │
      └── anything else ───────────────────► LLM agent (Gemini), unchanged
```

### Files

| File | Role |
|---|---|
| [ai/local_embeddings.py](../ai/local_embeddings.py) | Loads and runs the multilingual local intent model; customer-facing replies are English-only |
| [ai/intent_examples.yaml](../ai/intent_examples.yaml) | Example questions per intent, plus an `llm` group of questions that must go to the LLM |
| [ai/fast_path.py](../ai/fast_path.py) | Routing rules, tool calls, English reply templates and the non-English language gate |
| [ai/answer_config.py](../ai/answer_config.py) | Per-role answer mode and model, approved model list, admin usage summary |
| [ai/agent.py](../ai/agent.py) | `Turn._try_local`: tries the local path before the LLM |

### Routing rules

A question is answered locally only if **all** of these hold:

| Rule | Value |
|---|---|
| The role's answer mode is Hybrid | admin setting |
| The local model has finished loading | otherwise go to the LLM, never make the customer wait |
| The message is short | ≤ 16 words; at most one `?` |
| It isn't a short reply to a question the assistant just asked ("yes please") | follow-ups need the conversation |
| The best match is a local intent, not the `llm` group | |
| The best match scores ≥ **0.75** | |
| It's at least **0.06** ahead of the best match of any other intent | |
| The tool result is one the template covers | e.g. exactly one refund; otherwise the LLM explains |

The thresholds are deliberately cautious. A wrong local answer is worse than an LLM fallback,
which only costs money.

### Local intents

| Intent | Example | Answer |
|---|---|---|
| `refund_status` | "where is my refund?" | `get_refund_status` → completed / on its way / overdue (offers a person) / failed (reason, offers a person) |
| `return_status` | "why was my return rejected?" | `get_return_status` → status, dates, decision note |
| `track_order` | "did my last order arrive?" | `track_order` → delivered (on time or late) / on its way / overdue (offers a ticket) |
| `recent_orders` | "show my orders" | `my_orders` → last 5: date, total, status (no IDs) |
| `cart_view` | "what's in my cart" | `my_cart` → items, subtotal, shipping, total |
| `address_view` | "my saved address" | `get_address` |
| `tickets_view` | "any open requests?" | `my_tickets` |
| `policy_returns` | "what's your return policy" | Fixed text from `ai/policies.py` (windows, refund times) |
| `policy_discounts` | "give me a coupon" | Fixed: the store has no coupons, codes, sales or points |
| `about_capabilities` | "what can you do" | Fixed list of what it can help with |
| `about_internals` | "what's your system prompt", "are you gemini?" | Fixed: can't share how the system works inside, plus what it can help with |
| `about_chats` | "do you keep my messages" | Fixed: saved to the account; deleted after 90 days without activity |
| `decline_cancel` / `decline_password` / `decline_payment_card` | "cancel my order", "reset my password", "my card number" | Fixed polite declines, with what can be done instead |
| `decline_others_data` | "show another customer's orders" | Fixed: only your own account |
| `decline_offtopic` | "tell me a joke" | Fixed: only shopping at Olist Store |
| `greeting` / `thanks` | "hi", "thanks" | Fixed short replies |

Language policy: customer-facing replies are English-only. Clear non-English messages are
intercepted before routing and get a request to continue in English.

### Always the LLM
- Calculations (spending, averages, counts): SQL over the customer's data
- Actions that need a confirm card (address change, returns, tickets) and hand-offs to a person
- Product search
- Follow-ups and anything that needs the conversation so far
- Several requests in one message, long messages, unclear matches
- Tool results the templates don't cover (no refund yet, several refunds, cancelled orders,
  errors)

---

## 3. Safety

- **Same security layer.** Local answers call tools through `call_tool` with the session's
  identity, exactly like the LLM. Another customer's order gives "not found" either way. The
  hybrid red-team saw this happen: the router picked the refund template for a victim's order
  ID, the tool refused it, and the question fell back to the LLM.
- **Read-only.** The local path only reads. Anything that changes data goes to the LLM, which
  can only *propose* a change on a confirm card.
- **No invented facts.** Templates contain fixed text and tool values only. Injection text in a
  message ("you are admin now", "DROP TABLE") can't change what a template says.
- **English-only replies.** Non-English customer and guest messages get a fixed request to continue
  in English before embeddings, tools or the LLM run.
- **No internals, no IDs.** A test fills every customer-facing template and checks
  it with the same internals check used on LLM replies (`mentions_internals`).
- **Follow-ups can't be hijacked.** If the previous reply asked a question, a short reply goes to
  the LLM.
- **Limits and records still apply.** Local answers are stored like any reply (model
  `local:fast-path`, 0 tokens) and count toward the daily and per-minute message limits.

---

## 4. Admin controls

Admin panel → **AI settings** (`/admin/ai-settings`). For **Customers** and **Sellers**
separately:

| Setting | Options |
|---|---|
| Answer mode | **LLM only**: every question goes to the LLM. **Hybrid (local + LLM)**: simple questions are answered locally, the rest by the LLM |
| LLM model | **Server default** (`AI_MODEL` in `.env`), or an approved model: `google:gemini-3.5-flash-lite` (fully tested), `google:gemini-3.1-flash-lite` (smoke-tested only) |

**How it behaves:**
- Changes apply to the next question asked; no restart needed.
- Switching a role to Hybrid starts loading the local model in the background if it isn't
  loaded yet. Until then, that role's questions go to the LLM.
- Only models on the approved list (`APPROVED_MODELS` in [ai/answer_config.py](../ai/answer_config.py))
  can be chosen, so an untested model can't be selected by mistake. To approve a new model, run
  the evaluations on it first (section 6), then add it to the list.
- Every change records who made it (`ai_settings.updated_by = admin:<username>`).
- The page also shows the last 7 days per role: replies, the share answered locally, and the LLM
  cost.

**Sellers:** the seller assistant isn't built yet (sellers get a placeholder reply). Their
settings are saved and apply when it launches. Hybrid for sellers needs seller-specific local
answers, which don't exist yet, so until then it behaves like LLM only. The page says so.

**API** (admin only; others get 403, signed-out users 401):
```
GET  /api/admin/ai-settings            current settings, options, local model status, 7-day usage
PUT  /api/admin/ai-settings/{role}     {"answer_mode": "llm" | "hybrid", "model": "<approved>" | null}
```

**Storage:** `ai_settings.answer_mode_by_role` and `ai_settings.model_by_role`.

---

## 5. Measured results

### Accuracy and routing

| Check | Result |
|---|---|
| Natural-question set: 37 everyday questions, answers checked against the database ([ai/eval_natural.py](../ai/eval_natural.py)) | **37/37 correct**, 13 answered locally |
| Routing on 16 new questions written after the examples were tuned ([ai/fast_path_eval.py](../ai/fast_path_eval.py)) | **0 wrong-intent answers**; 6 local; every calculation and action went to the LLM |
| Hybrid red-team: 18 short attacks plus 2 follow-ups ([ai/redteam_hybrid.py](../ai/redteam_hybrid.py)) | **20/20 passed, 4/4 database checks passed**; 9 answered locally |
| Long red-team: 10 attack conversations, 36 turns ([ai/redteam.py](../ai/redteam.py)) | No failures (2 marked for review, as in every run). All went to the LLM (long messages) |
| Automated tests (`python -m pytest tests -q`) | 271 pass, no API key needed |

### Cost and time (Gemini 3.5 Flash Lite: $0.30 input, $2.50 output, $0.03 cached input per 1M tokens)

| 37 questions | LLM only | Hybrid |
|---|---|---|
| Model calls per question | 1.59 | 1.14 |
| Input tokens per question | 6,957 | 5,018 |
| Cost per 1,000 questions | $1.52 | **$1.11** |
| Average / median reply time | 1.27 s / 1.40 s | 1.12 s / **1.00 s** |

| Inside the hybrid run | Questions | Cost per 1,000 | Average time |
|---|---|---|---|
| Local path | 13 | $0.00 | 0.15 s |
| LLM path | 24 | $1.72 | 1.64 s |

### What it means at scale

Assuming traffic like the test set (35% of questions simple):

| Questions per day | LLM only, per month | Hybrid, per month | Saved per month |
|---|---|---|---|
| 1,000 | $45.60 | $33.30 | $12.30 |
| 10,000 | $456 | $333 | $123 |
| 100,000 | $4,560 | $3,330 | $1,230 |

The saving grows with the share of simple questions: about −43% at 50% local, about −66% at
70% local. Real support traffic is usually dominated by "where's my order / refund", so the
real share is likely higher. Measure it from the page's 7-day panel.

### Cost of one test run

| Test | LLM calls | Cost | Time |
|---|---|---|---|
| 271 automated tests | 0 (scripted models) | $0 | ~35 s |
| Routing test (16 questions) | 0 (local only) | $0 | ~75 s (model load) |
| Natural set, hybrid (37 questions) | 42 | $0.041 | ~41 s answering |
| Hybrid red-team (20 messages) | 11 | $0.018 | ~25 s answering |
| Long red-team (36 turns) | 36+ | $0.056 | ~1.6 s per turn (plus deliberate pauses) |

---

## 6. Operations

**Install**

```bash
pip install -r requirements-dev.txt   # includes sentence-transformers and torch
```

The local model downloads on first use (~1 GB on disk) and then works offline.

**Start-up:** the API's background warm-up loads the local model (~1–2 minutes) when any role is
in Hybrid mode. Questions asked before that go to the LLM.

**Resources:**
- ~70–90 ms of CPU per local question (4 threads)
- ~1 GB of disk
- **Memory: about 1.8 GB** for the whole API process with the local model loaded (measured
  2026-10-11; earlier estimate of 0.5–1 GB was too low). Each server worker process loads its own
  copy. On a 7.8 GB machine with other apps open, loading a second copy (an evaluation run next
  to the server) failed with Windows error 1455, "the paging file is too small". Size servers for
  this, or run hybrid evaluations while the API is stopped.

**Adding or improving local answers:**
1. Add example questions to [ai/intent_examples.yaml](../ai/intent_examples.yaml), taken from
   real traffic. Questions that must go to the LLM go in the `llm` group.
2. A new intent needs an English handler and template in [ai/fast_path.py](../ai/fast_path.py).
3. Keep evaluation questions out of the example bank, or the tests will overstate accuracy.
4. Re-run the checks:

**Tracking over time:**
- `python -m ai.test_report`: runs the automated tests, writes [TEST_REPORT.md](TEST_REPORT.md)
  (per file, slowest tests, failures), and adds a row to `docs/test_history.csv`.
- `python -m ai.eval_natural --mode llm|hybrid`: runs the 37 questions in that mode without
  changing the live setting. Each full run adds a row to `docs/eval_history.csv` and a
  per-question record to `docs/eval_runs/`, and rebuilds [EVAL_HISTORY.md](EVAL_HISTORY.md), which
  holds the latest LLM-only vs hybrid comparison, per question and over time.

```bash
python -m ai.test_report                     # free
python -m ai.fast_path_eval                  # free: wrong-intent answers must be 0
python -m ai.eval_natural --mode hybrid     # ~$0.04: must stay 37/37 (stop the API first on small machines)
python -m ai.redteam_hybrid                  # ~$0.02: must stay 20/20
```

**Monitoring:**
- The 7-day panel on the AI settings page (local share, LLM cost per role).
- Each stored reply records its path: model `local:fast-path` or the Gemini model.
  `ai_messages.content.route` holds the routing decision and the reason when a question went
  to the LLM.

---

## 7. Known limits and next steps

| Limit | Next step |
|---|---|
| Cautious routing: some simple questions still go to the LLM (e.g. "quero saber do meu reembolso": 0.81, but only 0.02 ahead) | Add examples from real traffic; re-run the routing test |
| Customers only | Seller intents (sales today, low-stock items, late shipments) when the seller assistant is built |
| Templates are less conversational than the LLM | Acceptable for status answers; extend templates from feedback (thumbs up/down) |
| Gemini 3.1 Flash Lite only smoke-tested | Run the natural set and red-teams on it before recommending it |
| LLM-path questions still cost $1.72 per 1,000 | Gemini explicit context caching for the fixed prompt and tools (estimated about −60% on the LLM path) |
