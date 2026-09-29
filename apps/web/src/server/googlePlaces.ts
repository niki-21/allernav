import type { LatLng, PlaceSummary } from "../lib/types.ts";

const GOOGLE_PLACES_BASE_URL = "https://places.googleapis.com/v1";

export class GooglePlacesError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GooglePlacesError";
  }
}

interface CacheEntry {
  payload: GooglePlaceDetails;
  expiresAt: number;
}

interface GoogleLocalizedText {
  text?: string;
}

interface GoogleAuthorAttribution {
  displayName?: string;
}

interface GooglePlacesApiReview {
  name?: string;
  authorAttribution?: GoogleAuthorAttribution;
  rating?: number;
  originalText?: GoogleLocalizedText;
  text?: GoogleLocalizedText;
  publishTime?: string;
  relativePublishTimeDescription?: string;
}

interface GooglePlacesApiPhoto {
  name?: string;
  widthPx?: number;
  heightPx?: number;
  authorAttributions?: GoogleAuthorAttribution[];
}

interface GooglePlacesApiPlace {
  id?: string;
  displayName?: GoogleLocalizedText;
  formattedAddress?: string;
  location?: {
    latitude?: number;
    longitude?: number;
  };
  rating?: number;
  userRatingCount?: number;
  primaryType?: string;
  websiteUri?: string;
  editorialSummary?: GoogleLocalizedText;
  googleMapsUri?: string;
  nationalPhoneNumber?: string;
  internationalPhoneNumber?: string;
  priceLevel?: string;
  priceRange?: {
    startPrice?: GoogleMoney;
    endPrice?: GoogleMoney;
  };
  regularOpeningHours?: GoogleOpeningHours;
  currentOpeningHours?: GoogleOpeningHours;
  takeout?: boolean;
  delivery?: boolean;
  dineIn?: boolean;
  reservable?: boolean;
  servesBreakfast?: boolean;
  servesBrunch?: boolean;
  servesLunch?: boolean;
  servesDinner?: boolean;
  servesVegetarianFood?: boolean;
  reviews?: GooglePlacesApiReview[];
  photos?: GooglePlacesApiPhoto[];
}

interface GoogleMoney {
  currencyCode?: string;
  units?: string | number;
  nanos?: number;
}

interface GoogleOpeningHours {
  openNow?: boolean;
  weekdayDescriptions?: string[];
}

export interface GooglePlaceReview {
  review_id: string;
  author_name?: string | null;
  rating?: number | null;
  text: string;
  publish_time?: string | null;
  relative_publish_time?: string | null;
}

export interface GooglePlacePhoto {
  name: string;
  width_px?: number | null;
  height_px?: number | null;
  author_names: string[];
}

export interface GooglePlaceDetails extends PlaceSummary {
  website_uri?: string | null;
  editorial_summary?: string | null;
  google_maps_uri?: string | null;
  national_phone_number?: string | null;
  international_phone_number?: string | null;
  price_level?: string | null;
  price_range?: string | null;
  regular_opening_hours?: GoogleOpeningHours | null;
  current_opening_hours?: GoogleOpeningHours | null;
  service_options?: Record<string, boolean | null | undefined>;
  reviews: GooglePlaceReview[];
  photos: GooglePlacePhoto[];
}

const placeCache = new Map<string, CacheEntry>();

function getApiKey(): string {
  const apiKey =
    process.env.GOOGLE_MAPS_API_KEY?.trim() ||
    process.env.GOOGLE_PLACES_API_KEY?.trim() ||
    process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY?.trim();

  if (!apiKey) {
    throw new GooglePlacesError(
      "Missing Google Places API key. Set GOOGLE_MAPS_API_KEY and NEXT_PUBLIC_GOOGLE_MAPS_API_KEY.",
    );
  }

  return apiKey;
}

async function requestJson<T>(
  endpoint: string,
  options: {
    method: "GET" | "POST";
    fieldMask: string;
    body?: Record<string, unknown>;
  },
): Promise<T> {
  const response = await fetch(`${GOOGLE_PLACES_BASE_URL}${endpoint}`, {
    method: options.method,
    headers: {
      "Content-Type": "application/json",
      "X-Goog-Api-Key": getApiKey(),
      "X-Goog-FieldMask": options.fieldMask,
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
    cache: "no-store",
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new GooglePlacesError(
      `Google Places request failed with ${response.status}: ${detail || response.statusText}`,
    );
  }

  return (await response.json()) as T;
}

function parsePlaceSummary(place: GooglePlacesApiPlace): PlaceSummary {
  const location = place.location ?? {};

  return {
    id: place.id ?? "",
    name: place.displayName?.text ?? "Unknown place",
    address: place.formattedAddress ?? null,
    location: {
      lat: location.latitude ?? 0,
      lng: location.longitude ?? 0,
    },
    rating: place.rating ?? null,
    user_rating_count: place.userRatingCount ?? null,
    primary_type: place.primaryType ?? null,
    website_url: place.websiteUri ?? null,
  };
}

function parsePlaceDetails(place: GooglePlacesApiPlace): GooglePlaceDetails {
  const location = place.location ?? {};
  const reviews = Array.isArray(place.reviews) ? place.reviews : [];
  const photos = Array.isArray(place.photos) ? place.photos : [];

  return {
    id: place.id ?? "",
    name: place.displayName?.text ?? "Unknown place",
    address: place.formattedAddress ?? null,
    location: {
      lat: location.latitude ?? 0,
      lng: location.longitude ?? 0,
    },
    rating: place.rating ?? null,
    user_rating_count: place.userRatingCount ?? null,
    primary_type: place.primaryType ?? null,
    website_uri: place.websiteUri ?? null,
    editorial_summary: place.editorialSummary?.text ?? null,
    google_maps_uri: place.googleMapsUri ?? null,
    national_phone_number: place.nationalPhoneNumber ?? null,
    international_phone_number: place.internationalPhoneNumber ?? null,
    price_level: place.priceLevel ?? null,
    price_range: formatPriceRange(place.priceRange),
    regular_opening_hours: place.regularOpeningHours ?? null,
    current_opening_hours: place.currentOpeningHours ?? null,
    service_options: {
      takeout: place.takeout,
      delivery: place.delivery,
      dine_in: place.dineIn,
      reservable: place.reservable,
      serves_breakfast: place.servesBreakfast,
      serves_brunch: place.servesBrunch,
      serves_lunch: place.servesLunch,
      serves_dinner: place.servesDinner,
      serves_vegetarian_food: place.servesVegetarianFood,
    },
    photos: photos
      .filter((photo) => typeof photo.name === "string")
      .slice(0, 6)
      .map((photo) => ({
        name: photo.name ?? "",
        width_px: photo.widthPx ?? null,
        height_px: photo.heightPx ?? null,
        author_names: Array.isArray(photo.authorAttributions)
          ? photo.authorAttributions
              .map((author) => author.displayName)
              .filter((name: unknown): name is string => typeof name === "string" && name.trim().length > 0)
          : [],
      })),
    reviews: reviews.map((review, index) => {
      const textPayload = review.originalText ?? review.text ?? {};

      return {
        review_id: review.name ?? `review-${index}`,
        author_name: review.authorAttribution?.displayName ?? null,
        rating: review.rating ?? null,
        text: textPayload.text ?? "",
        publish_time: review.publishTime ?? null,
        relative_publish_time: review.relativePublishTimeDescription ?? null,
      };
    }),
  };
}

function formatMoney(value: GoogleMoney | undefined): string | null {
  if (!value || typeof value.units === "undefined") {
    return null;
  }
  const prefix = value.currencyCode === "USD" ? "$" : value.currencyCode ? `${value.currencyCode} ` : "";
  return `${prefix}${value.units}`;
}

function formatPriceRange(value: GooglePlacesApiPlace["priceRange"]): string | null {
  const start = formatMoney(value?.startPrice);
  const end = formatMoney(value?.endPrice);
  if (start && end) {
    return `${start}-${end}`;
  }
  return start ?? end;
}

export class GooglePlacesClient {
  async searchPlaces(query: string, center: LatLng, maxResults = 12): Promise<PlaceSummary[]> {
    const usesTextSearch = query.trim().length > 0;
    const endpoint = usesTextSearch ? "/places:searchText" : "/places:searchNearby";
    const fieldMask = [
      "places.id",
      "places.displayName",
      "places.location",
      "places.formattedAddress",
      "places.rating",
      "places.userRatingCount",
      "places.primaryType",
      "places.websiteUri",
    ].join(",");

    const payload = await requestJson<{ places?: GooglePlacesApiPlace[] }>(endpoint, {
      method: "POST",
      fieldMask,
      body: usesTextSearch
        ? {
            textQuery: query.trim(),
            pageSize: maxResults,
            includedType: "restaurant",
            strictTypeFiltering: false,
            locationBias: {
              circle: {
                center: {
                  latitude: center.lat,
                  longitude: center.lng,
                },
                radius: 5000,
              },
            },
          }
        : {
            includedTypes: ["restaurant"],
            maxResultCount: maxResults,
            locationRestriction: {
              circle: {
                center: {
                  latitude: center.lat,
                  longitude: center.lng,
                },
                radius: 5000,
              },
            },
          },
    });

    return (payload.places ?? [])
      .filter((place) => place.id && place.location)
      .map((place) => parsePlaceSummary(place));
  }

  async getPlaceDetails(placeId: string): Promise<GooglePlaceDetails> {
    const cached = placeCache.get(placeId);
    const now = Date.now();

    if (cached && cached.expiresAt > now) {
      return cached.payload;
    }

    const payload = await requestJson<GooglePlacesApiPlace>(`/places/${encodeURIComponent(placeId)}`, {
      method: "GET",
      fieldMask: [
        "id",
        "displayName",
        "formattedAddress",
        "location",
        "rating",
        "userRatingCount",
        "websiteUri",
        "primaryType",
        "editorialSummary",
        "googleMapsUri",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "priceLevel",
        "priceRange",
        "regularOpeningHours",
        "currentOpeningHours",
        "takeout",
        "delivery",
        "dineIn",
        "reservable",
        "servesBreakfast",
        "servesBrunch",
        "servesLunch",
        "servesDinner",
        "servesVegetarianFood",
        "reviews",
        "photos",
      ].join(","),
    });

    const normalized = parsePlaceDetails(payload);
    placeCache.set(placeId, {
      payload: normalized,
      expiresAt: now + 10 * 60 * 1000,
    });

    return normalized;
  }
}
