# RAG Plan: Cutting Tokens for the Customer Support Agent

Status: **built and measured; switched off** (`rag_mode = off`). Trimming the prompt and tools per question cost more ($1.89 against $1.52 per 1,000 questions): it broke Gemini's prompt cache and the model made extra `load_more` calls. Replaced by the hybrid local/LLM answers described in [CUSTOMER_AGENT.md](CUSTOMER_AGENT.md) ($1.11 per 1,000 questions, 37/37 correct). Written 2026-10-11. Agent version at the time:
prompt `support-v7`, model `google:gemini-3.5-flash-lite`.

---

## 1. Token consumption today (measured)

**Every model call resends the same fixed context:**

| Part | ~Tokens per call |
|---|---|
| System prompt, total | 1,620 |
| - "How you work" rules | 690 |
| - Procedures (refund, late delivery, damaged item, hand-off) | 470 |
| - Actions | 140 |
| - Store policies | 125 |
| - Glossary | 110 |
| - Opening | 75 |
| Tool definitions (17 tools), total | **2,340** |
| - `query_my_data` (schema and SQL guide) | **940** |
| - `search_products` | 195 |
| - `update_address` | 170 |
| - Other 14 tools | ~1,035 |
| **Fixed cost of every call** | **~3,960** |

**Per customer message** (average of the last 54 stored turns):

| | Tokens |
|---|---|
| Input | ~7,000 (most questions need 2 calls: the tool call, then the answer) |
| Output | ~126 |
| Served from Gemini's automatic cache | ~24% of input |

98% of the tokens are input, and most of that is instructions and tool definitions a given
question doesn't need. A refund question doesn't need the cart tools, the address rules or the
940-token SQL guide.

**About the $0.69 in 15 minutes:** that came from automated test runs (the red-team, 4 runs of
the natural-question set, the benchmarks): several hundred model calls. It's not representative
of customer traffic. In production, cost is roughly *messages × (7,000 input + 126 output
tokens)*. Phase 0 adds a price table so every message's cost is recorded in dollars.

---

## 2. What to retrieve

Classic document RAG retrieves knowledge text. Ours is small (store policies are about 125
tokens), so retrieving documents would save almost nothing. What we resend on every call is
**instructions and tools**, so the retrieval should pick **which prompt sections and which
tools this message needs**. A second use: an answer cache for generic questions, which skips
the model entirely.

---

## 3. Design: retrieval before the ReAct loop

The agent already works in the ReAct pattern: reason, call a tool, observe the result, answer.
Retrieval runs **before** that loop, in code, with no extra model call:

```
message ─► [1] embed the message once (reused by steps 2 and 3)
           [2] answer cache: generic question seen before?  ── hit ──► stored answer (0 model calls)
                 │ miss
                 ▼
           [3] router: pick tool packs + prompt sections
                 ▼
           [4] ReAct loop with: core prompt + selected sections + selected packs
                 │ needs something that wasn't selected?
                 ▼
           [5] load_more(pack) tool → loop continues (one extra call, only on a miss)
```

### 3.1 Split the prompt

- **Core, always sent (about 500 tokens):**
  - identity and security rules
  - style rules (no internals, no arithmetic, ID rules)
  - a one-line list of what the assistant can do
- **Retrievable sections (100–300 tokens each):**
  - refund procedure
  - returns and windows
  - late delivery
  - hand-off rules
  - store policies
  - glossary
  - SQL guide

### 3.2 Group tools into fixed packs

| Pack | Tools |
|---|---|
| Orders and delivery | `my_orders`, `track_order` |
| Returns and refunds | `get_return_status`, `get_refund_status`, `request_return` |
| Cart | `my_cart`, `cart_add`, `cart_set_quantity` |
| Account | `get_address`, `update_address`, `my_tickets`, `open_ticket` |
| Analytics | `query_my_data` (and the SQL guide) |
| Catalog | `search_products`, `product_details`, `list_categories` |
| Always on | `transfer_to_human`, `load_more` |

**Why fixed packs instead of picking the top-k tools:** Gemini's cache only works on an
identical start of the request. With 6 fixed packs there are few distinct combinations, so they
still get cached. A different tool list for every message would lose the cache.

### 3.3 Router

- Turn the message into an embedding (vector) and compare it with a bank of labelled example
  questions per pack. Labels come from questions we already have: the 37 natural questions,
  the benchmark set, red-team turns, and the tools each one actually used.
- **Multi-topic** messages get the union of packs.
- **Follow-ups** ("add the first one") keep the previous turn's packs.
- **Low confidence** sends all packs, which is today's behaviour. A wrong route costs tokens,
  never a wrong answer.
- **Security doesn't change:** packs are chosen only from `tools_for(role)`, and `call_tool`
  still checks every call.

### 3.4 Answer cache (generic questions only)

- Examples: "what's your return policy?", "what can you do?", off-topic declines.
- Served when a new message is very close in meaning to a cached question, for the same role
  and the same prompt version.
- **Never cached** if any customer-data tool was used (orders, refunds, cart, address,
  analytics). Tested.
- A new prompt version clears the cache. Entries also expire after a set time.

---

## 4. Embeddings: which one, and what it costs

### Measured on our key (2026-10-11), on real customer messages

| Model | Tokens per message | Response time | Vector size | Languages |
|---|---|---|---|---|
| `gemini-embedding-001` | ~11 (6–19) | ~180 ms | 3,072 | Multilingual (English and Portuguese) |
| `gemini-embedding-2` | ~11 | ~190 ms | 3,072 | Multilingual; also images, audio, video |

Messages tested: "where is my refund?" (6 tokens), "How much did i spend every month from last
12 months" (14), a Portuguese complaint (19), "add the first one to my cart" (8), "what's your
return policy?" (8).

### Prices (secondary sources, to confirm on Google's pricing page)

| Model | Price | Free tier |
|---|---|---|
| `gemini-embedding-001` | about **$0.15 per 1M input tokens** ([Google launch post](https://developers.googleblog.com/gemini-embedding-available-gemini-api/)) | Yes, free in the free tier |
| `gemini-embedding-2` | about **$0.20 per 1M input tokens** ([pricing aggregator](https://www.getmaxim.ai/bifrost/llm-cost-calculator/provider/gemini/model/gemini-embedding-2.md)) | Not confirmed |

The project now has billing enabled, so assume paid rates apply.

**What that means for us:**
- One embedding per message, about 11 tokens: about **$0.0000017 per message**, or **about
  $1.65 per million messages** with `gemini-embedding-001`.
- Embedding the router's example bank (about 200 questions, about 2,500 tokens) once costs less
  than a tenth of a cent.
- The saving it unlocks is roughly **4,000–5,000 LLM input tokens per message**. That's about
  400 times the tokens the embedding uses.

### Local (free, runs on our server)

| Model | Notes |
|---|---|
| `bge-small-en-v1.5` | Small and popular, but **English only** ([source](https://thegtmdirectory.com/models/baai-bge-small-en/bge-small-en-v1.5)). Ruled out: our customers write in Portuguese too |
| `paraphrase-multilingual-MiniLM-L12-v2` or `multilingual-e5-small` | Multilingual and free; run on CPU via `fastembed` or `sentence-transformers`. Download size, memory use and speed to be measured in phase 2 (expected well under 50 ms, but not yet verified) |

### Recommendation

| | Gemini `gemini-embedding-001` | Local multilingual model |
|---|---|---|
| Cost | ~$1.65 per million messages | Free |
| Added time per message | ~180 ms | Expected <50 ms (to measure) |
| Setup | Nothing to install | Model download, memory on the server |
| Privacy | Message text goes to Google (it already does for the chat model, so no new exposure) | Stays on our server |
| If it's down | Router falls back to all packs | No external dependency |

- **Start with `gemini-embedding-001`.** It's effectively free at our scale, multilingual, GA,
  and needs no installation. `gemini-embedding-2` costs more and adds nothing for short text
  questions.
- **In phase 2, test one local multilingual model on the same labelled questions.** If it routes
  as accurately, switch to it to save the ~180 ms and the external dependency.
- The ~180 ms can also be hidden by embedding in parallel with the limit checks and history
  loading that already happen before the model call.

No vector database is needed. There are about 200 router examples plus cached answers, held in
memory (MySQL tables `ai_router_examples` and `ai_answer_cache` for persistence). A vector
database only becomes worth it later, for semantic product or review search.

---

## 5. Expected savings (to be verified, not assumed)

| Question type | Today, per call | With RAG, per call |
|---|---|---|
| Orders, refunds, cart (1–2 packs) | ~3,960 | ~1,200–1,500 (**about −65%**) |
| Analytics (with the SQL guide) | ~3,960 | ~2,000–2,300 (**about −45%**) |
| Generic or policy (cache hit) | ~3,960 × 1–2 calls | **0 model calls** |

**At scale, e.g. 10,000 messages a day:**

| | Input tokens per day |
|---|---|
| Today (~7,000 per message) | ~70M |
| With RAG (~2,500–3,500 per message) | ~25–35M |
| Embeddings (~11 per message) | ~0.11M |

---

## 6. Build phases (measured after each one)

| Phase | What | Pass criteria |
|---|---|---|
| 0 | Price table in `ai_settings.model_prices` so each message's cost is recorded in dollars; baseline from `cost_bench` and the natural-question set | Dollars per message reported per question type |
| 1 | Split the prompt into core + sections and the tools into packs, **still sending everything** | Identical behaviour: natural set 37/37, red-team passes |
| 2 | Router: example bank, embeddings, confidence threshold; **offline** test on every evaluation question; compare Gemini with one local model | Needed packs selected in ≥98% of questions; the rest fall back to all packs |
| 3 | Dynamic packs and sections in the ReAct loop, plus `load_more` | Natural set 37/37, red-team passes, input tokens about −50% |
| 4 | Answer cache for generic questions | Never serves personal data (tests); hit rate measured |
| 5 | Shadow mode (log router decisions without applying them), then switch on with `ai_settings.rag_enabled` | Same results on real traffic before switching on |

## 7. Risks

| Risk | Mitigation |
|---|---|
| Router misses a needed tool | Low confidence sends all packs; `load_more` escape hatch; routing test in CI |
| Fewer cache hits from changing tool sets | Fixed packs; measured in phase 3 |
| Stale cached answers | Generic questions only; cleared on a new prompt version; expiry time |
| Embedding service down | Router falls back to all packs (today's behaviour) |
| Follow-up questions lose context | The previous turn's packs stay selected |
