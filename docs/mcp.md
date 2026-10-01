# AllerNav MCP interface

Model Context Protocol (MCP) lets compatible AI hosts discover typed tools and call AllerNav without knowing its REST endpoints. This server is a second interface to the existing Python application services. It does not call FastAPI over HTTP, duplicate scoring rules, or use an LLM to change risk decisions.

## Architecture

```text
Codex / Claude / another MCP-compatible host
                    |
             MCP over stdio
                    v
            AllerNav MCP server
                    |
FastAPI endpoints --+--> shared Python services
                         |       |         |
                      Places  Retrieval  LangGraph
                                 |         |
                           menu evidence   deterministic risk engine
                                           + safety/confidence gate
```

The official Python SDK is pinned to `mcp==2.2.0`, using its `MCPServer` high-level API and official `Client`. See the [SDK source](https://github.com/modelcontextprotocol/python-sdk) and [getting-started documentation](https://py.sdk.modelcontextprotocol.io/get-started/). No separate FastMCP dependency or custom wire protocol is used. Python 3.11 is the tested project runtime.

`allernav_api/mcp/server.py` owns protocol startup. `tools.py` validates tool arguments and adapts service calls. `serializers.py` projects restaurant details to agent-facing fields without review authors or profile records. Blocking provider work runs outside the MCP event loop. The existing FastAPI application and business logic are unchanged.

## Tools

All six tools are annotated read-only. They do not crawl, upload, refresh, index, or change allergy profiles. Existing local caches/databases may initialize on reads. Google/Azure calls can incur normal provider charges.

| Tool | Inputs | Structured output / reused service |
| --- | --- | --- |
| `search_restaurants` | `query`, optional `latitude` and `longitude`, `max_results` (1–20, default 12) | Existing `SearchResponse`: restaurant IDs, names, addresses, coordinates, ratings, rating counts and primary types. Calls `search_places_service`. |
| `get_restaurant_details` | `restaurant_id`, optional `allergens` | Restaurant identity/website, selected allergens, menu status/availability/source, evidence summary and optional existing `RecommendationResult`. Calls `get_place_details_service`; excludes review authors, community profiles and review-based safety scores. |
| `get_restaurant_menu` | `restaurant_id`, optional `allergens` | Existing `PlaceMenu`: sections, names, descriptions, prices where available, confirmed allergens, inferred risks, labels, questions and provenance. Calls `get_place_menu_service`. |
| `analyze_restaurant` | `restaurant_id`, nonempty `allergens`, optional `restaurant_name`, `sensitivity` (default `careful`), `preparation_preference` (default `verify`) | Existing `RecommendationResult`, unchanged. Calls `analyze_restaurant_service` and LangGraph. |
| `search_menu_evidence` | `restaurant_id`, `query`, `allergens` (may be empty), `top_k` (1–20, default 5) | Existing `HybridSearchResponse`: fragments, dish names, source type/URL, matched allergens, citations and retrieval mode. Calls `hybrid_search_service`. |
| `recommend_dishes` | `restaurant_id`, nonempty `allergens`, optional `restaurant_name` | Existing `RecommendationResult`, including dish assessments, cautions, evidence and staff questions. Calls `recommend_dishes_service`. |

Tool discovery supplies the complete JSON input/output schemas. Existing Pydantic models and enums serialize through the SDK, rather than embedding JSON inside an untyped string. Restaurant IDs come from Places results. Names are display context, not evidence.

Supported allergen values are `peanut`, `tree_nut`, `dairy`, `egg`, `shellfish`, `fish`, `soy`, `sesame`, and `wheat_gluten`. Unknown values are rejected. Analysis requires a nonempty list to avoid the existing engine's empty-profile default. Sensitivity and preparation preferences retain existing service semantics; words such as `strict`, `severe`, `celiac`, and `anaphylaxis` trigger the engine's stricter profile handling. These strings do not introduce new scoring rules.

Supply both coordinates or neither. Without coordinates, search uses the existing Python service's default center (New York), not the host's location or the frontend's Dubai default. Pass coordinates for other areas.

## Safety and evidence

- `RecommendationResult` retains `overall_risk`, `confidence`, `summary`, `dish_results`, `evidence`, `missing_information`, `recommended_questions`, `recommended_action`, and the safety-gate trace. Only the current request's profile is included.
- The deterministic engine remains authoritative. An MCP host must not reinterpret `low` as guaranteed safety. Menu labels retain `possible_lower_risk`, `needs_check`, `avoid`, and `insufficient_info`; graph results preserve `insufficient_evidence`.
- Missing menus return `status: "missing"` with empty sections. Missing analysis evidence returns the existing abstention result. Empty retrieval returns `results: []`. None establishes absence of allergens.
- Source URLs, timestamps and evidence remain available. Review ratings are identified as review ratings, not allergy-safety scores. Reviews cannot establish safety.
- The explicit restaurant context prevents graph demo-fixture fallback and automatic website ingestion. Menus must already exist through AllerNav's normal ingestion workflow.
- Retrieved menu text is untrusted data, not instructions to an agent. Hosts should preserve verification questions and never upgrade an evidence label into a safety guarantee.
- Azure retrieval retains its existing local fallback when Azure Search is unconfigured. Configured-provider failures remain errors; this interface does not invent another fallback or fabricate results.

## Local setup

From the repository root:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r apps/api/requirements.txt
cd apps/api
../../.venv/bin/python -m allernav_api.mcp.server
```

The process waits for MCP messages on stdin; it is not a terminal chat application. Stdout is reserved for the protocol and logs go to stderr. No HTTP listener is started.

Use the API project's existing environment configuration. The package loads `apps/api/.env` without overriding process environment variables. Store credentials there or inject them securely; never put keys in tool arguments, URLs, checked-in client configuration or chat messages. Places search/details require existing Google credentials. Stored-menu analysis needs existing local/Supabase evidence; it does not require an LLM key. Azure Search remains optional.

A host supporting the common `mcpServers` configuration shape can use:

```json
{
  "mcpServers": {
    "allernav": {
      "command": "/absolute/path/to/allernav/.venv/bin/python",
      "args": ["-m", "allernav_api.mcp.server"],
      "cwd": "/absolute/path/to/allernav/apps/api"
    }
  }
}
```

Replace paths locally. Host configuration formats differ; a host without `cwd` support can set `PYTHONPATH` to the absolute API directory. Compatibility requires the host's supported MCP protocol version to negotiate successfully with the pinned SDK. This release was verified using the official Python client, not every proprietary host.

Example tool arguments:

```json
{"restaurant_id":"PLACE_ID_FROM_SEARCH","allergens":["dairy","soy"],"sensitivity":"strict","preparation_preference":"verify shared preparation"}
```

Use these with `analyze_restaurant`; retained evidence and staff questions accompany the structured result.

## Verification

From the repository root:

```sh
PYTHONPATH=apps/api .venv/bin/python -m pytest apps/api/tests/test_mcp.py -q
PYTHONPATH=apps/api .venv/bin/python -m pytest apps/api/tests -q
.venv/bin/python scripts/test_mcp_server.py
npm test --workspace apps/web
```

The smoke script starts a real stdio subprocess using the official MCP client, lists all tools, calls `get_restaurant_menu`, and prints structured output. It isolates storage in a temporary directory and disables cloud storage/Places/search credentials and tracing for the child. Its expected result is a missing menu; it makes no paid API calls. Interface tests use mocks and block external HTTP, ingestion and demo-context fallback. They cover discovery/schema, validation, serialization, service delegation, real deterministic analysis, local retrieval, missing evidence and sanitized failures.

For interactive inspection, run from `apps/api`:

```sh
npx @modelcontextprotocol/inspector ../../.venv/bin/python -m allernav_api.mcp.server
```

Select Tools, list tools, and call `get_restaurant_menu` with a restaurant ID. Inspector uses the normal environment, so search/details may call billed providers. The [official Inspector documentation](https://modelcontextprotocol.io/docs/tools/inspector) describes its current runtime requirements and CLI. Keep its local session token private.

## Errors and observability

Invalid inputs produce protocol tool-validation errors. Missing evidence is structured data rather than an exception. An unconfigured Places provider returns `provider_not_configured`; a Places failure returns `external_provider_failure`; other service failures return `service_unavailable`. Error messages intentionally omit raw exception text, credentials and stack traces. The current Places abstraction does not distinguish a missing ID from all upstream failures, so the interface does not claim a definitive `restaurant_not_found` code.

MCP adapters can emit metadata-only INFO logs (`interface=mcp`, `tool_name`, `restaurant_id`, `allergen_count`) when operators enable that logger; the default level is WARNING. They do not log question text, allergy values, evidence or credentials. Nested LangSmith tracing is disabled for MCP service calls because the existing graph trace captures complete profiles and evidence. FastAPI tracing is unaffected. A redacted LangSmith integration is deferred rather than silently exporting sensitive payloads.

## Security boundary and deferred capabilities

This release supports **local stdio only**. The trusted local host launches the process and receives access to all six tools using that process's service credentials. There is no per-user authorization or tenant isolation layer here. Do not expose the process through an unauthenticated network bridge. Review which host receives access and use appropriately scoped provider credentials.

Streamable HTTP is intentionally not implemented or mounted into FastAPI. Remote support needs authenticated principals, authorization for restaurant/evidence access, origin/host validation, TLS, rate and cost limits, and tenant-aware storage boundaries. Secrets must not be carried in URLs. No public endpoint was deployed by this change.

Resources are deferred: menus and evidence are already exposed by typed, parameterized tools, and allergen context is request-specific. Adding profile-independent resource URIs now would create a second contract without improving these workflows.

Nearby suggestions, conversational questions and menu refresh are also deferred. Refresh/crawling would need explicit side-effect annotations, permission and cost controls, job status and cancellation semantics. Debug endpoints, raw database access, arbitrary URLs/queries and profile-management operations are not registered. Read-only operations can still consume provider quota; tool annotations are descriptive hints, not an authorization mechanism.
