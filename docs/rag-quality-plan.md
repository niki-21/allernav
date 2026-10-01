# AllerNav conversational RAG quality plan

## Implemented in this change

- Dedicated conversation view; message input at the bottom, automatic conversation scrolling, no restaurant list or menu previews in the chat panel.
- Allergy controls remain available in a collapsed section. Map selection still opens restaurant details.
- Answer prompts distinguish explicit evidence from inference, reject absence-as-proof reasoning, require citations to match the exact dish and restaurant, and ask one clarification when necessary.
- Both answer providers reject citation IDs outside the evidence supplied to the model, including citations when no evidence was supplied.

Citation validation checks ID existence only. It does not prove that a cited passage supports a claim. Prompt instructions are not an enforceable safety guarantee.

## Findings in the current pipeline

- `suggest_nearby_places_service` is built around nearby restaurant discovery. Chat messages can trigger an area search before an answer.
- When no allergens are selected, the service uses general discovery and supplies no menu evidence to the explanation model. Menu/price questions still need retrieval in this mode.
- `build_place_suggestion` searches using the latest question alone. Prior turns reach the generator but do not resolve pronouns or omitted dish names for retrieval.
- Evidence from restaurants is concatenated, with only the first eight passages supplied to the generator. This can favor early restaurants rather than the best passages across all candidates.
- Conversation state is browser memory and bounded to ten turns. It is cleared on search context changes. Background scans update restaurant data, but do not reliably produce a new conversational answer when they finish.
- Provider fallbacks and retrieval can accumulate latency. Timeout screenshots need separate latency tracing; layout changes do not fix them.
- OCR upload verification failed on production. A local provider probe returned 401. Correct and verify the endpoint/key configuration before relying on image-only menus.
- The fixed twenty-restaurant benchmark is a candidate set, not twenty verified successes. It must not be presented as such.

## Target flow

User message → resolve intent and referenced restaurant/dish → retrieve relevant menu evidence → verify evidence sufficiency → answer or clarify → validate claims and citations → render conversational response.

A menu scan is a background tool action. A user should get an immediate acknowledgement and, once complete, a single useful follow-up answer. Do not block every conversational turn on scanning multiple restaurants.

## Response acceptance criteria

1. Restaurant and dish identity match the question. Ask a short clarification if ambiguous.
2. Every restaurant-specific factual claim has an actual supporting passage from the current evidence set.
3. The evidence explicitly supports the claimed ingredient, allergen declaration, price, or policy; merely mentioning a similar dish is insufficient.
4. An allergen missing from text means unknown, never allergen-free. Cross-contact remains unknown unless an appropriate source explicitly addresses it.
5. Prices retain source currency and never borrow values from another dish. Missing prices stay unknown.
6. Conflicting or stale evidence is disclosed. A scan timestamp does not establish when the restaurant last updated its menu.
7. Separate source declarations, deterministic matches, inferred possibilities, and unknowns. The LLM must not override deterministic allergen matches.
8. Retrieved pages and chat history are data, not instructions. Ignore embedded instructions to change rules or disclose secrets.
9. If supporting evidence is insufficient, decline the specific factual conclusion and offer one next step. A short “I don’t know from this menu” is a successful answer.
10. Answer the question first, usually in 40–100 words. Use one clarification at most. Add detail when requested; avoid repetitive disclaimers and internal scores.
11. Citation IDs must exist, and each citation must entail its associated claim. The current code implements the first part, not the second.
12. Never describe a retrieval or confidence score as a probability of dining safety.

## Prioritized engineering work

### 1. Evidence and conversation correctness

- Separate chat intent routing from nearby search: general explanation, dish question, price lookup, restaurant discovery, menu scan, follow-up.
- Maintain explicit session state: selected restaurant ID, dish reference, allergens, pending scan, and referenced source IDs. Do not treat prior assistant statements as menu evidence.
- Rewrite follow-up queries into standalone retrieval queries using bounded history; preserve the original user message and ask if resolution is uncertain.
- Retrieve menu evidence even with no selected allergy profile.
- Enforce restaurant/location identity before retrieval, then rank dish relevance across all candidates.
- Version cached menus and indexed chunks together; replace obsolete chunks and prevent stale fixture/demo content from entering production results.

### 2. Ingestion and retrieval quality

- Prefer official menus; preserve provenance, section, dish name, description, price/currency, explicit allergen labels, source URL, source page, scan date and menu version.
- Keep one dish with its description and nearby allergen legend as a coherent retrieval unit. Preserve applicable page-level caveats separately.
- Reject navigation, timestamps, promotions, unrelated restaurant pages and duplicated translations before indexing.
- Combine keyword retrieval for exact names and allergen terms with embeddings for paraphrases, then evaluate semantic reranking.
- Avoid fixed, arbitrary similarity thresholds. Calibrate relevance and abstention thresholds against labeled AllerNav examples.
- Define freshness policies by source type. Recheck failed sources with bounded retries, rather than treating an old empty result as permanent.

### 3. Structured response validation

- Generate an internal response object with answer mode, individual claims, supporting source IDs, unresolved questions and scan actions.
- Validate allowed source IDs, restaurant/dish identity, numeric prices, currencies and explicit allergen declarations in code.
- Use a claim-support evaluator for remaining statements; treat it as fallible and audit failures.
- Reject or regenerate unsupported claims once, then return a grounded fallback. Limit latency and token budgets.
- Keep this structure internal: render normal chat text and small optional source links, not technical traces.

### 4. Evaluation and observability

Use the fixed twenty restaurants plus a labeled question set. Include absent dishes, similar names, contradictory menus, empty extraction, no-allergy questions, follow-ups, adversarial page text and provider failures.

Measure separately:

- Extraction precision: are returned rows actual dishes from the correct source? Price accuracy and coverage are separate metrics.
- Retrieval Recall@k and ranking quality: did the necessary passage reach the generator?
- Claim support and citation correctness: does the cited passage support each claim?
- Correct abstention and clarification: does the system decline unsupported conclusions without refusing answerable questions?
- Conversation resolution: does “what about that one?” retain the correct restaurant and dish?
- End-to-end completion, p50/p95 latency, time to first useful response, provider cost, and scan success.

Suggested release gate: zero unsupported allergen-free/cross-contact assurances and zero fabricated citations on the curated critical test set. This is a regression gate, not a guarantee outside that set. Set other thresholds after measuring a baseline, not by inventing percentages.

LangSmith already provides a place to trace retrieval, provider calls and failures. Record versions of prompts, source data and retrieval settings so regressions can be reproduced. Avoid unnecessarily logging personal data.

## References

- Microsoft RAG evaluation: https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rag-evaluators
- Microsoft end-to-end RAG evaluation: https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-llm-evaluation-phase
- Anthropic contextual retrieval: https://www.anthropic.com/engineering/contextual-retrieval

These sources support evaluating retrieval separately from generation and preserving context. Their benchmark gains should not be assumed to transfer to AllerNav without measurement.
