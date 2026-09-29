"use client";

import { useEffect, useState, type FormEvent } from "react";

import { scanProgress } from "@/lib/scanProgress";
import { isIndividualFoodItem } from "@/lib/menuContent";

import MenuUpload from "@/components/MenuUpload";
import { useAuth } from "@/components/AuthProvider";
import { fetchCommunityReviews, submitCommunityReview } from "@/lib/api";

import type {
  AllergyTag,
  AskRestaurantResponse,
  CommunityReview,
  MenuItem,
  MenuRefreshJob,
  PlaceDetailsResponse,
  PlaceDetailState,
  PlaceSummary,
} from "@/lib/types";

interface TrustPanelProps {
  place: PlaceSummary | null;
  detailState: PlaceDetailState | undefined;
  onRetry: () => void;
  onAskRestaurant: () => void;
  askResponse?: AskRestaurantResponse | null;
  isAskingRestaurant?: boolean;
  isMenuLoading?: boolean;
  menuRefreshJob?: MenuRefreshJob;
  onRefreshMenu: () => void;
  onChooseAnother: () => void;
}

type PlaceTab = "summary" | "menu" | "community";
type VerificationTone = "needs-check" | "possible" | "possible-weak" | "avoid" | "unknown";

function displayDishName(value: string): string {
  const withoutTags = value.replace(/\s*\|\s*(?:AED\s*)?\d+(?:\.\d{1,2})?(?:\s+[A-Z, ]+)?\s*$/i, "").trim();
  if (!withoutTags || withoutTags !== withoutTags.toUpperCase()) {
    return withoutTags || value;
  }
  return withoutTags
    .toLowerCase()
    .replace(/(^|[\s'’/-])([a-z])/g, (_match, prefix: string, letter: string) => `${prefix}${letter.toUpperCase()}`);
}

function menuItemTooltip(item: MenuItem, detail?: string): string {
  const confirmed = item.confirmed_allergens ?? [];
  const allergenText = confirmed.length
    ? `Menu allergen labels: ${confirmed.map((value) => value.replaceAll("_", " ")).join(", ")}.`
    : "";
  return [item.description, allergenText, detail].filter(Boolean).join(" ");
}

interface MenuVerification {
  label: "Needs check" | "Possible lower-risk" | "Avoid" | "Insufficient info";
  tone: VerificationTone;
  metadata: string;
  detail: string;
}

function formatRiskLabel(value: string): string {
  return value.replace(/_/g, " ");
}

function formatPlaceType(value?: string | null): string {
  if (!value) {
    return "Restaurant";
  }
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatOpenStatus(data: PlaceDetailsResponse): string | null {
  const hours = data.current_opening_hours ?? data.regular_opening_hours;
  if (!hours || typeof hours.openNow !== "boolean") {
    return null;
  }
  return hours.openNow ? "Open now" : "Closed now";
}

function serviceLabels(options: Record<string, boolean | null | undefined> | undefined): string[] {
  const labels: Array<[string, string]> = [
    ["dine_in", "Dine-in"],
    ["takeout", "Takeout"],
    ["delivery", "Delivery"],
    ["reservable", "Reservations"],
    ["serves_lunch", "Lunch"],
    ["serves_dinner", "Dinner"],
    ["serves_vegetarian_food", "Vegetarian options"],
  ];
  return labels.filter(([key]) => options?.[key] === true).map(([, label]) => label);
}

function displayHostName(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function displayRestaurantFitLabel(label: string): string {
  return label.toLowerCase().includes("strong candidate") ? "Good candidate to ask about" : label;
}

function communityAllergenSummary(allergens: AllergyTag[] = []): string {
  return allergens.length > 0 ? allergens.map(formatAllergen).join(", ") : "general dining note";
}

function formatExtractionMethod(value?: string | null): string | null {
  if (!value) {
    return null;
  }
  if (value.includes("azure_document_intelligence")) {
    return "Azure Document Intelligence OCR";
  }
  return value.replace(/_/g, " ");
}

function formatAllergen(value: AllergyTag): string {
  return value.replace(/_/g, " ");
}

function isMenuDisplayArtifact(item: MenuItem): boolean {
  return /^(?:aed|usd|eur|gbp)?\s*\d+(?:\.\d+)?\s+allergens?$/i.test(item.name.trim());
}

function getMenuVerification(
  item: MenuItem,
  selectedAllergens: AllergyTag[],
  fallbackConfidence?: number | null,
): MenuVerification {
  const detectedAllergens = Array.from(
    new Set(
      [...(item.matched_allergens ?? []), ...item.likely_risky_for].filter((allergen) =>
        selectedAllergens.includes(allergen),
      ),
    ),
  );
  const confidence = item.confidence ?? item.ocr_confidence ?? fallbackConfidence;
  const confidenceDetail = typeof confidence === "number" ? `${Math.round(confidence * 100)}% evidence confidence.` : null;
  const detail = [item.risk_reasons?.join(" "), item.verification_question, confidenceDetail].filter(Boolean).join(" ");

  if (item.risk_label === "avoid" || detectedAllergens.length > 0) {
    const allergenList = detectedAllergens.map(formatAllergen).join(", ");
    return {
      label: "Avoid",
      tone: "avoid",
      metadata: allergenList ? `${allergenList} detected` : "selected allergen detected",
      detail: detail || `Selected allergen evidence was detected: ${allergenList}.`,
    };
  }

  if (item.risk_label === "needs_check") {
    return {
      label: "Needs check",
      tone: "needs-check",
      metadata: "",
      detail: detail || "Preparation or ingredient wording needs staff verification.",
    };
  }

  if (item.risk_label === "insufficient_info" || !item.description?.trim()) {
    return {
      label: "Insufficient info",
      tone: "unknown",
      metadata: "ingredient details missing",
      detail: detail || "The menu source does not provide enough ingredient or preparation detail.",
    };
  }

  return {
    label: "Possible lower-risk",
    tone: typeof confidence === "number" && confidence < 0.72 ? "possible-weak" : "possible",
    metadata: "",
    detail: detail || "No selected allergen was detected in the available menu text. Verify preparation with staff.",
  };
}

function traceStatusLabel(status: string): string {
  return status === "fallback_local" ? "complete" : status.replaceAll("_", " ");
}

function traceDetail(step: MenuRefreshJob["trace"][number]): string {
  if (step.status === "fallback_local" || step.detail.toLowerCase().includes("continued with local ingestion")) {
    return "Cloud job could not be saved, so this scan ran directly.";
  }
  return step.detail;
}

export default function TrustPanel({
  place,
  detailState,
  onRetry,
  onAskRestaurant,
  askResponse,
  isAskingRestaurant = false,
  isMenuLoading = false,
  menuRefreshJob,
  onRefreshMenu,
  onChooseAnother,
}: TrustPanelProps) {
  const { session, user, points, signInWithGoogle, refreshPoints } = useAuth();
  const [tabState, setTabState] = useState<{ placeId: string | null; tab: PlaceTab }>({
    placeId: null,
    tab: "summary",
  });
  const [expandedMenuGroups, setExpandedMenuGroups] = useState<{ placeId: string | null; keys: string[] }>({
    placeId: null,
    keys: [],
  });
  const [communityReviews, setCommunityReviews] = useState<CommunityReview[]>([]);
  const [reviewDraft, setReviewDraft] = useState({ author_name: "", body: "", rating: 5 });
  const [reviewStatus, setReviewStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [reviewMessage, setReviewMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!detailState || detailState.status !== "ready") {
      return;
    }
    const placeId = detailState.data.id;
    setCommunityReviews(detailState.data.community_reviews ?? []);
    setReviewStatus("idle");
    setReviewMessage(null);
    void fetchCommunityReviews(placeId)
      .then((reviews) => setCommunityReviews(reviews))
      .catch(() => undefined);
  }, [detailState]);

  if (!place) {
    return (
      <div className="trust-panel-empty">
        <p className="panel-eyebrow">Place details</p>
        <h2>Select a restaurant</h2>
        <p>Click a map marker or search result to review allergy evidence and menu signals.</p>
      </div>
    );
  }

  if (!detailState || detailState.status === "loading" || detailState.status === "idle") {
    return (
      <div className="trust-panel-loading">
        <p className="panel-eyebrow">Place details</p>
        <h2>{place.name}</h2>
        <div className="skeleton skeleton-badge" />
        <div className="skeleton skeleton-line" />
        <div className="skeleton skeleton-line" />
        <div className="skeleton skeleton-review" />
        <div className="skeleton skeleton-review" />
      </div>
    );
  }

  if (detailState.status === "error") {
    return (
      <div className="trust-panel-empty">
        <p className="panel-eyebrow">Place details</p>
        <h2>{place.name}</h2>
        <p>{detailState.message}</p>
        <button type="button" className="retry-button" onClick={onRetry}>
          Retry scoring
        </button>
      </div>
    );
  }

  const { data } = detailState;
  const activeTab = tabState.placeId === data.id ? tabState.tab : "summary";
  const menuSections = (data.menu?.sections ?? [])
    .map((section) => ({ ...section, items: section.items.filter((item) => !isMenuDisplayArtifact(item) && isIndividualFoodItem(item.name)) }))
    .filter((section) => section.items.length > 0);
  const menuItemCount = menuSections.reduce((count, section) => count + section.items.length, 0);
  const allergyMode = data.selected_allergens.length > 0;
  const classifiedMenuItems = menuSections.flatMap((section) =>
    section.items.map((item) => ({
      item,
      sectionTitle: section.title,
      verification: getMenuVerification(item, data.selected_allergens, data.menu?.extraction_confidence),
    })),
  );
  const possibleMenuItems = classifiedMenuItems
    .filter((entry) => entry.verification.label === "Possible lower-risk")
    .sort((left, right) => (right.item.confidence ?? 0) - (left.item.confidence ?? 0));
  const needsCheckMenuItems = classifiedMenuItems.filter((entry) => entry.verification.label === "Needs check");
  const avoidMenuItems = classifiedMenuItems.filter((entry) => entry.verification.label === "Avoid");
  const insufficientMenuItems = classifiedMenuItems.filter((entry) => entry.verification.label === "Insufficient info");
  const menuRiskGroups = [
    {
      key: "possible",
      title: "Possible lower-risk items to ask about",
      tone: "possible",
      count: possibleMenuItems.length,
      items: possibleMenuItems,
    },
    {
      key: "check",
      title: "Needs staff check",
      tone: "needs-check",
      count: needsCheckMenuItems.length,
      items: needsCheckMenuItems,
    },
    {
      key: "avoid",
      title: "Avoid for your allergies",
      tone: "avoid",
      count: avoidMenuItems.length,
      items: avoidMenuItems,
    },
    {
      key: "insufficient",
      title: "Insufficient info",
      tone: "unknown",
      count: insufficientMenuItems.length,
      items: insufficientMenuItems,
    },
  ];
  const menuBucketCounts = {
    possible: possibleMenuItems.length,
    check: needsCheckMenuItems.length,
    avoid: avoidMenuItems.length,
    insufficient: insufficientMenuItems.length,
  };
  const restaurantFitScore = data.menu?.restaurant_fit_score ?? data.restaurant_fit_score ?? null;
  const restaurantFitLabel =
    data.menu?.restaurant_fit_label ?? data.restaurant_fit_label ?? (menuItemCount > 0 ? "Needs verification" : "Menu scan needed");
  const visibleRestaurantFitLabel = displayRestaurantFitLabel(restaurantFitLabel);
  const restaurantFitReason = data.menu?.restaurant_fit_reason ?? data.restaurant_fit_reason ?? null;
  const hasRestaurantFit = allergyMode && menuItemCount > 0 && restaurantFitScore != null;
  const restaurantFitTone = (restaurantFitScore ?? 0) >= 70 ? "good" : (restaurantFitScore ?? 0) >= 45 ? "caution" : "risk";
  const restaurantFitMessage =
    restaurantFitReason ?? (menuBucketCounts.avoid > 0 && menuBucketCounts.possible > 0
      ? "Some dishes contain your allergens, but many menu items may be possible lower-risk after staff verification."
      : menuBucketCounts.possible > 0
        ? "Several menu items may be possible lower-risk after staff verification."
        : "The current menu evidence still needs careful staff verification.");
  const reviewSnippets = data.review_snippets ?? [];
  const communityReviewCount = communityReviews.length;
  const communityRatingAverage =
    communityReviews.length > 0
      ? communityReviews.reduce((sum, review) => sum + (review.rating ?? 0), 0) /
        Math.max(1, communityReviews.filter((review) => typeof review.rating === "number").length)
      : null;
  const reviewSource = data.review_source_summary;
  const reviewSourceLine = reviewSource
    ? reviewSource.expanded_reviews_configured
      ? reviewSource.expanded_review_status === "deferred"
        ? `Expanded Apify reviews are configured but loaded separately so place details stay fast. Showing ${reviewSource.google_review_count} Google snippet${reviewSource.google_review_count === 1 ? "" : "s"} now.`
        : reviewSource.expanded_review_count > 0
        ? `Apify expanded reviews analyzed: ${reviewSource.expanded_review_count}. Showing ${reviewSource.displayed_review_count} most relevant of ${reviewSource.analyzed_review_count} total review snippets.`
        : `Apify is configured, but no expanded reviews were returned for this place. Showing ${reviewSource.google_review_count} Google snippet${reviewSource.google_review_count === 1 ? "" : "s"}.`
      : `Apify is not configured for this running server. Showing Google’s limited review sample of ${reviewSource.google_review_count} snippet${reviewSource.google_review_count === 1 ? "" : "s"}.`
    : "Showing the review snippets returned by the current place details source.";
  const agentRecommendation = data.agent_recommendation ?? null;
  const agentConfidencePercent = agentRecommendation ? Math.round(agentRecommendation.confidence * 100) : null;
  const openStatus = formatOpenStatus(data);
  const services = serviceLabels(data.service_options);
  const extractionMethod = formatExtractionMethod(data.menu?.extraction_method);
  const extractionConfidence =
    typeof data.menu?.extraction_confidence === "number"
      ? `${Math.round(data.menu.extraction_confidence * 100)}% extraction confidence`
      : null;
  const menuEvidenceLine = [extractionMethod, extractionConfidence, data.menu?.page_count ? `${data.menu.page_count} page${data.menu.page_count === 1 ? "" : "s"}` : null]
    .filter(Boolean)
    .join(" · ");
  const indexingStatus =
    menuRefreshJob?.indexing_status ??
    menuRefreshJob?.trace.find((step) => step.id === "search_index")?.status ??
    null;
  const scanHasRun = Boolean(menuRefreshJob);
  const ragStatus =
    indexingStatus === "complete"
      ? { className: "rag-ready", label: "RAG index ready" }
      : indexingStatus === "pending" || indexingStatus === "running"
        ? { className: "rag-updating", label: "RAG index updating" }
        : indexingStatus === "failed"
          ? { className: "rag-unavailable", label: "RAG index unavailable" }
          : menuItemCount > 0
            ? { className: "rag-ready", label: "RAG index ready" }
            : scanHasRun
              ? { className: "rag-updating", label: "RAG index updating" }
              : null;
  const refreshFailed = menuRefreshJob?.status === "failed";
  const refreshFailureDetail = refreshFailed
    ? menuRefreshJob?.message || menuRefreshJob?.trace.find((step) => step.id === "menu_ingestion_error")?.detail
    : null;
  const refreshPending =
    isMenuLoading ||
    ["queued", "running", "discovering", "ocr_processing", "normalizing", "indexing", "needs_background_refresh"].includes(
      menuRefreshJob?.status ?? "",
    );
  const technicalMenuLifecycleLabel =
    menuItemCount > 0 && indexingStatus === "complete"
      ? "Menu found · RAG index ready"
      : menuItemCount > 0 && refreshPending
        ? "Menu found · deeper scan running"
        : menuItemCount > 0
          ? "Menu found"
          : refreshPending
            ? "Menu scan running"
            : "No menu evidence";
  const menuLifecycleLabel = menuItemCount > 0 ? "Menu found" : refreshPending ? "Scanning menu" : "Menu scan needed";
  const ocrTrace = menuRefreshJob?.trace.find((step) => step.id === "document_ocr");
  const ocrStatus =
    data.menu?.extraction_method?.includes("azure_document_intelligence") || ocrTrace?.status === "complete"
      ? { className: "ocr-used", label: "OCR used" }
      : ocrTrace?.status === "running"
        ? { className: "ocr-checking", label: "OCR checking" }
        : scanHasRun || menuItemCount > 0
          ? { className: "ocr-skipped", label: "OCR skipped" }
          : null;
  const ratingLine = [
    data.rating ? `${data.rating.toFixed(1)} on Google` : null,
    data.user_rating_count ? `${data.user_rating_count.toLocaleString()} reviews` : null,
    data.price_range ?? data.price_level?.replace("PRICE_LEVEL_", "").replace(/_/g, " ").toLowerCase() ?? null,
    formatPlaceType(data.primary_type),
  ].filter(Boolean).join(" · ");
  const generalMatchLabel = (data.rating ?? 0) >= 4.5 ? "Popular nearby option" : "Restaurant match";

  const handleCommunityReviewSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!session?.access_token) {
      setReviewStatus("error");
      setReviewMessage("Sign in with Google above the allergy filters to post and earn points.");
      return;
    }
    setReviewStatus("saving");
    setReviewMessage(null);
    try {
      const result = await submitCommunityReview(data.id, {
        ...reviewDraft,
        allergens: data.selected_allergens,
      }, session.access_token);
      setCommunityReviews((current) => [result.review, ...current.filter((review) => review.id !== result.review.id)]);
      setReviewDraft({ author_name: reviewDraft.author_name, body: "", rating: reviewDraft.rating });
      setReviewStatus("saved");
      setReviewMessage(`Saved. +${result.points_awarded} points · ${result.total_points} total.`);
      await refreshPoints();
    } catch (error) {
      setReviewStatus("error");
      setReviewMessage(error instanceof Error ? error.message : "Review could not be saved.");
    }
  };

  return (
    <div className="trust-panel-content">
      <div className="place-sheet-header">
        <p className="panel-eyebrow">Place details</p>
        <div className="place-title-with-fit">
          <h2>{data.name}</h2>
          {hasRestaurantFit && (
            <span
              className={`restaurant-fit-badge ${restaurantFitTone}`}
              title="Menu evidence fit: based on direct allergen matches and the number of dishes worth asking staff about. This is not a safety guarantee."
            >
              {restaurantFitScore}
            </span>
          )}
          {!allergyMode && data.rating != null && <span className="restaurant-rating-badge">{data.rating.toFixed(1)}★</span>}
        </div>
        {hasRestaurantFit && <p className="restaurant-fit-label">{visibleRestaurantFitLabel}</p>}
        {!allergyMode && <p className="restaurant-fit-label">{generalMatchLabel}</p>}
        <p>{data.address ?? "Address unavailable"}</p>
        {ratingLine && <p>{ratingLine}</p>}
      </div>

      <div className="detail-action-row compact-actions">
        <a className="detail-link google-link" href={data.google_maps_uri} target="_blank" rel="noreferrer">
          View on Google Maps
        </a>
        {data.website_uri && (
          <a className="detail-link" href={data.website_uri} target="_blank" rel="noreferrer">
            Website
          </a>
        )}
      </div>

      <div className="place-tabs" role="tablist" aria-label="Place information">
        {(["summary", "menu", "community"] as PlaceTab[]).map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={activeTab === tab}
            className={`place-tab ${activeTab === tab ? "active" : ""}`}
            onClick={() => setTabState({ placeId: data.id, tab })}
          >
            {tab}
          </button>
        ))}
      </div>

      {activeTab === "summary" && (
        <div className="place-tab-panel">
          <div className="overview-line">
            <strong>{openStatus ?? (menuItemCount === 0 ? "Restaurant overview" : data.decision_brief.headline)}</strong>
            <p>{data.editorial_summary ?? (menuItemCount === 0 ? "Explore this restaurant and scan its menu for dish information." : data.decision_brief.summary)}</p>
          </div>
          {services.length > 0 && (
            <div className="service-chip-row" aria-label="Service options">
              {services.slice(0, 5).map((service) => (
                <span key={service}>{service}</span>
              ))}
            </div>
          )}
          {allergyMode && !hasRestaurantFit && (
            <div className="overview-line">
              <strong>{menuItemCount === 0 ? "Menu not assessed" : "Allergy evidence"}</strong>
              <p>{menuItemCount === 0 ? "We haven’t assessed a menu for this restaurant yet. No menu-based allergy assessment is available." : data.decision_brief.summary}</p>
            </div>
          )}
          {hasRestaurantFit && (
            <div className={`overview-line restaurant-fit-overview ${restaurantFitTone}`}>
              <strong>{visibleRestaurantFitLabel}</strong>
              <p>{restaurantFitMessage}</p>
            </div>
          )}
          {allergyMode && menuItemCount > 0 && agentRecommendation && agentRecommendation.overall_risk !== "insufficient_evidence" && !hasRestaurantFit && (
            <div className={`overview-line agent-risk ${agentRecommendation.overall_risk}`}>
              <strong>
                Menu assessment: {formatRiskLabel(agentRecommendation.overall_risk)} ·{" "}
                {formatRiskLabel(agentRecommendation.recommended_action)}
              </strong>
              <p>{agentRecommendation.summary}</p>
              {agentConfidencePercent !== null && <p>{agentConfidencePercent}% source confidence.</p>}
            </div>
          )}
          <div className="overview-line compact-place-facts">
            {data.address && (
              <p>
                <strong>Address</strong>
                <span>{data.address}</span>
              </p>
            )}
            {(data.national_phone_number || data.international_phone_number) && (
              <p>
                <strong>Phone</strong>
                <span>{data.national_phone_number ?? data.international_phone_number}</span>
              </p>
            )}
            {data.website_uri && (
              <p>
                <strong>Website</strong>
                <a className="source-link" href={data.website_uri} target="_blank" rel="noreferrer">
                  {displayHostName(data.website_uri)}
                </a>
              </p>
            )}
          </div>
        </div>
      )}

      {menuRefreshJob && (
        <div className="scan-progress" role="status" aria-live="polite">
          <strong>{scanProgress(menuRefreshJob.status).label}</strong>
          <ol aria-label="Menu scan progress">
            {["Finding menu", "Reading dishes", "Preparing results", "Finished"].map((label, index) => (
              <li key={label} aria-current={scanProgress(menuRefreshJob.status).step === index ? "step" : undefined}
                className={scanProgress(menuRefreshJob.status).step >= index ? "reached" : ""}>{label}</li>
            ))}
          </ol>
          {menuItemCount > 0 && <button type="button" onClick={() => {
            setTabState({ placeId: data.id, tab: "menu" });
            requestAnimationFrame(() => document.getElementById("menu-results")?.scrollIntoView({ behavior: "smooth", block: "start" }));
          }}>View menu results</button>}
          {refreshFailed && <button type="button" onClick={onRefreshMenu}>Retry scan</button>}
        </div>
      )}

      {activeTab === "menu" && (
        <div className="place-tab-panel" id="menu-results">
          {menuItemCount > 0 && <p className="menu-provenance">
            {(data.menu?.source_url || data.menu?.document_url) && <a href={data.menu.source_url ?? data.menu.document_url ?? ""} target="_blank" rel="noreferrer">Menu source</a>}
            {" · "}{data.menu?.source_fetched_at ? "Last scanned " + new Date(data.menu.source_fetched_at).toLocaleDateString() : "Scan date unavailable"}
          </p>}
          {hasRestaurantFit && (
            <div className="menu-fit-summary" aria-label="Restaurant allergy fit summary">
              <span className="menu-primary-status">Menu found</span>
              <div className="menu-fit-heading">
                <strong>Menu evidence fit</strong>
                <span
                  className={`restaurant-fit-badge ${restaurantFitTone}`}
                  title="Based on direct allergen matches and the number of dishes worth asking staff about. This is not a safety guarantee."
                >
                  {restaurantFitScore}
                </span>
                <b>{visibleRestaurantFitLabel}</b>
              </div>
              <p>
                {menuBucketCounts.possible} possible · {menuBucketCounts.check} check · {menuBucketCounts.avoid} avoid
                {menuBucketCounts.insufficient > 0 ? ` · ${menuBucketCounts.insufficient} insufficient` : ""}
              </p>
            </div>
          )}
          {!hasRestaurantFit && (
            <div className="menu-scan-summary">
              <strong>{menuLifecycleLabel}</strong>
              <p>
                {menuItemCount > 0
                  ? allergyMode
                    ? "Allergy fit is updating from the latest menu evidence."
                    : `${menuItemCount} menu item${menuItemCount === 1 ? "" : "s"} found.`
                  : refreshPending
                  ? "Items will appear as the scan finishes."
                  : refreshFailureDetail ?? "Scan the official menu before comparing allergy fit."}
              </p>
            </div>
          )}
          {allergyMode && menuItemCount > 0 && (
            <p className="menu-allergy-disclaimer">
              Menu labels use available text only. Confirm ingredients, sauces, and shared preparation with staff.
            </p>
          )}
          {refreshPending && menuSections.length === 0 ? (
            <div className="menu-loading-state">
              <strong>Menu scan is still running</strong>
              <p>Extracted items will appear here as soon as the scan finishes.</p>
              <div className="skeleton skeleton-line" />
              <div className="skeleton skeleton-line" />
              <div className="skeleton skeleton-review" />
            </div>
          ) : menuSections.length > 0 && allergyMode ? (
            <div className="menu-risk-groups">
              {menuRiskGroups.filter((group) => group.items.length > 0).map((group) => {
                const isExpanded =
                  expandedMenuGroups.placeId === data.id && expandedMenuGroups.keys.includes(group.key);
                const visibleItems = isExpanded ? group.items : group.items.slice(0, 5);
                return (
                  <section key={group.key} className={`menu-risk-group ${group.tone}`}>
                    <div className="menu-risk-group-header">
                      <h3>{group.title}</h3>
                      <span>{group.count}</span>
                    </div>
                    {visibleItems.map(({ item, sectionTitle, verification }) => {
                    const tooltip = menuItemTooltip(item, verification.detail);
                    return (
                      <article
                        key={`${sectionTitle}-${item.name}`}
                        className="menu-list-item compact-menu-row"
                        title={tooltip}
                      >
                        <div>
                          <div className="menu-item-heading">
                            <strong>{displayDishName(item.name)}</strong>
                          </div>
                          <details className="dish-evidence">
                            <summary>Why this label?</summary>
                            {item.description && <p>{item.description}</p>}
                            <p>{verification.detail}</p>
                            {(item.risk_reasons ?? []).map((reason) => <p key={reason}>{reason}</p>)}
                            <p>{item.verification_question || "Ask staff to confirm ingredients, sauces, and shared preparation."}</p>
                            {item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">Dish source</a>}
                          </details>
                        </div>
                        <span className="menu-price">{item.price || "Price not listed"}</span>
                      </article>
                    );
                    })}
                    {group.items.length > 5 && (
                      <button
                        type="button"
                        className="menu-group-toggle"
                        onClick={() =>
                          setExpandedMenuGroups((current) => {
                            const keys = current.placeId === data.id ? current.keys : [];
                            return {
                              placeId: data.id,
                              keys: isExpanded ? keys.filter((key) => key !== group.key) : [...keys, group.key],
                            };
                          })
                        }
                      >
                        {isExpanded ? "Show less" : `Show ${group.items.length - 5} more`}
                      </button>
                    )}
                  </section>
                );
              })}
            </div>
          ) : menuSections.length > 0 ? (
            <div className="menu-risk-groups general-menu-groups">
              {menuSections.map((section) => (
                <section key={section.title} className="menu-risk-group general">
                  <div className="menu-risk-group-header">
                    <h3>{section.title}</h3>
                    <span>{section.items.length}</span>
                  </div>
                  {section.items.slice(0, 12).map((item) => (
                    <article
                      key={`${section.title}-${item.name}`}
                      className="menu-list-item compact-menu-row"
                      title={menuItemTooltip(item)}
                    >
                      <div>
                        <div className="menu-item-heading">
                          <strong>{displayDishName(item.name)}</strong>
                        </div>
                      </div>
                      <span className="menu-price">{item.price || "Price not listed"}</span>
                    </article>
                  ))}
                </section>
              ))}
            </div>
          ) : !scanHasRun ? (
            <article className="empty-menu-state">
              <strong>No menu scanned yet</strong>
              <p>Start a scan to check the official website and any linked PDF or image menus.</p>
              <button type="button" className="retry-button" onClick={onRefreshMenu}>
                Scan menu
              </button>
            </article>
          ) : (
            <article className="empty-menu-state">
              <strong>No stored menu evidence yet</strong>
              <p>Try the menu scan again or open the restaurant website to verify current menu information.</p>
              {data.website_uri && (
                <a className="source-link" href={data.website_uri} target="_blank" rel="noreferrer">
                  Restaurant website
                </a>
              )}
              <button type="button" className="retry-button" onClick={onRefreshMenu}>
                Retry menu scan
              </button>
            </article>
          )}

          {menuItemCount === 0 && !refreshPending && (
            <div className="empty-menu-actions">
              <MenuUpload key={data.id} />
              <button type="button" onClick={onChooseAnother}>Choose another restaurant</button>
            </div>
          )}

          {(isMenuLoading || menuRefreshJob || menuItemCount > 0) && (
            <details className="menu-trace">
              <summary>
                <strong>Technical trace</strong>
                <span className={`trace-status ${isMenuLoading ? "running" : indexingStatus ?? menuRefreshJob?.status ?? "idle"}`}>
                  {refreshPending
                    ? "Running"
                    : refreshFailed
                      ? "Needs attention"
                      : indexingStatus === "complete" || menuRefreshJob?.status === "complete"
                        ? "Complete"
                        : "Available"}
                </span>
              </summary>
              <div className="menu-technical-overview">
                <p>{technicalMenuLifecycleLabel}</p>
                {ragStatus && <p>{ragStatus.label}</p>}
                {ocrStatus && <p>{ocrStatus.label}</p>}
                {menuEvidenceLine && <p>{menuEvidenceLine}</p>}
                {restaurantFitReason && <p>{restaurantFitReason}</p>}
                {refreshFailed && <p>Refresh failed: {refreshFailureDetail}</p>}
                <div className="menu-technical-actions">
                  {(data.menu?.source_url || data.menu?.document_url) && (
                    <a className="source-link" href={data.menu.source_url ?? data.menu.document_url ?? ""} target="_blank" rel="noreferrer">
                      Open menu source
                    </a>
                  )}
                  <button type="button" className="retry-button" onClick={onRefreshMenu} disabled={refreshPending}>
                    {refreshPending ? "Scan running" : "Refresh menu"}
                  </button>
                </div>
              </div>
              <div className="menu-trace-list">
                {(menuRefreshJob?.total_documents ?? 0) > 0 && (
                  <p className="muted-line">
                    {menuRefreshJob?.processed_documents ?? 0} of {menuRefreshJob?.total_documents ?? 0} menu pages processed
                    {menuRefreshJob?.menu_version ? ` · ${menuRefreshJob.menu_version}` : ""}
                  </p>
                )}
                {(menuRefreshJob?.trace ?? []).map((step) => (
                  <article key={step.id} className={`menu-trace-step ${step.status}`}>
                    <div>
                      <strong>{step.label}</strong>
                      <span>{traceStatusLabel(step.status)}</span>
                    </div>
                    <p>{traceDetail(step)}</p>
                    <small>
                      {[step.provider?.replaceAll("_", " "), typeof step.duration_ms === "number" ? `${step.duration_ms} ms` : null]
                        .filter(Boolean)
                        .join(" · ")}
                    </small>
                    {step.source_url && (
                      <a href={step.source_url} target="_blank" rel="noreferrer">
                        Inspect source
                      </a>
                    )}
                  </article>
                ))}
              </div>
              {!isMenuLoading &&
                (menuRefreshJob?.status === "failed" || menuRefreshJob?.status === "needs_background_refresh") && (
                <button type="button" className="retry-button" onClick={onRefreshMenu}>
                  Retry menu scan
                </button>
              )}
            </details>
          )}

          {allergyMode && (
            <details className="menu-questions">
              <summary>Questions to ask staff</summary>
              <button type="button" className="ask-button" onClick={onAskRestaurant} disabled={isAskingRestaurant}>
                {isAskingRestaurant ? "Saving question..." : "Prepare a verification question"}
              </button>
              {askResponse && <p className="menu-job-note">{askResponse.suggested_script}</p>}
            </details>
          )}
        </div>
      )}

      {activeTab === "community" && (
        <div className="place-tab-panel">
          <div className="community-summary-card">
            <strong>AllerNav community</strong>
            <p>
              {communityReviewCount > 0
                ? `${communityReviewCount} allergy review${communityReviewCount === 1 ? "" : "s"} from AllerNav diners${
                    communityRatingAverage ? ` · ${communityRatingAverage.toFixed(1)} average` : ""
                  }.`
                : "No AllerNav allergy comments for this restaurant yet."}
            </p>
            <small>Posting is inside AllerNav. Google reviews remain read-only discovery context.</small>
          </div>

          <form className="community-review-form" onSubmit={handleCommunityReviewSubmit}>
            <div className="community-account-line">
              {user ? (
                <span><strong>{user.user_metadata?.full_name ?? user.email}</strong> · {points} total points</span>
              ) : (
                <button type="button" className="google-sign-in-inline" onClick={() => void signInWithGoogle()}>
                  Continue with Google to review
                </button>
              )}
            </div>
            <div className="community-form-row">
              <label>
                Rating
                <select
                  value={reviewDraft.rating}
                  onChange={(event) => setReviewDraft((current) => ({ ...current, rating: Number(event.target.value) }))}
                >
                  {[5, 4, 3, 2, 1].map((rating) => (
                    <option key={rating} value={rating}>
                      {rating} star{rating === 1 ? "" : "s"}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label>
              Allergy comment
              <textarea
                value={reviewDraft.body}
                onChange={(event) => setReviewDraft((current) => ({ ...current, body: event.target.value }))}
                placeholder="What did staff confirm? Mention allergens, prep, substitutions, or cross-contact."
                rows={4}
              />
            </label>
            <div className="community-review-footer">
              <small>Signed-in reviews earn points tied to your AllerNav account.</small>
              <button type="submit" className="retry-button" disabled={reviewStatus === "saving" || !user}>
                {reviewStatus === "saving" ? "Saving..." : "Post review"}
              </button>
            </div>
            {reviewMessage && <p className={`community-review-message ${reviewStatus}`}>{reviewMessage}</p>}
          </form>

          {communityReviews.length > 0 && (
            <div className="review-group">
              <strong>AllerNav reviews</strong>
              <div className="evidence-list compact">
                {communityReviews.slice(0, 6).map((review) => (
                  <article key={review.id} className="community-review-item">
                    <div className="evidence-item-header">
                      <span>{review.author_name}</span>
                      <span>{review.rating ? `${review.rating.toFixed(1)}★` : "Review"}</span>
                    </div>
                    <p className="evidence-excerpt">{review.body}</p>
                    <p className="review-source-line">
                      {communityAllergenSummary(review.allergens)} · {review.points_awarded ?? 0} points
                    </p>
                  </article>
                ))}
              </div>
            </div>
          )}

          {allergyMode && data.evidence.length > 0 && <details className="review-group google-signal-details">
            <summary>Google allergy mentions ({data.evidence.length})</summary>
            <p className="panel-note">{reviewSourceLine}</p>
            <div className="evidence-list compact">
              {data.evidence.slice(0, 4).map((item) => {
                return (
                  <article
                    key={`${item.review_id}-${item.signal_type}-${item.matched_phrase}`}
                    className={`evidence-item ${item.impact}`}
                  >
                    <div className="evidence-item-header">
                      <span>{item.author_name ?? "Google review"}</span>
                      <span>{item.rating ? `${item.rating.toFixed(1)}★` : "Rating unavailable"}</span>
                    </div>
                    <p className="evidence-excerpt">{item.excerpt}</p>
                    <p className="review-source-line">
                      Matched {item.signal_label.toLowerCase()} · {item.matched_allergens.map((allergen) => allergen.replace("_", " ")).join(", ")}
                    </p>
                  </article>
                );
              })}
            </div>
          </details>}

          {allergyMode && data.evidence.length === 0 && reviewSnippets.length > 0 && (
            <p className="google-signal-empty">No allergy-specific mentions found in the available Google review sample.</p>
          )}
        </div>
      )}

    </div>
  );
}
