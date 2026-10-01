import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildMenuRefreshPayload,
  buildNearbySuggestionPayload,
  buildPlaceDetailsUrl,
  buildSearchPayload,
  menuScanErrorMessage,
  nearbyRagErrorMessage,
  resolveNearbyCandidateWebsites,
} from "../api.ts";
import { extractSearchIntent } from "../searchIntent.ts";

test("extractSearchIntent preserves French cuisine intent", () => {
  assert.equal(extractSearchIntent("I want a french restaurant", ""), "French restaurants");
});

test("extractSearchIntent preserves cheap burger intent", () => {
  assert.equal(extractSearchIntent("I want a cheap burger restaurant", ""), "cheap burger restaurants");
});

test("extractSearchIntent keeps cafe intent near a named location", () => {
  assert.equal(
    extractSearchIntent("We are going to Central Park, I want cute cafes", ""),
    "cute cafes near Central Park",
  );
});

test("extractSearchIntent supports food, dinner, sushi, and brunch phrasing", () => {
  assert.equal(extractSearchIntent("Find French food near me", ""), "French restaurants");
  assert.equal(extractSearchIntent("Italian dinner", ""), "Italian restaurants");
  assert.equal(extractSearchIntent("sushi", ""), "sushi restaurants");
  assert.equal(extractSearchIntent("brunch", ""), "brunch restaurants");
});

test("extractSearchIntent keeps an existing specific search query", () => {
  assert.equal(extractSearchIntent("Suggest somewhere nearby", "pizza restaurants"), "pizza restaurants");
});

test("buildSearchPayload keeps query, center, and allergens aligned", () => {
  assert.deepEqual(
    buildSearchPayload("ramen", { lat: 1, lng: 2 }, ["peanut", "soy"]),
    {
      query: "ramen",
      center: { lat: 1, lng: 2 },
      allergens: ["peanut", "soy"],
    },
  );
});

test("buildPlaceDetailsUrl appends repeated allergen params", () => {
  assert.equal(
    buildPlaceDetailsUrl("abc 123", ["peanut", "soy"]),
    "/api/places/abc%20123?allergens=peanut&allergens=soy",
  );
});

test("buildMenuRefreshPayload includes restaurant name and website url", () => {
  assert.deepEqual(
    buildMenuRefreshPayload({
      placeName: "Nami Nori Williamsburg",
      websiteUrl: "https://example.com/menu",
    }),
    {
      restaurant_name: "Nami Nori Williamsburg",
      website_url: "https://example.com/menu",
      force_refresh: false,
    },
  );
});

test("buildMenuRefreshPayload marks explicit refreshes", () => {
  assert.equal(
    buildMenuRefreshPayload({
      placeName: "Angel",
      websiteUrl: "https://example.com/menu",
      forceRefresh: true,
    }).force_refresh,
    true,
  );
});

test("buildNearbySuggestionPayload includes current map candidates", () => {
  const candidates = [
    { id: "place-a", name: "Alpha", location: { lat: 40, lng: -73 } },
    { id: "place-b", name: "Bravo", location: { lat: 40.1, lng: -73.1 } },
    { id: "place-c", name: "Charlie", location: { lat: 40.2, lng: -73.2 } },
  ];
  assert.deepEqual(
    buildNearbySuggestionPayload(
      "suggest dinner",
      { lat: 40, lng: -73 },
      ["sesame"],
      candidates,
    ),
    {
      question: "suggest dinner",
      query: "suggest dinner",
      center: { lat: 40, lng: -73 },
      allergens: ["sesame"],
      candidate_place_ids: ["place-a", "place-b", "place-c"],
      candidate_places: candidates,
      allow_background_scan: false,
      max_places: 3,
      top_evidence: 3,
    },
  );
});

test("buildNearbySuggestionPayload keeps visible candidates for cuisine prompts", () => {
  const candidates = [
    { id: "place-a", name: "Alpha", location: { lat: 40, lng: -73 } },
    { id: "place-b", name: "Bravo", location: { lat: 40.1, lng: -73.1 } },
  ];
  assert.deepEqual(
    buildNearbySuggestionPayload(
      "I want a french restaurant",
      { lat: 40, lng: -73 },
      ["sesame"],
      candidates,
      false,
      "French restaurants",
    ),
    {
      question: "I want a french restaurant",
      query: "French restaurants",
      center: { lat: 40, lng: -73 },
      allergens: ["sesame"],
      candidate_place_ids: ["place-a", "place-b"],
      candidate_places: candidates,
      allow_background_scan: false,
      max_places: 2,
      top_evidence: 3,
    },
  );
});

test("nearby payload opts into controlled background scans", () => {
  const candidates = [{ id: "place-a", name: "Alpha", location: { lat: 40, lng: -73 } }];
  const payload = buildNearbySuggestionPayload(
    "compare nearby places",
    { lat: 40, lng: -73 },
    ["sesame"],
    candidates,
    true,
  );
  assert.equal(payload.allow_background_scan, true);
});

test("TrustPanel does not duplicate agent dish evidence in the menu tab", () => {
  const source = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("Agent dish evidence"), false);
  assert.equal(source.includes("Dish evidence found by agent analysis"), false);
});

test("chat excludes duplicate restaurant cards and diagnostic details", () => {
  const trustPanelSource = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");
  const pageSource = readFileSync(new URL("../../app/page.tsx", import.meta.url), "utf8");
  assert.equal(trustPanelSource.includes("confidenceText"), false);
  assert.equal(pageSource.includes("suggestion.restaurant_fit_score}/100"), false);
  assert.equal(pageSource.includes("hasScannedMenuEvidence(suggestion)"), false);
  assert.equal(pageSource.includes("nearbyBucketSummary(suggestion)"), false);
  assert.equal(pageSource.includes("Next: {suggestion.next_action}"), false);
  assert.equal(pageSource.includes("Ask staff about sauces, broths, and shared prep before ordering."), false);
  assert.equal(pageSource.includes("<summary>Evidence details</summary>"), false);
  assert.equal(pageSource.includes("<summary>Staff questions</summary>"), false);
});

test("Agentic RAG hides unscanned allergy scores and polls started scans", () => {
  const source = readFileSync(new URL("../../app/page.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("Scan priority #"), false);
  assert.equal(source.includes('scan_running: "Scanning menu…"'), true);
  assert.equal(source.includes("fetchMenuRefreshJob(suggestion.scan_job_id as string)"), true);
  assert.equal(source.includes("fetchPlaceMenu(job.place_id, selectedAllergens)"), true);
  assert.equal(source.includes("setNearbyAnswer(reranked)"), true);
});

test("Agentic RAG resets when the active search context changes", () => {
  const source = readFileSync(new URL("../../app/page.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("const nearbyContextKey = useMemo"), true);
  assert.equal(source.includes("nearbyContextRef.current !== nearbyContextKey"), true);
  assert.equal(source.includes("setNearbyAnswer(null)"), true);
  assert.equal(source.includes('setNearbyAskError("Search this area first.")'), false);
});

test("Ask AllerNav searches the current map area before requesting RAG candidates", () => {
  const source = readFileSync(new URL("../../app/page.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("const intentQuery = extractSearchIntent(question, query)"), true);
  assert.equal(source.includes("visiblePlaces = await runSearch(intentQuery, mapCenter, selectedAllergens)"), true);
  assert.equal(source.includes('setNearbyAskError("No restaurants were found in this area.'), true);
  assert.equal(source.includes('"Ready to search this area"'), false);
  assert.equal(source.includes('role="status">Checking your question'), true);
});

test("no-allergy UI uses general discovery and hides allergy scoring", () => {
  const pageSource = readFileSync(new URL("../../app/page.tsx", import.meta.url), "utf8");
  const panelSource = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");
  assert.equal(pageSource.includes('nearbyAnswer.ranking_mode === "general_discovery"'), false);
  assert.equal(pageSource.includes("nearby-rating-badge"), false);
  assert.equal(panelSource.includes("const allergyMode = data.selected_allergens.length > 0"), true);
  assert.equal(panelSource.includes("menuSections.length > 0 && allergyMode"), true);
  assert.equal(panelSource.includes("!allergyMode && data.rating != null"), true);
});

test("place cards leave the restaurant score to the details header", () => {
  const source = readFileSync(new URL("../../components/PlaceCard.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("restaurant_fit_score"), false);
});

test("Menu tab leads with the fit score and possible lower-risk section", () => {
  const source = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");
  const possibleIndex = source.indexOf('title: "Possible lower-risk items to ask about"');
  const checkIndex = source.indexOf('title: "Needs staff check"');
  const avoidIndex = source.indexOf('title: "Avoid for your allergies"');
  const insufficientIndex = source.indexOf('title: "Insufficient info"');

  assert.ok(source.includes("Restaurant allergy fit"));
  assert.ok(source.includes("data.menu?.restaurant_fit_score ?? data.restaurant_fit_score ?? null"));
  assert.equal(source.includes("data.restaurant_fit_score ?? data.menu?.restaurant_fit_score ?? 20"), false);
  assert.ok(possibleIndex < checkIndex && checkIndex < avoidIndex && avoidIndex < insufficientIndex);
  assert.equal(source.includes('<details className="menu-trace">'), false);
  assert.equal(source.includes('<details className="menu-trace" open>'), false);
  const mainMenuStart = source.indexOf('{activeTab === "menu"');
  const technicalTraceStart = source.indexOf('<details className="menu-trace">', mainMenuStart);
  const mainMenuView = source.slice(mainMenuStart, technicalTraceStart);
  assert.equal(mainMenuView.includes("ragStatus"), false);
  assert.equal(mainMenuView.includes("ocrStatus"), false);
  assert.equal(mainMenuView.includes('className="menu-source-row"'), false);
});

test("TrustPanel keeps Overview and Menu restaurant fit messaging consistent", () => {
  const source = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");
  assert.ok(source.includes("restaurant-fit-badge"));
  assert.ok(source.includes("(restaurantFitScore ?? 0) >= 70"));
  assert.ok(source.includes("(restaurantFitScore ?? 0) >= 45"));
  assert.ok(source.includes('menuItemCount > 0 && agentRecommendation && agentRecommendation.overall_risk !== "insufficient_evidence"'));
  assert.ok(source.includes("<strong>Menu evidence fit</strong>"));
  assert.ok(source.includes("This is not a safety guarantee."));
  assert.ok(source.includes("Some dishes contain your allergens, but many menu items may be possible lower-risk after staff verification."));
});

test("Dubai-first and community review UI are wired", () => {
  const pageSource = readFileSync(new URL("../../app/page.tsx", import.meta.url), "utf8");
  const mapSource = readFileSync(new URL("../../components/Map.tsx", import.meta.url), "utf8");
  const panelSource = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");

  assert.ok(pageSource.includes("lat: 25.2048"));
  assert.ok(pageSource.includes("lng: 55.2708"));
  assert.ok(mapSource.includes("lat: 25.2048"));
  assert.ok(panelSource.includes('type PlaceTab = "summary" | "menu" | "community"'));
  assert.equal(panelSource.includes('"overview", "menu", "reviews", "about"'), false);
  assert.ok(panelSource.includes("displayRestaurantFitLabel"));
  assert.ok(panelSource.includes("Google reviews remain read-only discovery context."));
  assert.ok(panelSource.includes("submitCommunityReview"));
});

test("Google login gates review points while public browsing remains available", () => {
  const pageSource = readFileSync(new URL("../../app/page.tsx", import.meta.url), "utf8");
  const panelSource = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");
  const authSource = readFileSync(new URL("../../components/AuthProvider.tsx", import.meta.url), "utf8");

  assert.ok(pageSource.includes("<AuthBar />"));
  assert.equal(pageSource.includes("selectedAllergenSummary"), false);
  assert.ok(authSource.includes('provider: "google"'));
  assert.ok(panelSource.includes("Sign in with Google above the allergy filters"));
  assert.equal(panelSource.includes("allernav_reviewer_id"), false);
  assert.equal(panelSource.includes("demo point"), false);
});

test("menu and community views remove repeated cautions", () => {
  const source = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");
  assert.ok(source.includes("Menu labels use available text only."));
  assert.equal(source.includes('metadata: "no selected allergen detected · verify prep"'), false);
  assert.equal(source.includes('metadata: "preparation needs staff review"'), false);
  assert.equal(source.includes("Returned review sample"), false);
  assert.ok(source.includes("No allergy-specific mentions found in the available Google review sample."));
  assert.ok(source.includes("isMenuDisplayArtifact"));
  assert.ok(source.includes("allergens?$/i.test"));
});

test("TrustPanel exposes the fast and deep menu scan lifecycle", () => {
  const source = readFileSync(new URL("../../components/TrustPanel.tsx", import.meta.url), "utf8");
  assert.ok(source.includes('"Menu found · deeper scan running"'));
  assert.ok(source.includes('"Menu found · RAG index ready"'));
  assert.ok(source.includes("Retry menu scan"));
  assert.ok(source.includes("menuRefreshJob?.message"));
});

test("menu refresh proxy preserves backend diagnostics", () => {
  const source = readFileSync(
    new URL("../../app/api/places/[placeId]/menu-refresh/route.ts", import.meta.url),
    "utf8",
  );
  assert.equal(source.includes("FastAPI menu ingestion failed"), false);
  assert.ok(source.includes('id: "menu_ingestion_error"'));
  assert.ok(source.includes('console.info("[menu-refresh] request"'));
  assert.ok(source.includes("return NextResponse.json(body, { status: response.status })"));
});

test("Agentic RAG hides technical trace", () => {
  const source = readFileSync(new URL("../../app/page.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("<summary>Technical trace</summary>"), false);
  assert.equal(source.includes('<details className="nearby-rag-details" open>'), false);
});

test("menuScanErrorMessage converts abort and timeout errors to user-facing copy", () => {
  assert.equal(
    menuScanErrorMessage(new DOMException("The operation was aborted due to timeout", "TimeoutError")),
    "The scan took too long. Try again or open the restaurant panel.",
  );
  assert.equal(
    menuScanErrorMessage('{"detail":"The signal timed out"}'),
    "The scan took too long. Try again or open the restaurant panel.",
  );
});

test("menuScanErrorMessage hides malformed JSON responses", () => {
  assert.equal(
    menuScanErrorMessage("{invalid-json"),
    "The scan took too long. Try again or open the restaurant panel.",
  );
});

test("nearbyRagErrorMessage converts aborts and raw responses to product copy", () => {
  assert.equal(
    nearbyRagErrorMessage(new DOMException("This operation was aborted", "AbortError")),
    "This request took too long. Try a specific restaurant or scan the menu first.",
  );
  assert.equal(
    nearbyRagErrorMessage('{"detail":"upstream failed"}'),
    "Nearby suggestions are temporarily unavailable. Try again shortly.",
  );
});


test("nearby scans resolve official websites before details have hydrated", async (t) => {
  const requests: string[] = [];
  t.mock.method(globalThis, "fetch", async (input: string) => {
    requests.push(input);
    return Response.json({ id: "pending", name: "Restaurant", website_uri: "https://menu.example" });
  });
  const candidates = await resolveNearbyCandidateWebsites([
    { id: "pending", name: "Restaurant", location: { lat: 0, lng: 0 } },
    { id: "known", name: "Known", location: { lat: 0, lng: 0 }, website_url: "https://known.example" },
  ], ["fish"]);
  assert.equal(requests.length, 1);
  assert.match(requests[0], /pending/);
  assert.equal(candidates[0].website_url, "https://menu.example");
  assert.equal(candidates[1].website_url, "https://known.example");
  assert.equal(buildNearbySuggestionPayload("Scan menus", { lat: 0, lng: 0 }, ["fish"], candidates, true)
    .candidate_places[0].website_url, "https://menu.example");
});

test("one failed website lookup does not prevent other nearby menus from scanning", async (t) => {
  t.mock.method(globalThis, "fetch", async (input: string) => {
    if (input.includes("unavailable")) throw new Error("Unavailable");
    return Response.json({ name: "Available", website_uri: "https://available.example" });
  });
  const candidates = await resolveNearbyCandidateWebsites([
    { id: "unavailable", name: "Unavailable", location: { lat: 0, lng: 0 } },
    { id: "available", name: "Available", location: { lat: 0, lng: 0 } },
  ], ["soy"]);
  assert.equal(candidates.length, 2);
  assert.equal(candidates[0].website_url, undefined);
  assert.equal(candidates[1].website_url, "https://available.example");
});

test("nearby chat includes bounded history without system messages", () => {
  const turns = Array.from({ length: 12 }, () => ({ role: "user" as const, content: "What about soy?" }));
  const payload = buildNearbySuggestionPayload("And dairy?", { lat: 25, lng: 55 }, [], [], false, "restaurants", turns);
  assert.equal(payload.conversation?.length, 10);
  assert.equal(payload.conversation?.[0].content, "What about soy?");
});


test("menu content excludes deals and non-food without excluding food names", async () => {
  const { isIndividualFoodItem } = await import("../menuContent.ts");
  for (const name of ["Menu added by users 27 days ago", "Menu from owner 3 months ago", "Papa's Meal for Two", "Party Meal", "Pizza Combo", "View Menu", "Coffee", "Water"]) {
    assert.equal(isIndividualFoodItem(name), false, name);
  }
  for (const name of ["Margherita Pizza", "Coffee Cake", "Beer Battered Fish", "Chicken Biryani"]) {
    assert.equal(isIndividualFoodItem(name), true, name);
  }
});

test("scan progress distinguishes reading, completion and failure", async () => {
  const { scanProgress } = await import("../scanProgress.ts");
  assert.equal(scanProgress("discovering").step, 0);
  assert.equal(scanProgress("ocr_processing").step, 1);
  assert.equal(scanProgress("normalizing").step, 1);
  assert.equal(scanProgress("indexing").step, 2);
  assert.equal(scanProgress("complete").step, 3);
  assert.equal(scanProgress("failed").step, -1);
  assert.notEqual(scanProgress("needs_background_refresh").step, 3);
});
