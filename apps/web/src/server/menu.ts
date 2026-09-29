import { isIndividualFoodItem } from "../lib/menuContent.ts";
import type { MenuItem, MenuSection, PlaceMenu } from "../lib/types.ts";

const menuCache = new Map<string, PlaceMenu | null>();
const MENU_NAVIGATION_WORDS = [
  "home",
  "hours",
  "reservations",
  "reservation",
  "order",
  "locations",
  "location",
  "directions",
  "about",
  "contact",
  "careers",
  "open",
  "skip",
];

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function toAbsoluteUrl(candidate: string, baseUrl: string): string | null {
  try {
    return new URL(candidate, baseUrl).toString();
  } catch {
    return null;
  }
}

function uniqueByName(items: MenuItem[]): MenuItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = item.name.toLowerCase();
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function extractString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? normalizeWhitespace(value) : null;
}

function looksLikeRealMenuItem(name: string, description?: string | null): boolean {
  const normalizedName = name.toLowerCase();
  const terms = normalizedName.split(/[^a-z0-9]+/).filter(Boolean);
  const blockedMatches = MENU_NAVIGATION_WORDS.filter((word) => normalizedName.includes(word));

  if (name.length < 3 || name.length > 60) {
    return false;
  }

  if (blockedMatches.length >= 2) {
    return false;
  }

  if (terms.length <= 1 && !description) {
    return false;
  }

  if (/menu|restaurant college park|college park now|home menu/i.test(name)) {
    return false;
  }

  return true;
}

function sanitizeSections(sections: MenuSection[]): MenuSection[] {
  return sections
    .map((section) => ({
      ...section,
      items: uniqueByName(
        section.items.filter((item) => isIndividualFoodItem(item.name) && looksLikeRealMenuItem(item.name, item.description)).slice(0, 20),
      ),
    }))
    .filter((section) => section.items.length > 0);
}

function buildMenuItem(
  value: unknown,
  selectedTitle: string,
): { item: MenuItem | null; nestedSections: MenuSection[]; nextMenuUrl: string | null } {
  if (!value || typeof value !== "object") {
    return { item: null, nestedSections: [], nextMenuUrl: null };
  }

  const item = value as Record<string, unknown>;
  const type = extractString(item["@type"])?.toLowerCase() ?? "";
  const name = extractString(item.name);
  const description = extractString(item.description);
  const price =
    extractString(item.price) ||
    extractString((item.offers as Record<string, unknown> | undefined)?.price) ||
    extractString((item.offers as Record<string, unknown> | undefined)?.priceCurrency);

  const nestedSections = extractMenuSections(item, selectedTitle);
  const nextMenuUrl =
    extractString(item.url) && type.includes("menu") && !type.includes("menuitem") ? extractString(item.url) : null;

  if (type.includes("menuitem") && name) {
    return {
      item: {
        name,
        description,
        price,
        likely_safe_for: [],
        likely_risky_for: [],
      },
      nestedSections,
      nextMenuUrl,
    };
  }

  if (!type && name && description && selectedTitle) {
    return {
      item: {
        name,
        description,
        price,
        likely_safe_for: [],
        likely_risky_for: [],
      },
      nestedSections,
      nextMenuUrl,
    };
  }

  return { item: null, nestedSections, nextMenuUrl };
}

function extractMenuSections(value: unknown, fallbackTitle = "Menu"): MenuSection[] {
  if (!value) {
    return [];
  }

  if (Array.isArray(value)) {
    return value.flatMap((entry) => extractMenuSections(entry, fallbackTitle));
  }

  if (typeof value !== "object") {
    return [];
  }

  const item = value as Record<string, unknown>;
  const type = extractString(item["@type"])?.toLowerCase() ?? "";
  const title = extractString(item.name) || fallbackTitle;

  const candidates = [
    item.hasMenuSection,
    item.hasPart,
    item.mainEntity,
    item.menuSection,
    item.menu,
  ].filter(Boolean);

  const childSections = candidates.flatMap((candidate) => extractMenuSections(candidate, title));
  const childItems = candidates.flatMap((candidate) => {
    if (!Array.isArray(candidate)) {
      return [candidate];
    }
    return candidate;
  });

  const parsedItems = childItems
    .map((candidate) => buildMenuItem(candidate, title).item)
    .filter((candidate): candidate is MenuItem => candidate !== null);

  const nestedSections = childItems.flatMap((candidate) => buildMenuItem(candidate, title).nestedSections);

  if (type.includes("menusection") || (parsedItems.length > 0 && title)) {
    return [
      ...childSections,
      ...nestedSections,
      {
        title,
        items: uniqueByName(parsedItems),
      },
    ].filter((section) => section.items.length > 0);
  }

  return [...childSections, ...nestedSections];
}

function extractJsonLd(html: string): unknown[] {
  const scripts = [...html.matchAll(/<script[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)];
  const payloads: unknown[] = [];

  for (const script of scripts) {
    const raw = script[1]?.trim();
    if (!raw) {
      continue;
    }

    try {
      payloads.push(JSON.parse(raw));
    } catch {
      // Ignore invalid JSON-LD blocks.
    }
  }

  return payloads;
}

function extractCandidateMenuUrl(html: string, baseUrl: string): string | null {
  const anchor = [...html.matchAll(/<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)].find((match) =>
    /menu|order|food/i.test(`${match[1] ?? ""} ${match[2] ?? ""}`),
  );

  if (!anchor?.[1]) {
    return null;
  }

  return toAbsoluteUrl(anchor[1], baseUrl);
}

async function fetchHtml(url: string): Promise<string | null> {
  try {
    const response = await fetch(url, {
      headers: {
        "User-Agent": "AllernavMenuBot/1.0 (+https://allernav.local)",
        Accept: "text/html,application/xhtml+xml",
      },
      cache: "no-store",
    });

    if (!response.ok) {
      return null;
    }

    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.includes("text/html")) {
      return null;
    }

    return await response.text();
  } catch {
    return null;
  }
}

export async function fetchMenuSnapshot(websiteUrl: string | null | undefined): Promise<PlaceMenu | null> {
  const normalizedUrl = extractString(websiteUrl);
  if (!normalizedUrl) {
    return null;
  }

  if (menuCache.has(normalizedUrl)) {
    return menuCache.get(normalizedUrl) ?? null;
  }

  const homepageHtml = await fetchHtml(normalizedUrl);
  if (!homepageHtml) {
    menuCache.set(normalizedUrl, null);
    return null;
  }

  const structuredSections = sanitizeSections(
    extractJsonLd(homepageHtml)
    .flatMap((entry) => extractMenuSections(entry))
    .filter((section) => section.items.length > 0),
  );

  if (structuredSections.length > 0) {
    const menu = {
      source_url: normalizedUrl,
      sections: structuredSections,
    };
    menuCache.set(normalizedUrl, menu);
    return menu;
  }

  const menuUrl = extractCandidateMenuUrl(homepageHtml, normalizedUrl);
  if (menuUrl && menuUrl !== normalizedUrl) {
    const menuHtml = await fetchHtml(menuUrl);
    if (menuHtml) {
      const menuSections = sanitizeSections(
        extractJsonLd(menuHtml)
        .flatMap((entry) => extractMenuSections(entry))
        .filter((section) => section.items.length > 0),
      );

      if (menuSections.length > 0) {
        const menu = {
          source_url: menuUrl,
          sections: menuSections,
        };
        menuCache.set(normalizedUrl, menu);
        return menu;
      }
    }
  }

  menuCache.set(normalizedUrl, null);
  return null;
}
