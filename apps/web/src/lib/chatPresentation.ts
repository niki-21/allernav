import type { NearbySuggestionResponse } from "./types";

export interface ChatSource { label: string; url: string; evidenceId?: string; }

export function menuSources(response: NearbySuggestionResponse): ChatSource[] {
  const sources: ChatSource[] = [];
  const add = (label: string, value?: string | null, evidenceId?: string) => {
    if (!value) return;
    try {
      const url = new URL(value);
      if (!["https:", "http:"].includes(url.protocol)) return;
      sources.push({ label, url: url.href, evidenceId });
    } catch { /* Missing or invalid provenance must not become a link. */ }
  };
  response.evidence.slice(0, 8).forEach((item, i) => add(`${item.restaurant_name || "Restaurant"} menu`, item.source_url, `E${i + 1}`));
  response.places.forEach((item) => item.menu_dishes?.forEach((dish) => add(`${item.place.name} menu`, dish.source_url)));
  return sources;
}

export function readableAnswer(content: string): string {
  return content.replace(/\s*\[E\d+\]/gi, "");
}
