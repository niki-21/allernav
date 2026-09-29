# Menu extraction benchmark

The fixed [20-restaurant candidate set](../apps/api/evals/menu_restaurants.json) covers Dubai restaurants, UAE shared menus, HTML menus, and menu-discovery hubs. Official source URLs were located on 29 September 2026. A source being available on the web is **not** proof that AllerNav extracts it successfully.

## Configuration

Add `FIRECRAWL_API_KEY` to the ignored `apps/api/.env`. The integration also requires the existing Azure OpenAI endpoint, key, and chat deployment. Do not put these values in the frontend or commit them.

For production, add `FIRECRAWL_API_KEY` to the Azure Function App's application settings (the Service Bus menu worker). Add it to the API service only if that service also executes background ingestion. Set `FIRECRAWL_ENABLED=false` to return to the existing ingestion path without deleting code.

Firecrawl runs only during deep background scans. It maps menu URLs, collects up to four pages, and passes text to Azure OpenAI structured extraction. Requests and input sizes are bounded. A usable result stops further crawling; an empty result or provider failure falls back to the existing ingestion pipeline. Each dish retains its source URL. Missing prices remain absent. Current normalization extracts English text only.

## Repeatable live run

From the repository root, using a Python environment with `apps/api/requirements.txt` installed:

```bash
PYTHONPATH=apps/api python apps/api/scripts/benchmark_menu_collection.py
PYTHONPATH=apps/api python apps/api/scripts/benchmark_menu_collection.py --live --limit 3
PYTHONPATH=apps/api python apps/api/scripts/benchmark_menu_collection.py --live
```

The first command only lists the cases. `--live` uses Firecrawl credits and Azure OpenAI tokens. Use `--only 3fils-dubai` to rerun one case. Results and collected evidence are saved under ignored `.data/`; the benchmark never writes to production Supabase or Azure Search.

## Acceptance and the known-working list

An automated extraction pass needs at least five grounded dishes. Prices are counted but are not mandatory: some official menus omit them. Reports include duration, source URLs, sample dishes/prices, and safe error types.

A restaurant becomes **verified** only after inspecting the saved evidence, confirming the correct location/menu, and checking sample dish-price pairs against the official source. Preserve the failed cases in the report. Keep a separately reviewed known-working set of 20 once enough restaurants pass; do not relabel untested entries as successes or treat this selected set as a representative coverage estimate.

For regressions, use the same list and retain dated run reports. Track extraction pass rate, manual review pass rate, priced-item coverage, and runtime. A first target is at least 16/20 automated passes, then manual review of all successes. A successful run does not establish menu completeness, current availability, or allergen safety.
