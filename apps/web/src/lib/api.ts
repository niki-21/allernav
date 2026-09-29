import type {
  AgentRecommendationResult,
  AllergyTag,
  AskRestaurantResponse,
  CommunityReview,
  ConversationTurn,
  LatLng,
  MenuRefreshJob,
  NearbySuggestionResponse,
  PlaceDetailsResponse,
  PlaceMenu,
  PlaceSummary,
  ReviewRefreshJob,
  SearchResponse,
} from "./types";

const API_PREFIX = (process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") || "") + "/api";

const MENU_SCAN_TIMEOUT_MESSAGE = "The scan took too long. Try again or open the restaurant panel.";
const NEARBY_RAG_TIMEOUT_MESSAGE =
  "This request took too long. Try a specific restaurant or scan the menu first.";

export function menuScanErrorMessage(error: unknown): string {
  const rawMessage = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  const normalized = rawMessage.toLowerCase();

  if (
    normalized.includes("abort") ||
    normalized.includes("timed out") ||
    normalized.includes("timeout") ||
    normalized.includes("signal")
  ) {
    return MENU_SCAN_TIMEOUT_MESSAGE;
  }

  if (rawMessage.trim().startsWith("{") || rawMessage.trim().startsWith("[")) {
    return MENU_SCAN_TIMEOUT_MESSAGE;
  }

  return rawMessage || "The menu scan could not finish. Try again or open the restaurant panel.";
}

export function nearbyRagErrorMessage(error: unknown): string {
  const rawMessage = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  const normalized = rawMessage.toLowerCase();
  if (
    normalized.includes("abort") ||
    normalized.includes("timed out") ||
    normalized.includes("timeout") ||
    normalized.includes("signal")
  ) {
    return NEARBY_RAG_TIMEOUT_MESSAGE;
  }
  if (rawMessage.trim().startsWith("{") || rawMessage.trim().startsWith("[")) {
    return "Nearby suggestions are temporarily unavailable. Try again shortly.";
  }
  return rawMessage || "Nearby suggestions are temporarily unavailable. Try again shortly.";
}

export function buildSearchPayload(query: string, center: LatLng, allergens: AllergyTag[]) {
  return {
    query,
    center,
    allergens,
  };
}

export function buildMenuRefreshPayload(context: {
  placeName?: string | null;
  websiteUrl?: string | null;
  forceRefresh?: boolean;
}) {
  return {
    restaurant_name: context.placeName ?? null,
    website_url: context.websiteUrl ?? null,
    force_refresh: context.forceRefresh ?? false,
  };
}

// Search results may arrive before their place details. Resolve official sites
// before asking the backend to scan; a missing URL makes a candidate ineligible.
export async function resolveNearbyCandidateWebsites(
  places: PlaceSummary[],
  allergens: AllergyTag[],
): Promise<PlaceSummary[]> {
  return Promise.all(places.map(async (place) => {
    if (place.website_url) return place;
    try {
      const details = await fetchPlaceDetails(place.id, allergens, AbortSignal.timeout(15_000));
      return { ...place, name: details.name || place.name, website_url: details.website_uri };
    } catch {
      // One unavailable listing must not prevent other restaurants from scanning.
      return place;
    }
  }));
}

export function buildNearbySuggestionPayload(
  question: string,
  center: LatLng,
  allergens: AllergyTag[],
  candidatePlaces: PlaceSummary[],
  allowBackgroundScan = false,
  searchQuery = question,
  conversation: ConversationTurn[] = [],
) {
  const candidates = candidatePlaces.slice(0, 8);
  return {
    question,
    ...(conversation.length ? { conversation: conversation.slice(-10).map((turn) => ({ ...turn, content: turn.content.slice(0, 2000) })) } : {}),
    query: searchQuery,
    center,
    allergens,
    candidate_place_ids: candidates.map((place) => place.id),
    candidate_places: candidates,
    allow_background_scan: allowBackgroundScan,
    max_places: Math.min(8, Math.max(1, candidates.length || 6)),
    top_evidence: 3,
  };
}

export function buildPlaceDetailsUrl(placeId: string, allergens: AllergyTag[]): string {
  const params = new URLSearchParams();
  allergens.forEach((allergen) => params.append("allergens", allergen));
  const queryString = params.toString();
  return `${API_PREFIX}/places/${encodeURIComponent(placeId)}${queryString ? `?${queryString}` : ""}`;
}

export async function searchPlaces(query: string, center: LatLng, allergens: AllergyTag[]): Promise<SearchResponse> {
  const response = await fetch(`${API_PREFIX}/search`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildSearchPayload(query, center, allergens)),
  });

  if (!response.ok) {
    throw new Error(await response.text());
  }

  return (await response.json()) as SearchResponse;
}

export async function fetchPlaceDetails(placeId: string, allergens: AllergyTag[], signal?: AbortSignal): Promise<PlaceDetailsResponse> {
  const response = await fetch(buildPlaceDetailsUrl(placeId, allergens), { signal });
  if (!response.ok) {
    throw new Error(await response.text());
  }

  return (await response.json()) as PlaceDetailsResponse;
}

export async function fetchPlaceMenu(placeId: string, allergens: AllergyTag[]): Promise<PlaceMenu | null> {
  const params = new URLSearchParams();
  allergens.forEach((allergen) => params.append("allergens", allergen));
  const response = await fetch(`${API_PREFIX}/places/${encodeURIComponent(placeId)}/menu?${params.toString()}`, {
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`Menu refresh result failed with status ${response.status}.`);
  }
  const menu = (await response.json()) as PlaceMenu;
  return menu.sections.length > 0 ? menu : null;
}

export async function refreshPlaceMenu(
  placeId: string,
  context: { placeName?: string | null; websiteUrl?: string | null; forceRefresh?: boolean } = {},
): Promise<MenuRefreshJob> {
  const response = await fetch(`${API_PREFIX}/places/${encodeURIComponent(placeId)}/menu-refresh`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildMenuRefreshPayload(context)),
    signal: AbortSignal.timeout(55_000),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string; message?: string } | null;
    throw new Error(body?.message ?? body?.detail ?? `Menu refresh failed with status ${response.status}.`);
  }
  return (await response.json()) as MenuRefreshJob;
}

export async function fetchMenuRefreshJob(jobId: string): Promise<MenuRefreshJob> {
  const response = await fetch(`${API_PREFIX}/menu-refresh-jobs/${encodeURIComponent(jobId)}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) {
    throw new Error(`Menu refresh status failed with status ${response.status}.`);
  }
  return (await response.json()) as MenuRefreshJob;
}

export async function refreshPlaceReviews(placeId: string): Promise<ReviewRefreshJob> {
  const response = await fetch(`${API_PREFIX}/places/${encodeURIComponent(placeId)}/reviews-refresh`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as ReviewRefreshJob;
}

export async function fetchCommunityReviews(placeId: string): Promise<CommunityReview[]> {
  const response = await fetch(`${API_PREFIX}/places/${encodeURIComponent(placeId)}/community-reviews`, {
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error("Community reviews are temporarily unavailable.");
  }
  const body = (await response.json()) as { reviews?: CommunityReview[] };
  return Array.isArray(body.reviews) ? body.reviews : [];
}

export async function submitCommunityReview(
  placeId: string,
  payload: {
    author_name: string;
    body: string;
    rating?: number | null;
    allergens?: AllergyTag[];
  },
  accessToken: string,
): Promise<{ review: CommunityReview; points_awarded: number; total_points: number }> {
  const response = await fetch(`${API_PREFIX}/places/${encodeURIComponent(placeId)}/community-reviews`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { error?: string } | null;
    throw new Error(body?.error ?? "Review could not be saved.");
  }
  return (await response.json()) as { review: CommunityReview; points_awarded: number; total_points: number };
}

export async function askRestaurant(
  placeId: string,
  placeName: string,
  allergens: AllergyTag[],
): Promise<AskRestaurantResponse> {
  const response = await fetch(`${API_PREFIX}/places/${encodeURIComponent(placeId)}/ask`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      place_id: placeId,
      place_name: placeName,
      allergens,
    }),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as AskRestaurantResponse;
}

export async function askNearbyPlaces(
  question: string,
  center: LatLng,
  allergens: AllergyTag[],
  candidatePlaces: PlaceSummary[],
  allowBackgroundScan = false,
  searchQuery = question,
  conversation: ConversationTurn[] = [],
): Promise<NearbySuggestionResponse> {
  const response = await fetch(`${API_PREFIX}/rag/nearby-suggestions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(
      buildNearbySuggestionPayload(question, center, allergens, candidatePlaces, allowBackgroundScan, searchQuery, conversation),
    ),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string; message?: string } | null;
    throw new Error(body?.detail ?? body?.message ?? `Nearby suggestions failed with status ${response.status}.`);
  }
  return (await response.json()) as NearbySuggestionResponse;
}

export async function analyzeRestaurant(
  placeId: string,
  placeName: string,
  allergens: AllergyTag[],
  websiteUrl?: string | null,
): Promise<AgentRecommendationResult> {
  const response = await fetch(`${API_PREFIX}/analyze-restaurant`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      restaurant_id: placeId,
      restaurant_name: placeName,
      website_url: websiteUrl ?? null,
      profile: {
        allergens,
        sensitivity: "careful",
        prep_preference: "verify",
      },
    }),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as AgentRecommendationResult;
}
