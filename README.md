# AllerNav

**An agentic AI dining-safety assistant that turns restaurant menus into evidence-backed allergen guidance.**

AllerNav helps diners find restaurants, inspect menu evidence, and identify dishes to discuss with staff. A map-first Next.js interface connects to a Python/FastAPI backend for menu ingestion, a LangGraph analysis workflow, retrieval-augmented explanations, and deterministic allergen scoring.

## The problem

Restaurant information is fragmented across websites, PDF menus, images, and reviews. Dish names often omit ingredients, and positive reviews do not establish how a kitchen handles cross-contact. AllerNav brings those sources together, preserves their provenance, and makes missing information visible alongside recommendations.

A diner can select allergens, search the map, scan an official menu, inspect dish-level risk reasons, and ask questions about nearby restaurants. Results include source evidence and suggested staff questions. **AllerNav provides decision support, not a guarantee that a meal is safe.**

## Architecture

The project separates evidence collection, deterministic risk decisions, and optional language-model explanations. The frontend uses **Next.js, React, and TypeScript**; **FastAPI** exposes typed Python APIs; **LangGraph** coordinates the staged dining-analysis workflow.

```text
Diner: location, question, selected allergens
                    |
        Next.js / React / TypeScript
        Map UI + same-origin API routes
             |                  |
       Google Places       FastAPI backend
                                |
              +-----------------+--------------------+
              |                 |                    |
       LangGraph analysis   Menu ingestion     Nearby RAG service
       profile / context    HTML / JSON-LD     menu-based ranking
       menu retrieval       PDF / image OCR    evidence retrieval
       deterministic risk        |             optional explanation
       evidence / response       |                    ^
       confidence gate      SQLite / Supabase ---------+
                                 |
                         Azure AI Search
                    keyword + optional vector search

Optional durable ingestion:
FastAPI -> Supabase job + Azure Service Bus -> Azure Functions
        -> Document Intelligence OCR -> Azure OpenAI normalization
        -> persisted menu -> Azure AI Search indexing

LangSmith: traces Python graph, retrieval, normalization, explanations
```

The web app has its own search and restaurant-detail routes. It bridges to FastAPI for agent analysis and nearby RAG; deploying the web app alone does not enable the Python workflow or Azure worker.

### Agentic workflow with LangGraph

[`agent_graph.py`](apps/api/allernav_api/agent_graph.py) defines a typed `StateGraph` with seven sequential stages:

```text
Intent/profile -> Restaurant/menu retrieval -> Normalization stage
  -> Allergen risk engine -> Evidence selection -> Explanation stage
  -> Safety/confidence gate
```

Retrieval uses supplied menu context, stored menus, or official-site ingestion. The risk engine produces dish assessments, evidence fragments, missing-information notes, and recommended actions. The final stage records whether the result calls for verification, avoidance, staff questions, or abstention.

This is a bounded orchestration workflow with fixed edges. Some named stages currently record trace information around work performed by ingestion and the risk engine; they are not separate autonomous agents or LLM calls. Optional model-based normalization and RAG explanation generation live in their respective services. A sequential fallback preserves the workflow if LangGraph cannot be imported.

### Menu ingestion and document understanding

[`menu_ingestion.py`](apps/api/allernav_api/menu_ingestion.py) discovers menu links through restaurant pages, common menu paths, sitemaps, and HTML/JSON-LD parsing. Optional Google Programmable Search or SerpAPI discovery and Apify Playwright rendering extend coverage when static pages are insufficient.

**Azure Document Intelligence** extracts text from PDF and image menus. The pipeline retains source URLs, timestamps, extraction methods, and available OCR confidence. Parsing filters out navigation, promotional text, and other non-dish content. Tests also cover Arabic menu text and allergen aliases.

The durable image-menu worker uses **LangChain with Azure OpenAI structured output** to normalize English OCR into validated dish records, checking names, descriptions, and prices against the source text. That normalization path is English-only and does not translate menus. Squarespace discovery can select the newest complete numbered image edition.

### RAG and Azure AI Search

[`rag_service.py`](apps/api/allernav_api/rag_service.py) combines candidate restaurants, scanned menus, deterministic restaurant-fit scores, and retrieved evidence. Unscanned restaurants are marked as needing a scan rather than assigned a menu-based allergy-fit score.

**Azure AI Search** indexes dish-level documents with source metadata and supports keyword queries plus optional Azure OpenAI embeddings for hybrid retrieval. The checked-in [index schema](apps/api/azure_search_index.json) uses a 1,536-dimensional HNSW vector index. Without Azure Search configuration, retrieval uses local keyword and rule-based semantic matching over stored menus; this fallback does not perform vector similarity search.

For supported evidence-backed questions, explanations try Azure OpenAI, then Gemini, then a deterministic response. Retrieval relevance and generated text do not determine the underlying allergen risk labels or restaurant-fit scores.

### Deterministic allergen safety scoring

The scoring layer is implemented in [`risk_engine.py`](apps/api/allernav_api/risk_engine.py), [`menu_risk.py`](apps/api/allernav_api/menu_risk.py), and [`restaurant_scoring.py`](apps/api/allernav_api/restaurant_scoring.py).

- Dish rules inspect explicit allergen terms, structured allergen codes, inferred risks, and preparation wording.
- Menu labels distinguish `avoid`, `needs_check`, `possible_lower_risk`, and `insufficient_info`.
- Restaurant-fit scores aggregate dish classifications and penalize shared-preparation signals and relevant adverse review language.
- The graph accounts for source quality, missing ingredient information, and stricter allergy profiles, with an insufficient-evidence outcome when appropriate.

These are explainable heuristics, not a trained or clinically validated risk model. “Possible lower risk” means the available evidence still needs verification; absence of an allergen term does not establish safety. Review-based scoring also exists in the web and API layers and should be distinguished from menu-based scoring.

### Persistence, background jobs, and observability

| Component | Role implemented in this repository |
| --- | --- |
| **Supabase** | Menu records, document metadata, refresh jobs, OCR page progress, and community reviews. Supabase Auth supports Google sign-in for community submissions and points. |
| **SQLite** | Local menu and review caches when developing without durable cloud storage. |
| **Azure Service Bus** | Queues menu-refresh messages after the API persists a job. |
| **Azure Functions** | Python queue consumer with retry-aware processing, cached OCR page reuse, and up to three concurrent document extractions. Menu publication and search indexing have separate statuses. |
| **LangSmith** | Optional traces for the LangGraph workflow, retrieval, OCR normalization, and RAG explanations, including evidence and outcome metadata. |
| **Google Maps / Places** | Map display, restaurant discovery, place details, photos, and review snippets. |
| **Apify** | Optional rendered menu discovery and expanded reviews through explicit refresh requests. |

## Engineering focus and current scope

The implementation demonstrates typed API contracts, structured model output with grounding checks, retrieval with provenance, deterministic decision logic, background processing, and observable fallback paths. Existing tests cover allergen matching, insufficient evidence, prompt-injection examples, OCR parsing, retrieval citations, worker retries, and frontend request/ranking behavior.

The repository includes demo fixtures and local restaurant snapshots. Feedback and profile/saved-place endpoints include in-memory state; they are not a complete persistent user-profile system. Cloud integrations require configuration and provisioning. Tests establish specific behaviors, not clinical accuracy, production readiness, or comprehensive resistance to prompt injection.

## Repository map

```text
apps/web/
  src/app/                 Next.js interface and API routes
  src/components/          Map, allergy picker, evidence/menu panels, auth
  src/lib/                 Client API, types, ranking helpers, tests
  src/server/              Places, menu/review services, scoring, tests
apps/api/
  app.py                   FastAPI routes
  allernav_api/            Graph, ingestion, retrieval, scoring, storage
  function_app.py          Azure Functions Service Bus trigger
  tests/                   Python unit, integration-style, and RAG checks
  scripts/                 Search-index setup and manual cloud checks
  azure_search_index.json  Azure AI Search schema
  supabase.sql             Persistence schema
  AZURE_MENU_WORKER.md      Worker infrastructure and deployment guide
docs/setup.md              Local setup, configuration, deployment, diagnostics
```

## Run locally

Use **Node.js 24.x** (declared in the packages) and **Python 3.11+**. Run commands from the repository root unless stated otherwise.

```bash
npm ci
cp apps/web/.env.example apps/web/.env.local
python3 -m venv apps/api/.venv
apps/api/.venv/bin/python -m pip install -r apps/api/requirements.txt
cp apps/api/.env.example apps/api/.env
```

Fill in the Google Maps browser key and Google Places server key in the local environment files. For the full application, set `FASTAPI_API_BASE_URL=http://localhost:8000` in the web environment. Leave `NEXT_PUBLIC_API_BASE_URL` unset to keep browser requests on the Next.js routes, including community authentication routes.

Start the backend and frontend in separate terminals:

```bash
# Terminal 1
cd apps/api
.venv/bin/python -m uvicorn app:app --reload --port 8000
```

```bash
# Terminal 2, repository root
npm run dev
```

Open [the web app](http://localhost:3000) and [FastAPI's interactive API docs](http://localhost:8000/docs). Core rule-based analysis does not require an LLM key. OCR, cloud retrieval, durable refresh, community sign-in, and generated explanations each require their corresponding configuration.

See [setup and deployment](docs/setup.md) for environment-variable groups, Azure Search setup, authentication, tracing, menu-refresh examples, and deployment options. See the [Azure worker guide](apps/api/AZURE_MENU_WORKER.md) for queue infrastructure and worker operations.

## Tests and checks

```bash
# Existing frontend tests
npm test

# Existing backend tests; live Azure smoke tests remain disabled
ALLERNAV_LIVE_CLOUD_TESTS=false PYTHONPATH=apps/api \
  apps/api/.venv/bin/python -m pytest apps/api/tests

# Additional frontend checks
npm run lint
npm run build
```

Live Azure checks are opt-in and use configured cloud resources:

```bash
ALLERNAV_LIVE_CLOUD_TESTS=true PYTHONPATH=apps/api \
  apps/api/.venv/bin/python -m pytest apps/api/tests/test_live_azure_smoke.py
```

The Next.js configuration currently skips TypeScript errors during builds, so a successful build alone is not evidence of type correctness.

## Configuration and data handling

Commit only blank or placeholder configuration templates. Local environment files, caches, and databases are ignored by Git. Server credentials, including the Supabase service-role key, must never use a `NEXT_PUBLIC_` prefix.

Tracing can include selected allergens, questions, and menu evidence. Enable it deliberately and review what is sent to LangSmith and model providers before using personal data. The setup guide uses placeholders and contains no deployment credentials.
