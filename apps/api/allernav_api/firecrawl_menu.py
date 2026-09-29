"""Bounded, server-side Firecrawl collection with source-grounded menu extraction."""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from urllib import error, parse, request

from .menu_normalization import azure_openai_menu_extraction_configured, extract_english_menu_page
from .models import MenuSection, MenuSource, SourceType


class FirecrawlError(RuntimeError):
    pass


def firecrawl_configured() -> bool:
    return bool(os.getenv("FIRECRAWL_API_KEY", "").strip()) and os.getenv("FIRECRAWL_ENABLED", "true").lower() != "false"


def _post(endpoint: str, payload: dict, timeout: float) -> dict:
    req = request.Request(
        f"https://api.firecrawl.dev/v2/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        # Never include provider bodies or request headers in logs/job messages.
        raise FirecrawlError(f"Firecrawl returned HTTP {exc.code}.") from None
    except (error.URLError, TimeoutError, ValueError):
        raise FirecrawlError("Firecrawl was unavailable or returned an invalid response.") from None
    if not isinstance(result, dict) or not result.get("success"):
        raise FirecrawlError("Firecrawl did not complete the collection.")
    return result


def _same_site(url: str, root: str) -> bool:
    try:
        candidate, origin = parse.urlsplit(url), parse.urlsplit(root)
        return (
            candidate.scheme in {"https", "http"}
            and not candidate.username and not candidate.password
            and candidate.hostname is not None
            and candidate.hostname.removeprefix("www.") == (origin.hostname or "").removeprefix("www.")
        )
    except ValueError:
        return False


def _menu_candidate(url: str, root: str) -> bool:
    if not _same_site(url, root):
        return False
    path = parse.urlsplit(url).path.lower()
    root_path = parse.urlsplit(root).path.lower().rstrip("/")
    # Preserve a location prefix such as /dubai or /en/dubai on multi-city sites.
    scope = root_path.split("/menu")[0]
    if scope and "." not in scope and not path.startswith(scope + "/") and path != scope:
        return False
    return any(token in path for token in ("menu", "dining", "food")) and not any(
        token in path for token in ("/blog/", "/news/", "cocktail", "wine", "beverage", "shisha")
    )


def collect_firecrawl_menu(
    website_url: str, *, restaurant_id: str | None = None,
    max_pages: int = 4, budget_seconds: float = 100,
) -> MenuSource | None:
    if not firecrawl_configured() or not azure_openai_menu_extraction_configured():
        return None
    deadline = time.monotonic() + budget_seconds
    candidates = [website_url]
    try:
        mapped = _post("map", {"url": website_url, "search": "menu", "limit": 30, "includeSubdomains": False}, min(20, budget_seconds))
        links = mapped.get("links", [])
        menu_links = [entry.get("url", "") for entry in links if isinstance(entry, dict)]
        menu_links = [url for url in menu_links if _menu_candidate(url, website_url)]
        candidates = list(dict.fromkeys(candidates + menu_links))
    except FirecrawlError:
        pass  # A direct scrape can still succeed when mapping is unavailable.

    sections: list[MenuSection] = []
    seen: set[tuple[str, str]] = set()
    sources: list[str] = []
    texts: list[str] = []
    last_error: FirecrawlError | None = None
    page_limit = max(1, min(max_pages, 6))
    for page_index, url in enumerate(candidates):
        if page_index >= page_limit:
            break
        remaining = deadline - time.monotonic()
        if remaining < 2:
            break
        timeout = min(30, remaining)
        try:
            data = _post("scrape", {
                "url": url, "formats": ["markdown", "links"], "onlyMainContent": True,
                "waitFor": 2500,
                "timeout": int(max(1000, (timeout - 1) * 1000)), "maxAge": 86400000,
            }, timeout).get("data", {})
        except FirecrawlError as exc:
            last_error = exc
            continue
        if not isinstance(data, dict):
            continue
        markdown = data.get("markdown", "")
        if isinstance(markdown, str) and len(markdown) < 1500 and "loading" in markdown.lower() and deadline - time.monotonic() > 10:
            # Do not repeatedly parse a cached client-side loading screen.
            try:
                data = _post("scrape", {
                    "url": url, "formats": ["markdown", "links"], "onlyMainContent": True,
                    "maxAge": 0, "waitFor": 5000, "timeout": 25000,
                }, min(30, deadline - time.monotonic())).get("data", {})
            except FirecrawlError as exc:
                last_error = exc
                continue
            if not isinstance(data, dict):
                continue
        for link in data.get("links", []):
            if isinstance(link, str) and _menu_candidate(link, website_url) and link not in candidates:
                candidates.append(link)
        content = data.get("markdown")
        if not isinstance(content, str) or not content.strip():
            continue
        # Bound input/model calls; keep every dish linked to the page it came from.
        for offset in range(0, min(len(content), 48000), 12000):
            if time.monotonic() >= deadline:
                break
            chunk = content[offset:offset + 12000]
            extracted = extract_english_menu_page(
                ocr_text=chunk, source_url=url, source_page=len(sources) + 1,
                ocr_confidence=None, restaurant_id=restaurant_id, source_kind="website text",
            )
            for section in extracted:
                items = []
                for item in section.items:
                    key = (item.name.casefold(), (item.price or "").casefold())
                    if key not in seen:
                        seen.add(key)
                        items.append(item)
                if items:
                    sections.append(MenuSection(title=section.title, items=items))
        sources.append(url)
        texts.append(content[:48000])
        if len(seen) >= 15:
            break  # A usable menu is enough; avoid crawling unrelated seasonal menus.
    if not sections:
        if last_error:
            raise last_error
        return None
    # Merge repeated headings so downstream section sanitization does not drop pages.
    merged: dict[str, MenuSection] = {}
    for section in sections:
        key = section.title.casefold()
        if key in merged:
            merged[key].items.extend(section.items)
        else:
            merged[key] = section
    return MenuSource(
        source_type=SourceType.RESTAURANT_WEBSITE, source_url=website_url,
        source_timestamp=datetime.now(UTC).isoformat(), reliability=0.65,
        sections=list(merged.values()), raw_text="\n\n".join(texts),
        document_urls=sources, content_type="text/markdown", page_count=len(sources),
        extraction_method="firecrawl_azure_openai",
    )
