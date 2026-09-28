import type { PlaceMenu } from "../lib/types.ts";

type BackendMenuItem = {
  name?: unknown;
  description?: unknown;
  price?: unknown;
  confirmed_allergens?: unknown;
  allergen_codes?: unknown;
  inferred_risks?: unknown;
  likely_safe_for?: unknown;
  likely_risky_for?: unknown;
  risk_label?: unknown;
  matched_allergens?: unknown;
  risk_reasons?: unknown;
  verification_question?: unknown;
  confidence?: unknown;
  source_page?: unknown;
  source_url?: unknown;
  ocr_confidence?: unknown;
};

type BackendMenuSection = {
  title?: unknown;
  items?: unknown;
};

type BackendPlaceMenu = {
  source_url?: unknown;
  source_fetched_at?: unknown;
  status?: unknown;
  content_type?: unknown;
  document_url?: unknown;
  document_urls?: unknown;
  menu_version?: unknown;
  extraction_method?: unknown;
  page_count?: unknown;
  extraction_confidence?: unknown;
  restaurant_fit_score?: unknown;
  restaurant_fit_label?: unknown;
  avoid_count?: unknown;
  needs_check_count?: unknown;
  possible_lower_risk_count?: unknown;
  insufficient_info_count?: unknown;
  sections?: unknown;
};

export interface RefreshMenuContext {
  restaurantName?: string | null;
  websiteUrl?: string | null;
}

function timeoutMs(envName: string, fallback: number): number {
  const parsed = Number(process.env[envName]);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(500, Math.min(30_000, Math.trunc(parsed)));
}

async function fetchWithTimeout(url: URL | string, init: RequestInit, timeout: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function cleanBaseUrl(value: string | undefined): string | null {
  if (!value?.trim()) {
    return null;
  }
  try {
    const url = new URL(value);
    return url.toString().replace(/\/$/, "");
  } catch {
    return null;
  }
}

export function getFastApiBaseUrl(): string | null {
  return (
    cleanBaseUrl(process.env.FASTAPI_API_BASE_URL) ??
    cleanBaseUrl(process.env.NEXT_PUBLIC_FASTAPI_API_BASE_URL) ??
    cleanBaseUrl(process.env.NEXT_PUBLIC_API_BASE_URL)
  );
}

export function buildFastApiUrl(path: string): string | null {
  const baseUrl = getFastApiBaseUrl();
  if (!baseUrl) {
    return null;
  }
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function allergenArray(value: unknown): PlaceMenu["sections"][number]["items"][number]["likely_risky_for"] {
  return stringArray(value) as PlaceMenu["sections"][number]["items"][number]["likely_risky_for"];
}

function riskLabelValue(value: unknown): PlaceMenu["sections"][number]["items"][number]["risk_label"] {
  return value === "avoid" ||
    value === "needs_check" ||
    value === "possible_lower_risk" ||
    value === "insufficient_info"
    ? value
    : null;
}

export function normalizeBackendMenu(raw: unknown): PlaceMenu | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }

  const menu = raw as BackendPlaceMenu;
  const sections = Array.isArray(menu.sections)
    ? menu.sections
        .map((section): PlaceMenu["sections"][number] | null => {
          if (!section || typeof section !== "object") {
            return null;
          }
          const typedSection = section as BackendMenuSection;
          const title = stringValue(typedSection.title) ?? "Menu";
          const items = Array.isArray(typedSection.items)
            ? typedSection.items
                .map((item): PlaceMenu["sections"][number]["items"][number] | null => {
                  if (!item || typeof item !== "object") {
                    return null;
                  }
                  const typedItem = item as BackendMenuItem;
                  const name = stringValue(typedItem.name);
                  if (!name) {
                    return null;
                  }
                  return {
                    name,
                    description: stringValue(typedItem.description),
                    price: stringValue(typedItem.price),
                    confirmed_allergens: allergenArray(typedItem.confirmed_allergens),
                    allergen_codes: stringArray(typedItem.allergen_codes),
                    likely_safe_for: allergenArray(typedItem.likely_safe_for),
                    likely_risky_for: [
                      ...allergenArray(typedItem.likely_risky_for),
                      ...allergenArray(typedItem.confirmed_allergens),
                      ...allergenArray(typedItem.inferred_risks),
                      ...allergenArray(typedItem.matched_allergens),
                    ],
                    risk_label: riskLabelValue(typedItem.risk_label),
                    matched_allergens: allergenArray(typedItem.matched_allergens),
                    risk_reasons: stringArray(typedItem.risk_reasons),
                    verification_question: stringValue(typedItem.verification_question),
                    confidence: numberValue(typedItem.confidence),
                    source_page: numberValue(typedItem.source_page),
                    source_url: stringValue(typedItem.source_url),
                    ocr_confidence: numberValue(typedItem.ocr_confidence),
                  };
                })
                .filter((item): item is PlaceMenu["sections"][number]["items"][number] => item !== null)
            : [];
          return items.length > 0 ? { title, items } : null;
        })
        .filter((section): section is PlaceMenu["sections"][number] => section !== null)
    : [];

  if (sections.length === 0) {
    return null;
  }

  return {
    source_url: stringValue(menu.source_url),
    source_fetched_at: stringValue(menu.source_fetched_at),
    status: stringValue(menu.status),
    content_type: stringValue(menu.content_type),
    document_url: stringValue(menu.document_url),
    document_urls: stringArray(menu.document_urls),
    menu_version: stringValue(menu.menu_version),
    extraction_method: stringValue(menu.extraction_method),
    page_count: numberValue(menu.page_count),
    extraction_confidence: numberValue(menu.extraction_confidence),
    restaurant_fit_score: numberValue(menu.restaurant_fit_score),
    restaurant_fit_label: stringValue(menu.restaurant_fit_label),
    avoid_count: numberValue(menu.avoid_count) ?? undefined,
    needs_check_count: numberValue(menu.needs_check_count) ?? undefined,
    possible_lower_risk_count: numberValue(menu.possible_lower_risk_count) ?? undefined,
    insufficient_info_count: numberValue(menu.insufficient_info_count) ?? undefined,
    sections,
  };
}

export async function fetchBackendPlaceMenu(placeId: string, allergens: string[] = []): Promise<PlaceMenu | null> {
  const backendUrl = buildFastApiUrl(`/api/places/${encodeURIComponent(placeId)}/menu`);
  if (!backendUrl) {
    return null;
  }
  const url = new URL(backendUrl);
  allergens.forEach((allergen) => url.searchParams.append("allergens", allergen));

  try {
    const response = await fetchWithTimeout(url, { cache: "no-store" }, timeoutMs("FASTAPI_READ_TIMEOUT_MS", 8000));
    if (!response.ok) {
      return null;
    }
    return normalizeBackendMenu(await response.json());
  } catch {
    return null;
  }
}

export async function refreshBackendPlaceMenu(
  placeId: string,
  context: RefreshMenuContext = {},
): Promise<PlaceMenu | null> {
  const url = buildFastApiUrl(`/api/places/${encodeURIComponent(placeId)}/menu-refresh`);
  if (!url) {
    return null;
  }

  const refreshUrl = new URL(url);
  if (context.restaurantName) {
    refreshUrl.searchParams.set("restaurant_name", context.restaurantName);
  }
  if (context.websiteUrl) {
    refreshUrl.searchParams.set("website_url", context.websiteUrl);
  }

  try {
    const response = await fetchWithTimeout(
      refreshUrl,
      { method: "POST", cache: "no-store" },
      timeoutMs("FASTAPI_REFRESH_TIMEOUT_MS", 12_000),
    );
    if (!response.ok) {
      return null;
    }
    return await fetchBackendPlaceMenu(placeId);
  } catch {
    return null;
  }
}
