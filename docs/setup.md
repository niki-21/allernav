# Setup and deployment

For the project overview and architecture, start with the [README](../README.md).

## Local development

Use Node.js 24.x and Python 3.11+. From the repository root:

```bash
npm ci
cp apps/web/.env.example apps/web/.env.local
python3 -m venv apps/api/.venv
apps/api/.venv/bin/python -m pip install -r apps/api/requirements.txt
cp apps/api/.env.example apps/api/.env
```

Edit these local files; do not commit credentials. The Python package loads `apps/api/.env`, and Next.js loads `apps/web/.env.local`. The root `dev.sh` checks that the web environment file exists and starts only the frontend.

Use separate Google keys:

| Variable | Location | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_GOOGLE_MAPS_API_KEY` | Web | Browser Maps JavaScript API; restrict to your site referrers. |
| `GOOGLE_PLACES_API_KEY` | Web and API | Server Places API; use server-appropriate restrictions, not browser referrers. |
| `FASTAPI_API_BASE_URL` | Web | Server-side bridge to FastAPI; use `http://localhost:8000` locally. |
| `FRONTEND_ORIGIN` | API | Additional allowed browser origins; localhost on port 3000 is already allowed. |

Keep `NEXT_PUBLIC_API_BASE_URL` unset for the usual same-origin web setup. Setting it changes the browser's API destination and bypasses Next.js routes; FastAPI does not implement every web route, including the community authentication endpoints.

Start the services in separate terminals:

```bash
# Terminal 1
cd apps/api
.venv/bin/python -m uvicorn app:app --reload --port 8000
```

```bash
# Terminal 2, repository root
npm run dev
```

The web app runs at `http://localhost:3000`; FastAPI serves interactive documentation at `http://localhost:8000/docs`.

## Optional integrations

The [API environment template](../apps/api/.env.example) lists the backend settings; the [web template](../apps/web/.env.example) covers the frontend and Next.js server. Configure each integration only in the process that uses it.

| Capability | Variables | Runtime |
| --- | --- | --- |
| PDF/image OCR | `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`, `AZURE_DOCUMENT_INTELLIGENCE_KEY` | API and worker |
| Azure AI Search | `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX_NAME` | API and worker |
| Embeddings | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` | API and worker |
| Structured OCR normalization / RAG explanations | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_CHAT_DEPLOYMENT`, `AZURE_OPENAI_CHAT_API_VERSION` | API and worker as applicable |
| Durable storage | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | API and worker; web for community reviews |
| Queue producer | `AZURE_SERVICE_BUS_SEND_CONNECTION_STRING`, `AZURE_SERVICE_BUS_MENU_QUEUE` | API |
| Queue consumer | `AZURE_SERVICE_BUS_CONNECTION_STRING` | Worker |
| Gemini explanations / recommendations | `GEMINI_API_KEY`, optional `GEMINI_MODEL` | API and/or web |
| Rendered menu discovery / expanded reviews | `APIFY_TOKEN` and related `APIFY_*` options in the API template | API/worker; web for expanded review refresh |
| Web menu search | `GOOGLE_SEARCH_API_KEY` and `GOOGLE_SEARCH_ENGINE_ID`, or `SERPAPI_API_KEY` | API and worker |
| Tracing | `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | Python processes |

Absent optional services, available fallbacks include static menu parsing, local menu storage and heuristic retrieval, and deterministic explanations. Agent analysis and nearby RAG still require FastAPI. An unconfigured Azure Search integration uses local retrieval; do not assume every configured-provider failure automatically falls back.

### Local storage and ingestion limits

The default menu database is `apps/api/.data/menu_ingestion.sqlite`; `ALLERNAV_MENU_DB` overrides its path. Review-cache settings include `ALLERNAV_REVIEWS_DB` and `APIFY_REVIEWS_CACHE_TTL_HOURS`. Relative paths resolve from the process working directory.

`MENU_INGESTION_TIMEOUT_SECONDS`, `MENU_FETCH_TIMEOUT_SECONDS`, and `WEB_MENU_SEARCH_TIMEOUT_SECONDS` bound discovery work. When extraction cannot finish in the interactive budget, traces can report `needs_background_refresh`. Menu freshness is controlled by `MENU_CACHE_TTL_HOURS`; explicit refreshes can bypass the cache.

`MENU_REFRESH_MODE` supports `auto`, `local`, and `durable`. Durable processing requires both Supabase and Service Bus. Production durable mode reports a failed job when those services are missing; local background work is not a substitute for a persistent queue on serverless hosts.

### Azure AI Search

Configure Search and, for vector retrieval, an Azure OpenAI embedding deployment. The [schema](../apps/api/azure_search_index.json) requires 1,536-dimensional embeddings; the template names `text-embedding-3-small`, but the deployment name must match your Azure resource.

Create or update the index from `apps/api`:

```bash
cd apps/api
.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(); from scripts.setup_azure_search_index import main; main()"
```

This command loads the local `.env` before running the index setup script; the script itself reads process environment variables. Index a stored restaurant menu through `POST /api/restaurants/{restaurant_id}/search-index`. Query it through `POST /api/search/hybrid`. Source metadata and citations remain available independently of retrieval score.

### Supabase and Google sign-in

1. Apply [supabase.sql](../apps/api/supabase.sql) in your Supabase SQL editor. It creates menu, document, job, OCR-page, and community-review tables.
2. Set `SUPABASE_URL` and server-only `SUPABASE_SERVICE_ROLE_KEY` in the services that persist data. The Python store accepts either a project URL or its `/rest/v1` URL.
3. For community sign-in, set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` in the web app. The browser also accepts `NEXT_PUBLIC_SUPABASE_ANON_KEY` as a fallback.
4. Enable Google in Supabase Authentication and configure the Google OAuth client. Add the callback URL shown by Supabase to the client's authorized redirect URIs.
5. Add your local and deployed web origins to the Supabase authentication URL configuration.

Browsing remains public; posting a community review requires a validated Supabase session. Review points are stored with submissions. Do not expose the service-role key in a browser environment variable.

### LangSmith tracing

Enable `LANGSMITH_TRACING=true` and supply `LANGSMITH_API_KEY` and a project name in the API/worker environment. Optional settings include `LANGSMITH_ENDPOINT` and `LANGCHAIN_CALLBACKS_BACKGROUND` in the API template.

The core graph is deterministic and does not need an OpenAI key just to produce traces. Azure OpenAI or Gemini credentials are needed only for their respective model-backed paths. Traces cover the top-level dining graph, LangGraph stages, retrieval, normalization, and RAG explanation calls. Current metadata includes selected allergens and source context; review trace inputs and retention before processing personal data.

### Apify and discovery

Apify review refresh is explicit through `/api/places/{place_id}/reviews-refresh`, keeping expanded retrieval outside ordinary place-detail requests. `APIFY_REVIEWS_ACTOR`, `APIFY_REVIEWS_LIMIT`, sorting, language, region, timeouts, and cache options are documented in the API template. Leaving the review search query blank allows the application's bounded retrieval and local allergy-relevance ranking.

Rendered menu discovery uses the configured Playwright scraper actor and bounded page/time limits. It supplements static links, sitemaps, common menu paths, and optional web search. Reviews provide supplemental caution signals; they do not establish that a dish is safe.

## Deployment

### Next.js web application

Deploy the web app with `apps/web` as the Vercel project root, the Next.js preset, `npm install` as the install command, and `npm run build` as the build command. Set the web variables in that project's environment, including `FASTAPI_API_BASE_URL` for the Python backend. This gives users one public web URL while backend services run separately.

### FastAPI service

The repository includes [apps/api/vercel.json](../apps/api/vercel.json) for a separate Python deployment. Set its root to `apps/api`, configure the API environment, and point the web bridge to its public URL. Alternatively, run `uvicorn app:app` from `apps/api` on a Python host. Set `FRONTEND_ORIGIN` when allowing direct browser access from additional origins.

SQLite and process memory are local fallbacks, not durable storage across serverless instances. Provision Supabase and the queue worker for durable menu processing.

### Azure Functions worker

Follow the [Azure menu worker guide](../apps/api/AZURE_MENU_WORKER.md) for infrastructure, send/listen credentials, application settings, publishing, and retry/dead-letter checks. Apply the Supabase schema first.

The current Function trigger is bound to **`menu-refresh`** in code. Keep the sender's `AZURE_SERVICE_BUS_MENU_QUEUE` set to that same name; changing an environment setting alone does not change the consumer binding.

For local Functions configuration, copy `apps/api/local.settings.json.example` to the ignored `apps/api/local.settings.json` and supply the worker settings. The manual worker script invokes configured services and can persist records:

```bash
cd apps/api
PYTHONPATH=. .venv/bin/python scripts/test_menu_worker_message.py ./sample-menu-job.json
```

## API examples and diagnostics

Selected FastAPI endpoints:

| Endpoint | Purpose |
| --- | --- |
| `POST /api/analyze-restaurant` | Run the dining-safety graph for a restaurant/context. |
| `POST /api/analyze-menu` | Analyze supplied menu sources. |
| `POST /api/recommend-dishes` | Assess dishes from supplied context. |
| `POST /api/chat` | Return a graph-derived explanation and recommendation. |
| `GET /api/restaurants/{id}/evidence` | Retrieve analysis evidence. |
| `POST /api/rag/nearby-suggestions` | Rank candidate restaurants and retrieve menu evidence. |
| `POST /api/places/{id}/menu-refresh` | Start a menu refresh. |
| `GET /api/menu-refresh-jobs/{job_id}` | Inspect refresh and indexing status. |
| `GET /api/places/{id}/menu` | Read a menu with optional repeated `allergens` parameters. |
| `POST /api/places/{id}/reviews-refresh` | Refresh expanded reviews. |
| `POST /api/restaurants/{id}/search-index` | Index a stored menu. |
| `POST /api/search/hybrid` | Retrieve dish evidence. |
| `POST /api/feedback` | Record feedback in process memory. |

Request schemas are available at `/docs`. To refresh a restaurant, substitute its official website and a stable place identifier:

```bash
curl -X POST 'http://localhost:8000/api/places/demo/menu-refresh?restaurant_name=Demo&website_url=https%3A%2F%2Fexample.com&force_refresh=true'
curl 'http://localhost:8000/api/menu-refresh-jobs/JOB_ID'
curl 'http://localhost:8000/api/places/demo/menu?allergens=sesame&allergens=fish'
```

The repository includes an [Arabic OCR PDF fixture](../apps/web/public/demo/allernav_arabic_menu_ocr_test.pdf) and its [HTML source](../apps/api/scripts/arabic_menu_ocr_test.html). To exercise document ingestion, serve the PDF at a publicly accessible HTTPS URL and pass that URL as `website_url`. Azure Document Intelligence must be configured. This tests source-text extraction; the durable English normalization path is not a multilingual translation service.

Check `/api/health` on the web app and `/health` on FastAPI for configuration flags. These flags indicate configured settings, not successful end-to-end cloud connectivity.

`GET /api/debug/storage` on FastAPI probes Supabase menu reads and inserts/deletes a temporary refresh-job record. Its sanitized response distinguishes missing configuration from table or API-path failures. Apply the SQL schema for missing-table errors; verify the Supabase URL for invalid-path errors such as `PGRST125`.

For durable jobs, poll the job endpoint, inspect Function logs, and check Service Bus active/dead-letter counts as described in the worker guide. A published menu remains available if search indexing fails; inspect `indexing_status` separately from job completion.
