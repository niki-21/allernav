"""Opt-in live menu benchmark; writes isolated reports, never production menu records.

Run from the repository root with PYTHONPATH=apps/api. Charges Firecrawl/LLM usage.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from allernav_api.firecrawl_menu import collect_firecrawl_menu, firecrawl_configured
from allernav_api.menu_ingestion import sanitize_sections
from allernav_api.menu_normalization import azure_openai_menu_extraction_configured


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly enable paid provider calls')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--only', help='Run a single restaurant ID')
    parser.add_argument('--workers', type=int, default=1, help='Concurrent restaurants, capped at 3')
    parser.add_argument('--output', type=Path, default=Path('.data/menu-benchmark.json'))
    args = parser.parse_args()
    cases = json.loads((Path(__file__).resolve().parents[1] / 'evals/menu_restaurants.json').read_text())
    cases = [case for case in cases if not args.only or case['id'] == args.only][:max(0, min(args.limit, 20))]
    if not args.live:
        print(json.dumps({'mode':'dry_run', 'restaurants':cases}, indent=2))
        return 0
    if not firecrawl_configured() or not azure_openai_menu_extraction_configured():
        print('Configure FIRECRAWL_API_KEY and Azure OpenAI in apps/api/.env first.')
        return 2
    report = {'run_at':datetime.now(UTC).isoformat(), 'provider':'firecrawl_azure_openai',
              'acceptance':'At least 5 grounded dishes; manual branch/source and sample-price review still required.', 'results':[]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def run_case(case):
        started = time.monotonic()
        result = {**case, 'status':'failed', 'item_count':0, 'priced_count':0}
        try:
            source = collect_firecrawl_menu(case['url'], restaurant_id=case['id'])
            sections = sanitize_sections(source.sections, max_sections=24, max_items_per_section=100) if source else []
            items = [item for section in sections for item in section.items]
            result.update(item_count=len(items), priced_count=sum(bool(item.price) for item in items),
                          status='extracted_pending_review' if len(items) >= 5 else 'insufficient_items',
                          sample=[{'name':item.name, 'price':item.price, 'source_url':item.source_url} for item in items[:5]])
            if source:
                source.sections = sections
                (args.output.parent / f"{case['id']}-menu.json").write_text(source.model_dump_json(indent=2))
        except Exception as exc:
            # Exception bodies can contain secrets; publish only their class.
            result['error_type'] = type(exc).__name__
        result['elapsed_seconds'] = round(time.monotonic() - started, 1)
        return result

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 3))) as pool:
        for result in pool.map(run_case, cases):
            report['results'].append(result)
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
            print(f"{result['name']}: {result['status']}, {result['item_count']} dishes, {result['priced_count']} prices", flush=True)
    passed = sum(row['status'] == 'extracted_pending_review' for row in report['results'])
    print(f'Extraction threshold: {passed}/{len(cases)}. Report: {args.output}')
    return 0 if passed == len(cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
