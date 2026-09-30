from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from . import supabase_store
from .firecrawl_menu import collect_firecrawl_menu, firecrawl_configured
from .apify_menu_discovery import (
    ApifyMenuDiscoveryError,
    RenderedMenuDiscovery,
    apify_menu_discovery_configured,
    discover_rendered_menu_evidence,
)
from .document_intelligence import (
    AzureDocumentIntelligenceClient,
    DocumentExtraction,
    document_content_type,
    extract_document_from_url,
    looks_like_document_url,
)
from .models import AllergyTag, EvidenceFragment, IngestionTraceStep, MenuItem, MenuSection, MenuSource, PlaceMenu, SourceType
from .risk_engine import is_prompt_injection, parse_raw_menu_text
from .web_menu_discovery import WebMenuCandidate, discover_web_menu_candidates, web_menu_discovery_configured


FetchHtml = Callable[[str], str | None]
ExtractDocument = Callable[[str], DocumentExtraction | None]
ExtractBytes = Callable[[bytes, str], DocumentExtraction | None]

MENU_NAVIGATION_WORDS = {
    "home",
    "hours",
    "hour",
    "reservation",
    "reservations",
    "order",
    "locations",
    "directions",
    "about",
    "contact",
    "careers",
    "privacy",
    "terms",
    "press",
    "gallery",
    "merch",
}

SCHEDULE_WORDS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
    "am",
    "pm",
    "open",
    "closed",
    "hours",
    "hour",
    "calendar",
    "event",
    "events",
    "market",
}

BEVERAGE_ONLY_WORDS = {
    "beer",
    "wine",
    "cocktail",
    "cocktails",
    "drink",
    "drinks",
    "soda",
    "coffee",
    "tea",
    "spezi",
    "cola",
    "lemonade",
    "espresso",
    "latte",
    "cappuccino",
    "lager",
    "ale",
    "ipa",
    "pilsner",
}

NON_DISH_SECTION_WORDS = {
    "about",
    "contact",
    "events",
    "gallery",
    "hours",
    "locations",
    "private events",
    "reservations",
    "visit",
}

PROMO_OR_DEAL_WORDS = {
    "combo",
    "deal",
    "deals",
    "value",
    "meal",
    "meals",
    "bundle",
    "bundles",
    "special",
    "specials",
    "starting",
    "starts",
}

PREPARATION_ONLY_WORDS = {
    "sauced",
    "fried",
    "grilled",
    "roasted",
    "steamed",
    "crispy",
    "baked",
    "spicy",
    "mild",
    "hot",
}

ADD_ON_ONLY_WORDS = {
    "add",
    "adds",
    "addon",
    "addons",
    "add-on",
    "add-ons",
    "addition",
    "additions",
    "extra",
    "extras",
    "protein",
    "proteins",
    "modifier",
    "modifiers",
    "substitute",
    "substitutions",
}

MENU_PATH_CANDIDATES = (
    "/menu",
    "/menus",
    "/menus/brunch",
    "/menus/lunch",
    "/menus/dinner",
    "/menus/desserts",
    "/food-menu",
    "/dinner-menu",
    "/lunch-menu",
    "/brunch-menu",
    "/main-menu",
    "/restaurant-menu",
)

SITEMAP_PATH_CANDIDATES = (
    "/sitemap.xml",
    "/sitemap_index.xml",
)

MENU_DISCOVERY_HUB_WORDS = (
    "menu",
    "menus",
    "food",
    "dining",
    "eat",
    "order",
    "online-ordering",
    "locations",
)

THIRD_PARTY_MENU_HOSTS = (
    "toasttab.com",
    "popmenu.com",
    "singleplatform.com",
    "chownow.com",
    "menupages.com",
    "zmenu.com",
)

LOGGER = logging.getLogger(__name__)
SENSITIVE_ENV_NAMES = (
    "SUPABASE_SERVICE_ROLE_KEY",
    "AZURE_DOCUMENT_INTELLIGENCE_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_SEARCH_API_KEY",
    "APIFY_TOKEN",
    "GOOGLE_SEARCH_API_KEY",
    "SERPAPI_API_KEY",
    "LANGSMITH_API_KEY",
)


def default_db_path() -> Path:
    configured = os.getenv("ALLERNAV_MENU_DB")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / ".data" / "menu_ingestion.sqlite"


def menu_ingestion_timeout_seconds() -> float:
    raw = os.getenv("MENU_INGESTION_TIMEOUT_SECONDS", "45")
    try:
        return max(10.0, min(50.0, float(raw)))
    except ValueError:
        return 45.0


def menu_fetch_timeout_seconds() -> float:
    raw = os.getenv("MENU_FETCH_TIMEOUT_SECONDS", "4")
    try:
        return max(1.0, min(10.0, float(raw)))
    except ValueError:
        return 4.0


def remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def sanitize_source_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed_url = parse.urlsplit(value)
        hostname = parsed_url.hostname or ""
        if not hostname:
            return None
        try:
            port = parsed_url.port
        except ValueError:
            port = None
        netloc = f"{hostname}:{port}" if port else hostname
        return parse.urlunsplit((parsed_url.scheme, netloc, parsed_url.path, "", ""))[:500]
    except ValueError:
        return None


def sanitize_log_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())[:200]


def sanitize_ingestion_exception(exc: BaseException) -> str:
    message = " ".join((str(exc) or "No error detail was provided.").split())
    for env_name in SENSITIVE_ENV_NAMES:
        secret = os.getenv(env_name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(
        r"(?i)([\"']?\b(?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?)([^\"',;\s}]+)",
        r"\1[redacted]",
        message,
    )
    message = re.sub(
        r"https?://[^\s]+",
        lambda match: sanitize_source_url(match.group(0)) or "[redacted-url]",
        message,
    )
    return f"{type(exc).__name__}: {message}"[:500]


def log_menu_event(event: str, **fields: object) -> None:
    LOGGER.info("%s %s", event, json.dumps(fields, ensure_ascii=True, sort_keys=True, default=str))


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS menu_records (
            restaurant_id TEXT PRIMARY KEY,
            restaurant_name TEXT,
            source_url TEXT,
            source_type TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            raw_text TEXT,
            menu_json TEXT NOT NULL
        )
        """
    )
    return connection


def save_menu_source(
    *,
    restaurant_id: str,
    restaurant_name: str | None,
    source: MenuSource,
    status: str = "complete",
    error_message: str | None = None,
    db_path: Path | None = None,
    save_local: bool = True,
) -> bool:
    has_grounded_items = any(section.items for section in sanitize_sections(source.sections))
    if (status != "complete" or not has_grounded_items) and load_menu_record(restaurant_id, db_path):
        return False

    fetched_at = source.source_timestamp or datetime.now(UTC).isoformat()
    remote_saved = supabase_store.save_menu_source(
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        source=source,
        status=status,
        error_message=error_message,
    )
    if not save_local:
        return remote_saved
    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO menu_records (
                restaurant_id, restaurant_name, source_url, source_type,
                fetched_at, status, error, raw_text, menu_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(restaurant_id) DO UPDATE SET
                restaurant_name=excluded.restaurant_name,
                source_url=excluded.source_url,
                source_type=excluded.source_type,
                fetched_at=excluded.fetched_at,
                status=excluded.status,
                error=excluded.error,
                raw_text=excluded.raw_text,
                menu_json=excluded.menu_json
            """,
            (
                restaurant_id,
                restaurant_name,
                source.source_url,
                source.source_type.value,
                fetched_at,
                status,
                error_message,
                source.raw_text,
                source.model_dump_json(),
            ),
        )
    return True


def load_menu_record(restaurant_id: str, db_path: Path | None = None) -> tuple[str | None, MenuSource] | None:
    supabase_record = supabase_store.load_menu_source(restaurant_id)
    if supabase_record:
        restaurant_name, source = supabase_record
        sanitized_sections = sanitize_sections(source.sections)
        if sanitized_sections:
            return restaurant_name, source.model_copy(
                update={
                    "sections": sanitized_sections,
                    "raw_text": summarize_menu_text(sanitized_sections) or None,
                }
            )

    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT restaurant_name, menu_json, status FROM menu_records WHERE restaurant_id = ?",
            (restaurant_id,),
        ).fetchone()

    if not row or row[2] != "complete":
        return None
    source = MenuSource.model_validate_json(row[1])
    sanitized_sections = sanitize_sections(source.sections)
    if not sanitized_sections:
        return None
    return row[0], source.model_copy(
        update={
            "sections": sanitized_sections,
            "raw_text": summarize_menu_text(sanitized_sections) or None,
        }
    )


def load_menu_source(restaurant_id: str, db_path: Path | None = None) -> MenuSource | None:
    record = load_menu_record(restaurant_id, db_path)
    if not record:
        return None
    return record[1]


def load_place_menu(restaurant_id: str, db_path: Path | None = None) -> PlaceMenu:
    source = load_menu_source(restaurant_id, db_path)
    if not source:
        return PlaceMenu(place_id=restaurant_id, status="missing")
    return PlaceMenu(
        place_id=restaurant_id,
        source_url=source.source_url,
        source_fetched_at=source.source_timestamp,
        status="complete",
        content_type=source.content_type,
        document_url=source.document_url,
        document_urls=source.document_urls,
        menu_version=source.menu_version,
        extraction_method=source.extraction_method,
        page_count=source.page_count,
        extraction_confidence=source.extraction_confidence,
        sections=source.sections,
    )


def stored_evidence(restaurant_id: str, db_path: Path | None = None) -> list[EvidenceFragment]:
    source = load_menu_source(restaurant_id, db_path)
    if not source:
        return []

    fragments: list[EvidenceFragment] = []
    for section in source.sections:
        for item in section.items:
            text = f"{item.name}: {item.description}" if item.description else item.name
            fragments.append(
                EvidenceFragment(
                    id=f"stored-{restaurant_id}-{len(fragments)}",
                    source_type=source.source_type,
                    source_url=source.source_url,
                    source_timestamp=source.source_timestamp,
                    dish_name=item.name,
                    text=text,
                    reliability=source.reliability,
                )
            )
    return fragments


def ingest_menu_from_website(
    *,
    restaurant_id: str,
    restaurant_name: str | None,
    website_url: str,
    restaurant_address: str | None = None,
    fetch_html: FetchHtml | None = None,
    extract_document: ExtractDocument | None = None,
    extract_bytes: ExtractBytes | None = None,
    db_path: Path | None = None,
    trace: list[IngestionTraceStep] | None = None,
    fast_only: bool = False,
    deep_scan: bool = False,
) -> MenuSource:
    active_trace = trace if trace is not None else []
    log_menu_event(
        "menu_ingestion_started",
        restaurant_name=sanitize_log_text(restaurant_name),
        website_url=sanitize_source_url(website_url),
        fast_only=fast_only,
        deep_scan=deep_scan,
    )
    try:
        source = _ingest_menu_from_website(
            restaurant_id=restaurant_id,
            restaurant_name=restaurant_name,
            website_url=website_url,
            restaurant_address=restaurant_address,
            fetch_html=fetch_html,
            extract_document=extract_document,
            extract_bytes=extract_bytes,
            db_path=db_path,
            trace=active_trace,
            fast_only=fast_only,
            deep_scan=deep_scan,
        )
    except Exception as exc:
        detail = sanitize_ingestion_exception(exc)
        append_trace_step(
            active_trace,
            step_id="menu_ingestion_error",
            label="Run menu discovery",
            status="failed",
            detail=detail,
            provider="fastapi",
            source_url=sanitize_source_url(website_url),
        )
        log_menu_event(
            "menu_ingestion_failed",
            restaurant_name=sanitize_log_text(restaurant_name),
            website_url=sanitize_source_url(website_url),
            fast_only=fast_only,
            deep_scan=deep_scan,
            error=detail,
            final_item_count=0,
        )
        raise

    log_menu_event(
        "menu_ingestion_finished",
        restaurant_name=sanitize_log_text(restaurant_name),
        website_url=sanitize_source_url(website_url),
        fast_only=fast_only,
        deep_scan=deep_scan,
        rendered_scan_attempted=any(
            step.id == "rendered_menu_scan_running" and step.status == "running" for step in active_trace
        ),
        ocr_attempted=any(
            step.id in {"document_ocr", "screenshot_ocr_running", "screenshot_ocr_complete", "screenshot_ocr_failed"}
            and step.status not in {"deferred", "skipped", "skipped_no_document"}
            for step in active_trace
        ),
        final_item_count=menu_source_item_count(source),
    )
    return source


def _ingest_menu_from_website(
    *,
    restaurant_id: str,
    restaurant_name: str | None,
    website_url: str,
    restaurant_address: str | None = None,
    fetch_html: FetchHtml | None = None,
    extract_document: ExtractDocument | None = None,
    extract_bytes: ExtractBytes | None = None,
    db_path: Path | None = None,
    trace: list[IngestionTraceStep] | None = None,
    fast_only: bool = False,
    deep_scan: bool = False,
) -> MenuSource:
    fetcher = fetch_html or fetch_html_url
    # Managed collection belongs in background scans, never the interactive search.
    if deep_scan and not fast_only and firecrawl_configured():
        try:
            managed_source = collect_firecrawl_menu(website_url, restaurant_id=restaurant_id)
        except Exception:
            # Provider/model errors may contain credentials or response content.
            managed_source = None
        if managed_source:
            managed_source.sections = sanitize_sections(managed_source.sections, max_sections=24, max_items_per_section=100)
        count = menu_source_item_count(managed_source)
        append_trace_step(
            trace, step_id="firecrawl_menu", label="Collect website menu",
            status="complete" if count else "failed", provider="firecrawl_azure_openai",
            detail=f"Extracted {count} source-grounded dishes." if count else "Managed collection produced no usable dishes; trying existing menu sources.",
            source_url=website_url, item_count=count,
        )
        if managed_source and count:
            save_menu_source(restaurant_id=restaurant_id, restaurant_name=restaurant_name, source=managed_source, db_path=db_path)
            return managed_source
    document_client = AzureDocumentIntelligenceClient()
    document_extractor = extract_document or document_client.extract_from_url
    byte_extractor = extract_bytes or (
        lambda content, content_type: document_client.extract_from_bytes(content, content_type=content_type)
    )
    started_at = time.monotonic()
    deadline = started_at + menu_ingestion_timeout_seconds()
    discovery_deadline = min(deadline, started_at + 8.0)
    static_candidate_urls = discover_candidate_urls(
        website_url,
        fetcher,
        allow_rendered_discovery=False,
        deadline=discovery_deadline,
    )
    append_trace_step(
        trace,
        step_id="source_discovery",
        label="Discover menu sources",
        status="complete" if static_candidate_urls else "failed",
        detail=(
            f"Found {len(static_candidate_urls)} website candidate URL{'s' if len(static_candidate_urls) != 1 else ''}."
            if static_candidate_urls
            else "The restaurant website did not produce any candidate menu URLs."
        ),
        provider="restaurant_website",
        source_url=website_url,
        started_at=started_at,
    )
    parse_started_at = time.monotonic()
    static_deadline = min(deadline, parse_started_at + 12.0)
    static_parse_urls = static_candidate_urls[:6]
    if fast_only:
        static_parse_urls = [url for url in static_parse_urls if not looks_like_document_url(url)]
    elif deep_scan:
        static_parse_urls = sorted(static_parse_urls, key=lambda url: not looks_like_document_url(url))
    last_source: MenuSource | None = ingest_first_matching_source(
        candidate_urls=static_parse_urls,
        fetcher=fetcher,
        document_extractor=document_extractor,
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        official_website_url=website_url,
        trace=trace,
        db_path=db_path,
        deadline=static_deadline,
    )
    static_item_count = menu_source_item_count(last_source)
    document_candidates = [url for url in static_candidate_urls if looks_like_document_url(url)]
    log_menu_event(
        "menu_ingestion_candidates",
        restaurant_name=sanitize_log_text(restaurant_name),
        website_url=sanitize_source_url(website_url),
        fast_only=fast_only,
        deep_scan=deep_scan,
        static_candidate_count=len(static_candidate_urls),
        document_candidate_count=len(document_candidates),
        ocr_attempted=bool(document_candidates and not fast_only),
    )
    append_trace_step(
        trace,
        step_id="static_extraction",
        label="Parse website menu",
        status="complete" if static_item_count else "failed",
        detail=(
            f"Extracted {static_item_count} dish-level item{'s' if static_item_count != 1 else ''} from static website content."
            if static_item_count
            else "Static HTML and structured data did not yield reliable dish-level items."
        ),
        provider="html_json_ld",
        source_url=last_source.source_url if last_source else website_url,
        item_count=static_item_count,
        started_at=parse_started_at,
    )
    if fast_only and document_candidates:
        append_trace_step(
            trace,
            step_id="document_ocr",
            label="Read menu document",
            status="deferred",
            detail="PDF or image OCR was deferred to the deeper background scan.",
            provider="azure_document_intelligence",
            source_url=document_candidates[0],
        )
    elif fast_only and not static_item_count:
        append_trace_step(
            trace,
            step_id="document_ocr",
            label="Read menu document",
            status="deferred",
            detail="Rendered discovery and screenshot OCR were deferred to the deeper background scan.",
            provider="azure_document_intelligence",
            source_url=website_url,
        )
    elif document_candidates:
        ocr_configured = AzureDocumentIntelligenceClient().configured or extract_document is not None
        used_ocr = bool(last_source and last_source.extraction_method == "azure_document_intelligence")
        append_trace_step(
            trace,
            step_id="document_ocr",
            label="Read menu document",
            status="complete" if used_ocr and static_item_count else "failed",
            detail=(
                f"Azure Document Intelligence extracted {static_item_count} usable dish-level item{'s' if static_item_count != 1 else ''}."
                if used_ocr and static_item_count
                else (
                    "A PDF or image candidate was found, but OCR returned no usable dish-level items. The source may block Azure URL access or the parser may have rejected the layout."
                    if ocr_configured
                    else "A PDF or image candidate was found, but Azure Document Intelligence is not configured on the API deployment."
                )
            ),
            provider="azure_document_intelligence",
            source_url=(last_source.document_url if last_source else None) or document_candidates[0],
            item_count=static_item_count if used_ocr else 0,
        )
    elif static_item_count:
        append_trace_step(
            trace,
            step_id="document_ocr",
            label="Read menu document",
            status="skipped_no_document",
            detail="No PDF or image menu candidate was discovered on the restaurant website.",
            provider="azure_document_intelligence",
        )
    best_source = last_source if last_source and last_source.sections else None
    if fast_only:
        return last_source or MenuSource(
            source_type=SourceType.RESTAURANT_WEBSITE,
            source_url=website_url,
            source_timestamp=datetime.now(UTC).isoformat(),
            reliability=0.25,
            sections=[],
            extraction_method="fast_html_scan",
        )
    if last_source and last_source.sections and not deep_scan:
        return last_source

    search_started_at = time.monotonic()
    search_budget = min(12.0, remaining_seconds(deadline))
    search_diagnostics: list[str] = []
    web_candidates = (
        discover_web_menu_candidates(
            restaurant_name=restaurant_name,
            website_url=website_url,
            address=restaurant_address,
            time_budget_seconds=search_budget,
            diagnostics=search_diagnostics,
        )
        if search_budget >= 1.0
        else []
    )
    excluded_urls = set(static_candidate_urls)
    web_candidate_urls = sorted(
        {candidate.url for candidate in web_candidates if candidate.url not in excluded_urls},
        key=candidate_url_priority,
    )
    web_source = ingest_first_matching_source(
        candidate_urls=web_candidate_urls,
        fetcher=fetcher,
        document_extractor=document_extractor,
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        official_website_url=website_url,
        candidate_metadata={candidate.url: candidate for candidate in web_candidates},
        trace=trace,
        db_path=db_path,
        deadline=deadline,
    )
    if web_source and web_source.extraction_method == "azure_document_intelligence":
        _record_document_ocr(trace, web_source)
    web_item_count = menu_source_item_count(web_source)
    append_trace_step(
        trace,
        step_id="web_search",
        label="Search official menu evidence",
        status="complete" if web_item_count else ("failed" if web_menu_discovery_configured() else "skipped"),
        detail=(
            f"Web discovery found {len(web_candidate_urls)} candidate URL{'s' if len(web_candidate_urls) != 1 else ''} and extracted {web_item_count} dish-level item{'s' if web_item_count != 1 else ''}."
            if web_item_count
            else (
                f"Web discovery found {len(web_candidate_urls)} candidate URL{'s' if len(web_candidate_urls) != 1 else ''}, but none produced reliable dish-level items."
                + (f" {' '.join(search_diagnostics)}" if search_diagnostics else "")
                if web_menu_discovery_configured()
                else "Google Programmable Search or SerpAPI is not configured on the API deployment."
            )
        ),
        provider="google_programmable_search_or_serpapi",
        source_url=web_source.source_url if web_source else None,
        item_count=web_item_count,
        started_at=search_started_at,
    )
    if web_source and web_source.sections:
        source = web_source.model_copy(update={"extraction_method": web_source.extraction_method or "web_menu_search"})
        save_menu_source(
            restaurant_id=restaurant_id,
            restaurant_name=restaurant_name,
            source=source,
            db_path=db_path,
        )
        if not deep_scan:
            return source
        best_source = better_menu_source(best_source, source)
    if web_source:
        last_source = web_source

    rendered_started_at = time.monotonic()
    rendered_budget = remaining_seconds(deadline)
    rendered_candidates = sorted(
        {
            url
            for url in [*static_candidate_urls, *web_candidate_urls]
            if not looks_like_document_url(url) and normalize_url(url) != normalize_url(website_url)
        },
        key=candidate_url_priority,
    )
    should_render = fetch_html is None and rendered_budget >= 3.0
    log_menu_event(
        "menu_ingestion_rendered_scan",
        restaurant_name=sanitize_log_text(restaurant_name),
        website_url=sanitize_source_url(website_url),
        rendered_scan_attempted=should_render,
        fast_only=fast_only,
        deep_scan=deep_scan,
    )
    append_trace_step(
        trace,
        step_id="rendered_menu_scan_running",
        label="Inspect rendered menu sources",
        status="running" if should_render else "skipped",
        detail=(
            "Inspecting rendered links, iframes, images, script data, and OpenGraph media."
            if should_render
            else "Rendered discovery is unavailable for this scan."
        ),
        provider="apify_playwright",
        source_url=website_url,
    )
    rendered_discovery = (
        discover_rendered_menu_evidence_safely(
            website_url,
            candidate_urls=rendered_candidates,
            timeout_seconds=rendered_budget,
        )
        if should_render
        else RenderedMenuDiscovery(urls=[], pages=[])
    )
    rendered_source = ingest_rendered_menu_pages(
        rendered_discovery=rendered_discovery,
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        official_website_url=website_url,
        trace=trace,
        db_path=db_path,
    )
    rendered_item_count = menu_source_item_count(rendered_source)
    rendered_timed_out = bool(
        rendered_discovery.error
        and any(token in rendered_discovery.error.lower() for token in ("timeout", "timed out", "deadline"))
    )
    rendered_deferred = fetch_html is None and (rendered_budget < 3.0 or rendered_timed_out)
    append_trace_step(
        trace,
        step_id="rendered_browser",
        label="Inspect rendered website",
        status=(
            "complete"
            if rendered_item_count
            else (
                "deferred"
                if rendered_deferred
                else ("failed" if apify_menu_discovery_configured() else "skipped")
            )
        ),
        detail=(
            f"Rendered browsing extracted {rendered_item_count} dish-level item{'s' if rendered_item_count != 1 else ''}."
            if rendered_item_count
            else (
                "Menu candidates were found, but dish extraction exceeded the interactive request budget. A background refresh is needed."
                if rendered_deferred
                else (
                    f"Rendered browsing found {len(rendered_discovery.pages)} menu-like page"
                    f"{'s' if len(rendered_discovery.pages) != 1 else ''} and {len(rendered_discovery.urls)} candidate link"
                    f"{'s' if len(rendered_discovery.urls) != 1 else ''}, but no usable dishes."
                )
                if apify_menu_discovery_configured()
                else "Apify rendered menu discovery is not configured on the API deployment."
            )
        ),
        provider="apify_playwright",
        source_url=rendered_source.source_url,
        item_count=rendered_item_count,
        started_at=rendered_started_at,
    )
    if rendered_source.sections:
        if not deep_scan:
            return rendered_source
        best_source = better_menu_source(best_source, rendered_source)
    if rendered_source.source_url:
        last_source = rendered_source

    rendered_candidate_urls = [url for url in rendered_discovery.urls if url not in static_candidate_urls]
    rendered_link_source = ingest_first_matching_source(
        candidate_urls=rendered_candidate_urls,
        fetcher=fetcher,
        document_extractor=document_extractor,
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        official_website_url=website_url,
        trace=trace,
        db_path=db_path,
        deadline=deadline,
    )
    if rendered_link_source and rendered_link_source.extraction_method == "azure_document_intelligence":
        _record_document_ocr(trace, rendered_link_source)
    if rendered_link_source and rendered_link_source.sections:
        if not deep_scan:
            return rendered_link_source
        best_source = better_menu_source(best_source, rendered_link_source)
    if rendered_link_source:
        last_source = rendered_link_source

    screenshot_source: MenuSource | None = None
    if not menu_source_item_count(rendered_link_source) and not rendered_item_count:
        screenshot_source = ingest_rendered_screenshot_ocr(
            rendered_discovery=rendered_discovery,
            extract_bytes=byte_extractor,
            restaurant_id=restaurant_id,
            restaurant_name=restaurant_name,
            official_website_url=website_url,
            trace=trace,
            db_path=db_path,
        )
        if screenshot_source and screenshot_source.sections:
            _record_document_ocr(trace, screenshot_source)
            if not deep_scan:
                return screenshot_source
            best_source = better_menu_source(best_source, screenshot_source)
        if screenshot_source:
            last_source = screenshot_source

    if not document_candidates and not any(step.id == "document_ocr" for step in trace or []):
        append_trace_step(
            trace,
            step_id="document_ocr",
            label="Read menu document",
            status="skipped_no_document",
            detail="No direct document or rendered screenshot was available for OCR.",
            provider="azure_document_intelligence",
        )

    if best_source and best_source.sections:
        save_menu_source(
            restaurant_id=restaurant_id,
            restaurant_name=restaurant_name,
            source=best_source,
            db_path=db_path,
        )
        return best_source

    failed_source = last_source or MenuSource(
        source_type=SourceType.RESTAURANT_WEBSITE,
        source_url=website_url,
        source_timestamp=datetime.now(UTC).isoformat(),
        reliability=0.7,
        raw_text=None,
        sections=[],
    )
    needs_background_refresh = any(step.status == "deferred" for step in trace or [])
    save_menu_source(
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        source=failed_source,
        status="needs_background_refresh" if needs_background_refresh else "failed",
        error_message=(
            "Menu candidates were found, but extraction exceeded the interactive request budget."
            if needs_background_refresh
            else "No structured menu items were extracted from official website pages."
        ),
        db_path=db_path,
    )
    return failed_source


def menu_source_item_count(source: MenuSource | None) -> int:
    if not source:
        return 0
    return sum(len(section.items) for section in source.sections)


def better_menu_source(current: MenuSource | None, candidate: MenuSource | None) -> MenuSource | None:
    if not candidate or not candidate.sections:
        return current
    if not current or not current.sections:
        return candidate
    current_rank = (menu_source_item_count(current), current.reliability)
    candidate_rank = (menu_source_item_count(candidate), candidate.reliability)
    return candidate if candidate_rank > current_rank else current


def append_trace_step(
    trace: list[IngestionTraceStep] | None,
    *,
    step_id: str,
    label: str,
    status: str,
    detail: str,
    provider: str | None = None,
    source_url: str | None = None,
    item_count: int | None = None,
    started_at: float | None = None,
) -> None:
    if trace is None:
        return
    duration_ms = round((time.monotonic() - started_at) * 1000) if started_at is not None else None
    trace.append(
        IngestionTraceStep(
            id=step_id,
            label=label,
            status=status,
            detail=detail,
            provider=provider,
            source_url=source_url,
            item_count=item_count,
            duration_ms=duration_ms,
        )
    )


def ingest_first_matching_source(
    *,
    candidate_urls: list[str],
    fetcher: FetchHtml,
    document_extractor: ExtractDocument,
    restaurant_id: str,
    restaurant_name: str | None,
    official_website_url: str | None = None,
    candidate_metadata: dict[str, WebMenuCandidate] | None = None,
    trace: list[IngestionTraceStep] | None = None,
    db_path: Path | None = None,
    deadline: float | None = None,
) -> MenuSource | None:
    last_source: MenuSource | None = None
    for candidate_url in candidate_urls:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if looks_like_document_url(candidate_url):
            identity = validate_source_identity(
                candidate_url=candidate_url,
                official_website_url=official_website_url,
                restaurant_name=restaurant_name,
                metadata=candidate_metadata.get(candidate_url) if candidate_metadata else None,
            )
            _record_source_identity(trace, candidate_url, *identity)
            if not identity[0]:
                continue
            source = parse_menu_document(candidate_url, document_extractor)
            last_source = source
            if source.sections:
                save_menu_source(
                    restaurant_id=restaurant_id,
                    restaurant_name=restaurant_name,
                    source=source,
                    db_path=db_path,
                )
                return source
            continue

        page = fetcher(candidate_url)
        if not page:
            continue
        identity = validate_source_identity(
            candidate_url=candidate_url,
            official_website_url=official_website_url,
            restaurant_name=restaurant_name,
            page_text=page,
            metadata=candidate_metadata.get(candidate_url) if candidate_metadata else None,
        )
        _record_source_identity(trace, candidate_url, *identity)
        if not identity[0]:
            continue
        source = parse_menu_html(page, candidate_url)
        last_source = source
        if source.sections:
            save_menu_source(
                restaurant_id=restaurant_id,
                restaurant_name=restaurant_name,
                source=source,
                db_path=db_path,
            )
            return source
    return last_source


def ingest_rendered_menu_pages(
    *,
    rendered_discovery: RenderedMenuDiscovery,
    restaurant_id: str,
    restaurant_name: str | None,
    official_website_url: str | None = None,
    trace: list[IngestionTraceStep] | None = None,
    db_path: Path | None = None,
) -> MenuSource:
    last_source = MenuSource(
        source_type=SourceType.RESTAURANT_WEBSITE,
        source_url=None,
        source_timestamp=datetime.now(UTC).isoformat(),
        reliability=0.25,
        sections=[],
        extraction_method="apify_rendered_text",
    )
    for rendered_page in rendered_discovery.pages:
        identity = validate_source_identity(
            candidate_url=rendered_page.url,
            official_website_url=official_website_url,
            restaurant_name=restaurant_name,
            page_text=f"{rendered_page.title or ''}\n{rendered_page.visible_text}",
        )
        _record_source_identity(trace, rendered_page.url, *identity)
        if not identity[0]:
            continue
        source = parse_rendered_menu_text(rendered_page.visible_text, rendered_page.url)
        last_source = source
        if source.sections:
            save_menu_source(
                restaurant_id=restaurant_id,
                restaurant_name=restaurant_name,
                source=source,
                db_path=db_path,
            )
            return source
    return last_source


def ingest_rendered_screenshot_ocr(
    *,
    rendered_discovery: RenderedMenuDiscovery,
    extract_bytes: ExtractBytes,
    restaurant_id: str,
    restaurant_name: str | None,
    official_website_url: str | None,
    trace: list[IngestionTraceStep] | None = None,
    db_path: Path | None = None,
) -> MenuSource | None:
    screenshot_pages = [page for page in rendered_discovery.pages if page.screenshot_png]
    if not screenshot_pages:
        append_trace_step(
            trace,
            step_id="screenshot_ocr_failed",
            label="Read rendered menu screenshot",
            status="failed",
            detail="Rendered discovery did not return a menu-page screenshot for OCR.",
            provider="azure_document_intelligence",
            source_url=official_website_url,
        )
        return None

    last_source: MenuSource | None = None
    for page in screenshot_pages:
        accepted, detail = validate_source_identity(
            candidate_url=page.url,
            official_website_url=official_website_url,
            restaurant_name=restaurant_name,
            page_text=f"{page.title or ''}\n{page.visible_text}",
        )
        _record_source_identity(trace, page.url, accepted, detail)
        if not accepted:
            continue

        started_at = time.monotonic()
        append_trace_step(
            trace,
            step_id="screenshot_ocr_running",
            label="Read rendered menu screenshot",
            status="running",
            detail="Sending the official rendered menu screenshot to Azure Document Intelligence.",
            provider="azure_document_intelligence",
            source_url=page.url,
        )
        extraction = extract_bytes(page.screenshot_png or b"", "image/png")
        source = menu_source_from_document_extraction(
            extraction,
            source_url=page.url,
            document_url=page.url,
            fallback_content_type="image/png",
            extraction_method="azure_document_intelligence_screenshot",
        )
        last_source = source
        item_count = menu_source_item_count(source)
        append_trace_step(
            trace,
            step_id="screenshot_ocr_complete" if item_count else "screenshot_ocr_failed",
            label="Read rendered menu screenshot",
            status="complete" if item_count else "failed",
            detail=(
                f"Screenshot OCR extracted {item_count} usable dish-level item"
                f"{'s' if item_count != 1 else ''}."
                if item_count
                else "Azure Document Intelligence returned no usable dish-level items from the screenshot."
            ),
            provider="azure_document_intelligence",
            source_url=page.url,
            item_count=item_count,
            started_at=started_at,
        )
        if source.sections:
            save_menu_source(
                restaurant_id=restaurant_id,
                restaurant_name=restaurant_name,
                source=source,
                db_path=db_path,
            )
            return source
    return last_source


def validate_source_identity(
    *,
    candidate_url: str,
    official_website_url: str | None,
    restaurant_name: str | None,
    page_text: str | None = None,
    metadata: WebMenuCandidate | None = None,
) -> tuple[bool, str]:
    candidate_host = _hostname(candidate_url)
    official_host = _hostname(official_website_url)
    if candidate_host and official_host and (
        candidate_host == official_host
        or candidate_host.endswith(f".{official_host}")
    ):
        return True, f"Accepted official-domain source {candidate_host}."

    normalized_name = _identity_text(restaurant_name)
    if not official_host and not normalized_name:
        return True, "Accepted source because no restaurant identity context was supplied."
    evidence = _identity_text(
        " ".join(
            value
            for value in (
                getattr(metadata, "title", None) if metadata else None,
                getattr(metadata, "snippet", None) if metadata else None,
                page_text,
            )
            if value
        )
    )
    if normalized_name and re.search(rf"\b{re.escape(normalized_name)}\b", evidence):
        return True, "Accepted third-party source because its metadata or page content identifies the restaurant."
    return False, "Rejected source because its domain and content do not identify the selected restaurant."


def _hostname(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (parse.urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _identity_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _record_source_identity(
    trace: list[IngestionTraceStep] | None,
    source_url: str,
    accepted: bool,
    detail: str,
) -> None:
    if trace is None:
        return
    trace[:] = [step for step in trace if step.id != "source_identity_check"]
    trace.append(
        IngestionTraceStep(
            id="source_identity_check",
            label="Verify menu source identity",
            status="accepted" if accepted else "rejected",
            detail=detail,
            provider="source_identity_validator",
            source_url=source_url,
        )
    )


def _record_document_ocr(trace: list[IngestionTraceStep] | None, source: MenuSource) -> None:
    if trace is None:
        return
    item_count = menu_source_item_count(source)
    trace[:] = [step for step in trace if step.id != "document_ocr"]
    trace.append(
        IngestionTraceStep(
            id="document_ocr",
            label="Read menu document",
            status="complete" if item_count else "failed",
            detail=(
                f"Azure Document Intelligence extracted {item_count} usable dish-level item"
                f"{'s' if item_count != 1 else ''}."
                if item_count
                else "Azure Document Intelligence returned no usable dish-level items."
            ),
            provider="azure_document_intelligence",
            source_url=source.document_url or source.source_url,
            item_count=item_count,
        )
    )


def discover_candidate_urls(
    website_url: str,
    fetch_html: FetchHtml | None = None,
    *,
    allow_rendered_discovery: bool | None = None,
    rendered_urls: list[str] | None = None,
    deadline: float | None = None,
) -> list[str]:
    normalized = normalize_url(website_url)
    if not normalized:
        return []

    candidates = [normalized]
    fetcher = fetch_html or fetch_html_url
    use_rendered_discovery = fetch_html is None if allow_rendered_discovery is None else allow_rendered_discovery
    fetched_pages: dict[str, str | None] = {}

    def fetch_once(url: str) -> str | None:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if url not in fetched_pages:
            fetched_pages[url] = fetcher(url)
        return fetched_pages[url]

    homepage = fetch_once(normalized)
    if homepage:
        for url in sorted(extract_candidate_menu_urls(homepage, normalized), key=candidate_url_priority):
            if url not in candidates:
                candidates.append(url)

    if use_rendered_discovery:
        urls = rendered_urls
        if urls is None:
            urls = discover_rendered_menu_evidence_safely(normalized).urls
        for url in sorted(urls, key=candidate_url_priority):
            if url not in candidates:
                candidates.append(url)

    for sitemap_url in sitemap_url_candidates(normalized):
        if deadline is not None and time.monotonic() >= deadline:
            break
        sitemap = fetch_once(sitemap_url)
        if not sitemap:
            continue
        for url in sorted(extract_sitemap_menu_urls(sitemap, normalized), key=candidate_url_priority):
            if url not in candidates:
                candidates.append(url)

    for url in common_menu_url_candidates(normalized):
        if url not in candidates:
            candidates.append(url)

    if homepage:
        for hub_url in sorted(extract_menu_discovery_hub_urls(homepage, normalized), key=candidate_url_priority)[:4]:
            if deadline is not None and time.monotonic() >= deadline:
                break
            hub_page = fetch_once(hub_url)
            if not hub_page:
                continue
            for url in sorted(extract_candidate_menu_urls(hub_page, hub_url), key=candidate_url_priority):
                if url not in candidates:
                    candidates.append(url)

    return sorted(candidates, key=candidate_url_priority)[:16]


def discover_rendered_menu_evidence_safely(
    website_url: str,
    *,
    candidate_urls: list[str] | None = None,
    timeout_seconds: float | None = None,
) -> RenderedMenuDiscovery:
    try:
        return discover_rendered_menu_evidence(
            website_url,
            candidate_urls=candidate_urls,
            timeout_seconds=timeout_seconds,
        )
    except ApifyMenuDiscoveryError as exc:
        return RenderedMenuDiscovery(urls=[], pages=[], error=str(exc) or "Rendered menu discovery failed.")


def fetch_html_url(url: str) -> str | None:
    normalized = normalize_url(url)
    if not normalized:
        return None
    req = request.Request(normalized)
    req.add_header("User-Agent", "AllerNavMenuBot/1.0 (+https://allernav.local)")
    req.add_header("Accept", "text/html,application/xhtml+xml")
    try:
        with request.urlopen(req, timeout=menu_fetch_timeout_seconds()) as response:
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                return None
            return response.read().decode("utf-8", errors="ignore")
    except (error.HTTPError, error.URLError, TimeoutError, ValueError):
        return None


def parse_menu_html(html_text: str, source_url: str) -> MenuSource:
    timestamp = datetime.now(UTC).isoformat()
    sections = sanitize_sections(parse_json_ld_menus(html_text) + parse_visible_html_menu(html_text))
    raw_text = summarize_menu_text(sections)
    return MenuSource(
        source_type=SourceType.RESTAURANT_WEBSITE,
        source_url=source_url,
        source_timestamp=timestamp,
        reliability=0.78 if sections else 0.35,
        raw_text=raw_text or None,
        sections=sections,
    )


def parse_menu_document(document_url: str, extract_document: ExtractDocument | None = None) -> MenuSource:
    extractor = extract_document or extract_document_from_url
    extraction = extractor(document_url)
    return menu_source_from_document_extraction(
        extraction,
        source_url=document_url,
        document_url=document_url,
        fallback_content_type=document_content_type(document_url),
    )


def menu_source_from_document_extraction(
    extraction: DocumentExtraction | None,
    *,
    source_url: str,
    document_url: str,
    fallback_content_type: str,
    extraction_method: str | None = None,
) -> MenuSource:
    timestamp = datetime.now(UTC).isoformat()
    if not extraction:
        return MenuSource(
            source_type=SourceType.OFFICIAL_MENU,
            source_url=source_url,
            source_timestamp=timestamp,
            reliability=0.2,
            raw_text=None,
            sections=[],
            content_type=fallback_content_type,
            document_url=document_url,
            extraction_method=extraction_method or "azure_document_intelligence",
        )

    cleaned_content = clean_ocr_text(extraction.content)
    sections = sanitize_sections(parse_raw_menu_text(cleaned_content))
    sections = [
        section.model_copy(
            update={
                "items": [
                    item.model_copy(
                        update={
                            "source_url": document_url,
                            "ocr_confidence": extraction.confidence,
                        }
                    )
                    for item in section.items
                ]
            }
        )
        for section in sections
    ]
    confidence = extraction.confidence if extraction.confidence is not None else 0.55
    reliability = round(min(0.76, max(0.22, confidence)), 2) if sections else 0.22
    return MenuSource(
        source_type=SourceType.OFFICIAL_MENU,
        source_url=source_url,
        source_timestamp=timestamp,
        reliability=reliability,
        raw_text=cleaned_content or None,
        sections=sections,
        content_type=extraction.content_type,
        document_url=document_url,
        extraction_method=extraction_method or extraction.extraction_method,
        page_count=extraction.page_count,
        extraction_confidence=extraction.confidence,
    )


def clean_ocr_text(content: str) -> str:
    return "\n".join(
        line.strip()
        for line in content.splitlines()
        if line.strip() and not is_prompt_injection(line)
    )[:100_000]


def parse_rendered_menu_text(text: str, source_url: str) -> MenuSource:
    timestamp = datetime.now(UTC).isoformat()
    sections = sanitize_sections(parse_raw_menu_text("\n".join(line for line in text.splitlines() if not is_prompt_injection(line))))
    return MenuSource(
        source_type=SourceType.RESTAURANT_WEBSITE,
        source_url=source_url,
        source_timestamp=timestamp,
        reliability=0.58 if sections else 0.25,
        raw_text=summarize_menu_text(sections) or None,
        sections=sections,
        extraction_method="apify_rendered_text",
    )


def parse_json_ld_menus(html_text: str) -> list[MenuSection]:
    sections: list[MenuSection] = []
    for raw_json in re.findall(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>([\s\S]*?)</script>",
        html_text,
        flags=re.IGNORECASE,
    ):
        try:
            payload = json.loads(html.unescape(raw_json.strip()))
        except json.JSONDecodeError:
            continue
        for node in flatten_json_ld(payload):
            sections.extend(extract_menu_sections(node))
    return sections


def parse_visible_html_menu(html_text: str) -> list[MenuSection]:
    cleaned = re.sub(r"<(script|style|nav|footer|header)[^>]*>[\s\S]*?</\1>", "\n", html_text, flags=re.IGNORECASE)
    blocks = re.findall(r"<(?:article|li|div|section)[^>]*(?:menu|item|dish)[^>]*>([\s\S]*?)</(?:article|li|div|section)>", cleaned, flags=re.IGNORECASE)
    items: list[MenuItem] = []

    for block in blocks:
        heading_match = re.search(r"<(h2|h3|h4|strong)[^>]*>([\s\S]*?)</\1>", block, flags=re.IGNORECASE)
        heading_html = heading_match.group(2) if heading_match else ""
        code_text = " ".join(
            html_to_text(value)
            for value in re.findall(r"<small[^>]*>([\s\S]*?)</small>", heading_html, flags=re.IGNORECASE)
        )
        heading_without_codes = re.sub(r"<small[^>]*>[\s\S]*?</small>", "", heading_html, flags=re.IGNORECASE)
        name = html_to_text(heading_without_codes) if heading_match else first_tag_text(block, ("h2", "h3", "h4", "strong"))
        description = first_tag_text(block, ("p", "span"))
        if not name:
            continue
        name, price = split_menu_heading(name)
        allergen_codes, confirmed_allergens = parse_menu_allergen_codes(code_text)
        item = build_menu_item(
            name,
            description,
            price,
            confirmed_allergens=confirmed_allergens,
            allergen_codes=allergen_codes,
        )
        if item:
            items.append(item)

    if not items:
        text = html_to_text(cleaned)
        sections = parse_raw_menu_text("\n".join(line for line in text.splitlines() if not is_prompt_injection(line)))
        return sanitize_sections(sections)

    return [MenuSection(title="Extracted menu", items=items)]


def extract_candidate_menu_urls(html_text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for href, label in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", html_text, flags=re.IGNORECASE):
        text = html_to_text(label).lower()
        target = href.lower()
        absolute = absolute_url(href, base_url)
        if not absolute:
            continue
        signal_text = f"{target} {text} {parse.urlparse(absolute).netloc.lower()}"
        if (
            not re.search(r"menu|food|dinner|lunch|brunch|order|toast|popmenu|singleplatform|chownow", signal_text)
            and not looks_like_document_url(absolute)
            and not is_known_menu_provider_url(absolute)
        ):
            continue
        if absolute and absolute not in urls:
            urls.append(absolute)
    return urls


def extract_menu_discovery_hub_urls(html_text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for href, label in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", html_text, flags=re.IGNORECASE):
        absolute = absolute_url(href, base_url)
        if not absolute or looks_like_document_url(absolute):
            continue
        text = html_to_text(label).lower()
        parsed = parse.urlparse(absolute)
        signal_text = f"{parsed.path.lower()} {text} {parsed.netloc.lower()}"
        if not any(word in signal_text for word in MENU_DISCOVERY_HUB_WORDS):
            continue
        if absolute not in urls:
            urls.append(absolute)
    return urls


def extract_sitemap_menu_urls(xml_text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    loc_values = re.findall(r"<loc>\s*([^<]+)\s*</loc>", xml_text, flags=re.IGNORECASE)
    for loc in loc_values:
        cleaned = html.unescape(loc).strip()
        absolute = absolute_url(cleaned, base_url)
        if not absolute:
            continue
        parsed = parse.urlparse(absolute)
        signal_text = f"{parsed.path.lower()} {parsed.netloc.lower()}"
        if (
            not re.search(r"menu|menus|food|dinner|lunch|brunch|order|toast|popmenu|singleplatform|chownow", signal_text)
            and not looks_like_document_url(absolute)
            and not is_known_menu_provider_url(absolute)
        ):
            continue
        if absolute not in urls:
            urls.append(absolute)
    return urls


def common_menu_url_candidates(base_url: str) -> list[str]:
    parsed = parse.urlparse(base_url)
    root = parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return [parse.urljoin(root, path) for path in MENU_PATH_CANDIDATES]


def sitemap_url_candidates(base_url: str) -> list[str]:
    parsed = parse.urlparse(base_url)
    root = parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return [parse.urljoin(root, path) for path in SITEMAP_PATH_CANDIDATES]


def candidate_url_priority(url: str) -> tuple[int, str]:
    parsed = parse.urlparse(url)
    path = parsed.path.lower()
    host = parsed.netloc.lower()
    if looks_like_document_url(url):
        return (0, url)
    if is_known_menu_provider_url(url):
        return (1, url)
    if re.search(r"/(menu|menus|food-menu|dinner-menu|lunch-menu|brunch-menu)(/|$)", path):
        return (2, url)
    if re.search(r"(menu|food|dinner|lunch|brunch)", path):
        return (3, url)
    if "order" in path or "order" in host:
        return (4, url)
    return (5, url)


def is_known_menu_provider_url(url: str) -> bool:
    try:
        host = parse.urlparse(url).netloc.lower()
    except ValueError:
        return False
    return any(provider in host for provider in THIRD_PARTY_MENU_HOSTS)


def extract_menu_sections(value: Any, fallback_title: str = "Menu") -> list[MenuSection]:
    if isinstance(value, list):
        return [section for entry in value for section in extract_menu_sections(entry, fallback_title)]
    if not isinstance(value, dict):
        return []

    schema_type = schema_types(value)
    title = clean_text(value.get("name")) or fallback_title
    candidates = [
        value.get("hasMenuSection"),
        value.get("hasMenuItem"),
        value.get("hasPart"),
        value.get("mainEntity"),
        value.get("menuSection"),
        value.get("menu"),
    ]
    child_values = flatten_candidates(candidates)
    child_sections = [section for child in child_values for section in extract_menu_sections(child, title)]
    child_items = [item for child in child_values if (item := extract_menu_item(child))]

    if "menusection" in schema_type or child_items:
        return [
            *child_sections,
            MenuSection(title=title, items=dedupe_items(child_items)),
        ] if child_items else child_sections

    if "menu" in schema_type:
        return child_sections
    return child_sections


def extract_menu_item(value: Any) -> MenuItem | None:
    if not isinstance(value, dict):
        return None
    schema_type = schema_types(value)
    name = clean_text(value.get("name"))
    description = clean_text(value.get("description"))
    offers = value.get("offers") if isinstance(value.get("offers"), dict) else {}
    price = clean_text(value.get("price")) or clean_text(offers.get("price")) if isinstance(offers, dict) else None

    if "menuitem" not in schema_type and not (name and description):
        return None
    if not name:
        return None
    return build_menu_item(name, description, price)


MENU_ALLERGEN_CODE_MAP: dict[str, AllergyTag] = {
    "D": AllergyTag.DAIRY,
    "E": AllergyTag.EGG,
    "F": AllergyTag.FISH,
    "G": AllergyTag.WHEAT_GLUTEN,
    "P": AllergyTag.PEANUT,
    "S": AllergyTag.SOY,
    "SE": AllergyTag.SESAME,
    "C": AllergyTag.SHELLFISH,
    "SF": AllergyTag.SHELLFISH,
    "TN": AllergyTag.TREE_NUT,
}


def split_menu_heading(value: str) -> tuple[str, str | None]:
    cleaned = clean_text(value) or ""
    match = re.match(r"^(.*?)\s*\|\s*(?:AED\s*)?(\d+(?:\.\d{1,2})?)\s*$", cleaned, flags=re.IGNORECASE)
    if not match:
        return cleaned, None
    return match.group(1).strip(), f"AED {match.group(2)}"


def parse_menu_allergen_codes(value: str) -> tuple[list[str], list[AllergyTag]]:
    codes = [code.upper() for code in re.findall(r"\b(?:TN|SF|SE|D|E|F|G|P|S|C)\b", value.upper())]
    unique_codes = list(dict.fromkeys(codes))
    allergens = list(dict.fromkeys(MENU_ALLERGEN_CODE_MAP[code] for code in unique_codes))
    return unique_codes, allergens


def build_menu_item(
    name: str,
    description: str | None = None,
    price: str | None = None,
    *,
    confirmed_allergens: list[AllergyTag] | None = None,
    allergen_codes: list[str] | None = None,
) -> MenuItem | None:
    name = clean_text(name) or ""
    description = clean_text(description)
    if not looks_like_real_menu_item(name, description):
        return None
    return MenuItem(
        name=name,
        description=description,
        price=clean_text(price),
        confirmed_allergens=confirmed_allergens or [],
        allergen_codes=allergen_codes or [],
    )


def sanitize_sections(
    sections: Iterable[MenuSection],
    *,
    max_sections: int = 8,
    max_items_per_section: int = 30,
) -> list[MenuSection]:
    cleaned: list[MenuSection] = []
    seen_sections: set[str] = set()
    for section in sections:
        title = clean_text(section.title) or "Menu"
        if is_non_dish_section_title(title):
            continue
        key = title.lower()
        items = dedupe_items(item for item in section.items if looks_like_real_menu_item(item.name, item.description))
        if not items or key in seen_sections:
            continue
        seen_sections.add(key)
        cleaned.append(MenuSection(title=title, items=items[:max_items_per_section]))
    return cleaned[:max_sections]


def looks_like_real_menu_item(name: str, description: str | None = None) -> bool:
    if re.search(r"^menus?\b|\b(?:added|uploaded|updated)\s+by\b|\b(?:days?|weeks?|months?|years?)\s+ago\b", name.strip(), re.IGNORECASE):
        return False
    if is_prompt_injection(name) or (description and is_prompt_injection(description)):
        return False
    normalized = name.lower()
    if " ".join(normalized.split()).strip(" :-") in {
        "menu category",
        "menu categories",
        "allergen information",
        "allergens",
    }:
        return False
    description_normalized = (description or "").lower()
    combined = f"{normalized} {description_normalized}"
    terms = re.split(r"[^a-z0-9]+", normalized)
    if len(name) < 3 or len(name) > 80:
        return False
    if sum(1 for word in MENU_NAVIGATION_WORDS if word in terms) >= 2:
        return False
    if re.search(r"privacy|copyright|newsletter|instagram|facebook|careers|gift card", normalized):
        return False
    if re.fullmatch(r"(?:aed|usd|eur|gbp)?\s*\d+(?:\.\d+)?\s+allergens?", normalized.strip()):
        return False
    if looks_like_schedule_or_event_text(name, description):
        return False
    if looks_like_non_dish_marketing_text(name, description):
        return False
    if looks_like_meal_deal_or_promo(name, description):
        return False
    if looks_like_add_on_modifier_row(name, description):
        return False
    if looks_like_preparation_phrase_without_dish(name, description):
        return False
    if contains_arabic(name) and description and contains_arabic(description):
        return len(name.strip()) >= 3 and len(description.strip()) >= 3
    if len([term for term in terms if term]) <= 1 and not description:
        return False
    return menu_item_quality_score(name, description) >= 4


def contains_arabic(value: str) -> bool:
    return bool(re.search(r"[\u0600-\u06ff]", value))


def menu_item_quality_score(name: str, description: str | None = None) -> int:
    normalized = name.lower()
    description_normalized = (description or "").lower()
    combined = f"{normalized} {description_normalized}"
    terms = [term for term in re.split(r"[^a-z0-9]+", normalized) if term]
    score = 0

    if 2 <= len(terms) <= 7:
        score += 1
    if description and 8 <= len(description) <= 180:
        score += 2
    if looks_like_food_text(combined):
        score += 2
    if re.search(r"\$\d|\b\d{1,3}\.\d{2}\b", combined):
        score += 1
    if re.search(r"\b(with|served|over|topped|sauce|contains|ingredient|ingredients)\b", combined):
        score += 1
    if re.search(r"\b(menu|plate|bowl|sandwich|salad|roll|taco|burger|pizza|pasta|noodle|soup|entree)\b", combined):
        score += 1

    if is_beverage_only(name, description):
        score -= 3
    if re.search(r"\b(hours?|open|closed|event|events|calendar|reservation|book|order online|located)\b", combined):
        score -= 3
    if re.search(r"\b(gift cards?|newsletter|follow us|learn more|read more|sign up|subscribe|directions)\b", combined):
        score -= 3
    if re.search(r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", combined):
        score -= 2
    return score


def is_non_dish_section_title(title: str) -> bool:
    normalized = title.lower().strip()
    return normalized in NON_DISH_SECTION_WORDS or bool(
        re.search(r"\b(hours?|events?|reservations?|contact|location|gallery|press)\b", normalized)
    )


def looks_like_non_dish_marketing_text(name: str, description: str | None = None) -> bool:
    text = f"{name} {description or ''}".lower()
    if re.search(r"\b(gift cards?|newsletter|follow us|instagram|facebook|tiktok|careers|privacy|terms)\b", text):
        return True
    if re.search(r"\b(order online|book now|reserve|make a reservation|view menu|download menu)\b", text):
        return True
    if re.search(r"\b(private events?|catering inquiries|press inquiries|located at|visit us)\b", text):
        return True
    if re.search(r"\b(our|we|us|story|vision|began|founded|artist|craft|crave-able|ultimate)\b", text) and not re.search(
        r"\$\d|\b\d{1,3}\.\d{2}\b",
        text,
    ):
        return True
    return False


def looks_like_meal_deal_or_promo(name: str, description: str | None = None) -> bool:
    normalized_name = name.lower()
    if re.search(r"\b(meals?|combos?|bundles?|deals?|offers?|vouchers?)\b", normalized_name):
        return True
    text = f"{name} {description or ''}".lower()
    tokens = set(re.split(r"[^a-z0-9]+", text))
    has_price = bool(re.search(r"\$\d|\b\d{1,3}\.\d{2}\b", text))
    has_food_noun = has_menu_item_noun(text)

    if re.fullmatch(r"\d+\s+(for|fo)\s+\w+", normalized_name):
        return True
    if re.search(r"\b\d+\s+for\s+\w+\b", normalized_name) and not has_food_noun:
        return True
    if any(word in tokens for word in PROMO_OR_DEAL_WORDS) and re.search(
        r"\b(pick|choose|select|get|starting at|best value|beverage|starter|main)\b",
        text,
    ):
        return True
    if has_price and re.search(r"\b(starting at|value meal|pick your|beverage,?\s+starter|main)\b", text) and not has_food_noun:
        return True
    return False


def looks_like_add_on_modifier_row(name: str, description: str | None = None) -> bool:
    normalized_name = name.lower().strip()
    text = f"{name} {description or ''}".lower()
    name_tokens = [term for term in re.split(r"[^a-z0-9-]+", normalized_name) if term]
    text_tokens = set(re.split(r"[^a-z0-9-]+", text))

    if normalized_name in ADD_ON_ONLY_WORDS:
        return True
    if len(name_tokens) <= 2 and any(token in ADD_ON_ONLY_WORDS for token in name_tokens):
        return True
    if re.fullmatch(r"(add|extra|substitute|protein)s?(\s+(on|ons|in|with|for))?", normalized_name):
        return True
    if any(token in ADD_ON_ONLY_WORDS for token in text_tokens) and re.search(
        r"\b(chicken|shrimp|steak|salmon|tofu|egg|avocado|cheese|bacon)\s+\d{1,2}\b",
        text,
    ):
        return True
    return False


def looks_like_preparation_phrase_without_dish(name: str, description: str | None = None) -> bool:
    normalized_name = name.lower()
    text = f"{name} {description or ''}".lower()
    tokens = [term for term in re.split(r"[^a-z0-9]+", normalized_name) if term]
    if not tokens:
        return False
    prep_token_count = sum(1 for token in tokens if token in PREPARATION_ONLY_WORDS or token in {"or", "and"})
    if prep_token_count >= len(tokens) - 1 and not has_menu_item_noun(text):
        return True
    if re.fullmatch(r"(sauced|fried|grilled|roasted|steamed|crispy)(,\s*|\s+or\s+|\s+and\s+).+", normalized_name):
        return not has_menu_item_noun(text)
    return False


def is_beverage_only(name: str, description: str | None = None) -> bool:
    text = f"{name} {description or ''}".lower()
    tokens = set(re.split(r"[^a-z0-9]+", text))
    has_beverage = any(word in tokens for word in BEVERAGE_ONLY_WORDS)
    if not has_beverage:
        return False
    food_without_beverage = re.sub(
        r"\b(beer|wine|cocktails?|drinks?|drink|soda|coffee|tea|spezi|cola|lemonade|espresso|latte|cappuccino|lager|ale|ipa|pilsner|soft)\b",
        " ",
        text,
    )
    return not looks_like_food_text(food_without_beverage)


def looks_like_schedule_or_event_text(name: str, description: str | None = None) -> bool:
    text = f"{name} {description or ''}".lower()
    tokens = set(re.split(r"[^a-z0-9]+", text))
    time_pattern = re.search(r"\b\d{1,2}\s*(?::\d{2})?\s*(am|pm)\b|\b\d{1,2}\s*-\s*\d{1,2}\b", text)
    weekday_count = sum(1 for word in SCHEDULE_WORDS if word in tokens)
    if time_pattern and weekday_count > 0:
        return True
    if weekday_count >= 2 and not looks_like_food_text(text):
        return True
    if re.search(r"\b(open|closed|hours?|calendar|events?)\b", text) and time_pattern:
        return True
    return False


def looks_like_food_text(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"rice|bowl|noodle|noodles|pasta|sauce|sandwich|salad|roll|taco|burger|pizza|"
            r"chicken|beef|pork|fish|salmon|tuna|shrimp|crab|tofu|egg|cheese|cream|"
            r"sesame|peanut|soy|bread|flour|vegetable|tomato|greens|beans|soup|"
            r"cake|dessert|cookie|fries|fried|grilled|roasted|steamed|spicy|"
            r"dumpling|curry|kebab|falafel|hummus|gyro|steak|rib|wings|sausage"
            r")\b",
            text,
        )
    )


def has_menu_item_noun(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"bowl|noodle|noodles|pasta|sandwich|salad|roll|taco|burger|pizza|"
            r"chicken|beef|pork|fish|salmon|tuna|shrimp|crab|tofu|egg|eggs|cheese|"
            r"vegetable|tomato|greens|beans|soup|cake|dessert|cookie|cookies|fries|"
            r"dumpling|curry|kebab|falafel|hummus|gyro|steak|rib|ribs|wings|sausage|"
            r"burrito|quesadilla|nachos|toast|omelet|omelette|pancake|waffle|rice"
            r")\b",
            text,
        )
    )


def flatten_json_ld(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [node for entry in value for node in flatten_json_ld(entry)]
    if isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            return [value, *graph]
        return [value]
    return []


def flatten_candidates(values: Iterable[Any]) -> list[Any]:
    output: list[Any] = []
    for value in values:
        if isinstance(value, list):
            output.extend(value)
        elif value is not None:
            output.append(value)
    return output


def schema_types(value: dict[str, Any]) -> str:
    raw_type = value.get("@type", "")
    if isinstance(raw_type, list):
        return " ".join(str(item).lower() for item in raw_type)
    return str(raw_type).lower()


def first_tag_text(block: str, tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        match = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", block, flags=re.IGNORECASE)
        if match:
            value = clean_text(html_to_text(match.group(1)))
            if value:
                return value
    return None


def html_to_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</(?:p|div|li|article|section|h2|h3|h4)>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [clean_text(line) for line in html.unescape(value).splitlines()]
    return "\n".join(line for line in lines if line)


def clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = html.unescape(re.sub(r"\s+", " ", value)).strip()
    if not text or is_prompt_injection(text):
        return None
    return text


def dedupe_items(items: Iterable[MenuItem]) -> list[MenuItem]:
    seen: set[str] = set()
    output: list[MenuItem] = []
    for item in items:
        key = item.name.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def summarize_menu_text(sections: list[MenuSection]) -> str:
    lines = []
    for section in sections:
        for item in section.items[:20]:
            line = f"{item.name} - {item.description}" if item.description else item.name
            lines.append(line)
    return "\n".join(lines[:80])


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if not re.match(r"^https?://", candidate, flags=re.IGNORECASE):
        candidate = f"https://{candidate}"
    try:
        parsed = parse.urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return parse.urlunparse(parsed)


def absolute_url(candidate: str, base_url: str) -> str | None:
    try:
        return parse.urljoin(base_url, candidate)
    except ValueError:
        return None
