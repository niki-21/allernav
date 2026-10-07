# Question-triggered restaurant shortlist

The web chat inspects a bounded shortlist when a user asks, rather than crawling every map result.

1. Use the user's location or explicitly selected/search area. On an initial chat request without a chosen area, request browser geolocation; if unavailable, ask the user to choose an area instead of silently using the default map center.
2. Send up to eight nearby search candidates. The backend selects at most five, using existing cuisine-intent matching and scan-priority scoring (rating, review count, distance and place type), before reading their menus. Discovery ratings are not allergy assessments.
3. Reuse stored menu evidence. For shortlisted restaurants without evidence and with a website, enqueue up to five existing background refresh jobs concurrently. Missing websites and job failures remain evidence gaps. No new crawling implementation is introduced.
4. While jobs run, show a short progress reply. Poll within a 60-second budget, with bounded status requests. Re-query the same shortlist with scanning disabled, preserving conversation history, and replace the progress reply with the resulting answer. A changed question or area invalidates an older pending response.
5. Use existing menu classification and retrieval. Supply up to five classified dishes per restaurant, including available prices, descriptions, source URLs and verification questions, to the explanation layer. Ask for up to three restaurant/dish suggestions. The deterministic fallback also names actual dishes and available prices and excludes avoid/insufficient-info dishes from allergy recommendations.

General dining questions with no allergens use the same menu-collection flow without creating allergy scores. Missing evidence is never converted to a favorable assessment. Recommendations describe the inspected shortlist, not a definitive best restaurant in the area.

## Limits

- Queue/worker configuration and accessible restaurant websites are still required for background collection. This flow does not guarantee five successful menus.
- Existing stored evidence is reused; this change does not introduce a new freshness/expiration policy. Existing explicit menu refresh remains available.
- The polling budget excludes initial search and final answer-generation latency. Background jobs can finish after the UI stops waiting.
- Cuisine detection and candidate ranking retain existing heuristics. This is a bounded workflow, not an unrestricted LLM tool-selection loop.
- The LLM receives deterministic dish labels and grounding instructions. Existing answer validation is unchanged; it is not a full semantic proof that every generated claim follows its citation.
- External API calls are mocked in regression tests. Live scan quality and production deployment are separate verification steps.
