export type AllergyTag =
  | "peanut"
  | "tree_nut"
  | "dairy"
  | "egg"
  | "shellfish"
  | "fish"
  | "soy"
  | "sesame"
  | "wheat_gluten";

export type ProfileSensitivity = "watchful" | "careful" | "strict";
export type DiningMode = "grab_go" | "sit_down" | "late_night" | "study_break";
export type Verdict = "good_fit" | "use_caution" | "high_risk";
export type ImpactDirection = "positive" | "negative";
export type EvidenceStatus = "meaningful" | "limited" | "general";
export type EvidenceTone = "reassuring" | "risk_note";
export type AgentRiskLevel = "low" | "medium" | "high" | "insufficient_evidence";
export type AgentRecommendedAction = "verify" | "avoid" | "ask_staff" | "insufficient_evidence";
export type AgentSourceType = "official_menu" | "restaurant_website" | "review" | "fixture" | "user_upload" | "unknown";
export type MenuRiskLabel = "avoid" | "needs_check" | "possible_lower_risk" | "insufficient_info";
export type SignalType =
  | "accommodation"
  | "staff_knowledge"
  | "menu_labeling"
  | "cross_contact_risk"
  | "reaction_report"
  | "uncertainty";

export interface LatLng {
  lat: number;
  lng: number;
}

export interface PlaceSummary {
  id: string;
  name: string;
  address?: string | null;
  location: LatLng;
  rating?: number | null;
  user_rating_count?: number | null;
  primary_type?: string | null;
  website_url?: string | null;
}

export interface PlaceScoreSummary {
  score: number;
  verdict: Verdict;
  confidence: number;
  fit_score: number;
  fit_verdict: Verdict;
  evidence_confidence: number;
  positive_signals: string[];
  negative_signals: string[];
  evidence_count: number;
  meaningful_evidence: boolean;
  evidence_status: EvidenceStatus;
  evidence_summary: string;
}

export interface ReviewEvidence {
  review_id: string;
  author_name?: string | null;
  rating?: number | null;
  text: string;
  matched_allergens: AllergyTag[];
  signal_type: SignalType;
  impact: ImpactDirection;
  excerpt: string;
  matched_phrase: string;
  signal_label: string;
  tone: EvidenceTone;
  is_allergy_relevant: boolean;
  weight: number;
  publish_time?: string | null;
}

export interface PlaceReviewSnippet {
  review_id: string;
  author_name?: string | null;
  rating?: number | null;
  text: string;
  publish_time?: string | null;
  relative_publish_time?: string | null;
}

export interface ReviewSourceSummary {
  google_review_count: number;
  expanded_review_count: number;
  local_snapshot_review_count: number;
  analyzed_review_count: number;
  displayed_review_count: number;
  expanded_reviews_configured: boolean;
  expanded_review_provider?: "apify" | null;
  expanded_review_status?: "not_configured" | "deferred" | "loaded" | "failed";
}

export interface PlacePhoto {
  name: string;
  url: string;
  width_px?: number | null;
  height_px?: number | null;
  author_names: string[];
}

export interface PlaceOpeningHours {
  openNow?: boolean | null;
  weekdayDescriptions?: string[];
}

export interface MenuItem {
  name: string;
  description?: string | null;
  price?: string | null;
  confirmed_allergens?: AllergyTag[];
  allergen_codes?: string[];
  likely_safe_for: AllergyTag[];
  likely_risky_for: AllergyTag[];
  risk_label?: MenuRiskLabel | null;
  matched_allergens?: AllergyTag[];
  risk_reasons?: string[];
  verification_question?: string | null;
  confidence?: number | null;
  source_page?: number | null;
  source_url?: string | null;
  ocr_confidence?: number | null;
}

export interface MenuSection {
  title: string;
  items: MenuItem[];
}

export interface PlaceMenu {
  source_url?: string | null;
  source_fetched_at?: string | null;
  status?: string | null;
  content_type?: string | null;
  document_url?: string | null;
  document_urls?: string[];
  menu_version?: string | null;
  extraction_method?: string | null;
  page_count?: number | null;
  extraction_confidence?: number | null;
  restaurant_fit_score?: number | null;
  general_match_score?: number | null;
  general_match_label?: string | null;
  restaurant_fit_label?: string | null;
  restaurant_fit_reason?: string | null;
  avoid_count?: number;
  needs_check_count?: number;
  possible_lower_risk_count?: number;
  insufficient_info_count?: number;
  possible_item_names?: string[];
  avoid_item_names?: string[];
  sections: MenuSection[];
}

export interface RecommendedMenuItem {
  name: string;
  section_title?: string | null;
  reason: string;
  caution?: string | null;
  source: "heuristic" | "llm";
}

export interface MenuRefreshJob {
  id: string;
  place_id: string;
  status:
    | "queued"
    | "running"
    | "discovering"
    | "ocr_processing"
    | "normalizing"
    | "indexing"
    | "complete"
    | "failed"
    | "needs_background_refresh";
  message: string;
  item_count?: number;
  source_url?: string | null;
  content_type?: string | null;
  extraction_method?: string | null;
  page_count?: number | null;
  extraction_confidence?: number | null;
  document_urls?: string[];
  total_documents?: number;
  processed_documents?: number;
  menu_version?: string | null;
  indexing_status?: "pending" | "running" | "complete" | "failed" | "skipped" | null;
  trace: IngestionTraceStep[];
  created_at: string;
  completed_at?: string | null;
}

export interface IngestionTraceStep {
  id: string;
  label: string;
  status: "running" | "complete" | "failed" | "skipped" | string;
  detail: string;
  provider?: string | null;
  source_url?: string | null;
  item_count?: number | null;
  duration_ms?: number | null;
}

export interface ReviewRefreshJob {
  id: string;
  place_id: string;
  status: "queued" | "running" | "complete" | "failed" | "skipped";
  message: string;
  reviews_count: number;
  reviews?: PlaceReviewSnippet[];
  created_at: string;
  completed_at?: string | null;
}

export interface AskRestaurantResponse {
  id: string;
  status: "queued" | "sent" | "failed";
  message: string;
  suggested_script: string;
}

export interface PlaceDecisionBrief {
  headline: string;
  summary: string;
  recommended_action: string;
  caution_flags: string[];
}

export interface PlatformUserProfile {
  id: string;
  name: string;
  email?: string | null;
  auth_provider: "google";
}

export interface CommunityReview {
  id: string;
  author_name: string;
  body: string;
  rating?: number | null;
  allergens?: AllergyTag[];
  helpful_count?: number;
  points_awarded?: number;
  created_at: string;
  verification_status: "verified_visit" | "signed_in" | "unverified";
}

export interface AgentEvidenceFragment {
  id: string;
  source_type: AgentSourceType;
  text: string;
  source_url?: string | null;
  source_timestamp?: string | null;
  dish_name?: string | null;
  matched_allergens: AllergyTag[];
  reliability: number;
}

export interface DishRiskResult {
  dish: string;
  risk_level: AgentRiskLevel;
  confidence: number;
  detected_allergens: AllergyTag[];
  evidence: AgentEvidenceFragment[];
  missing_information: string[];
  recommended_questions: string[];
  recommended_action: AgentRecommendedAction;
}

export interface AgentTraceSummary {
  nodes: string[];
  tool_calls: string[];
  abstained: boolean;
  routed_to_safety_gate: boolean;
}

export interface AgentRecommendationResult {
  restaurant_id?: string | null;
  restaurant_name?: string | null;
  overall_risk: AgentRiskLevel;
  confidence: number;
  summary: string;
  dish_results: DishRiskResult[];
  evidence: AgentEvidenceFragment[];
  missing_information: string[];
  recommended_questions: string[];
  recommended_action: AgentRecommendedAction;
  trace: AgentTraceSummary;
}

export interface PlaceDetailsResponse extends PlaceSummary {
  website_uri?: string | null;
  editorial_summary?: string | null;
  national_phone_number?: string | null;
  international_phone_number?: string | null;
  price_level?: string | null;
  price_range?: string | null;
  regular_opening_hours?: PlaceOpeningHours | null;
  current_opening_hours?: PlaceOpeningHours | null;
  service_options?: Record<string, boolean | null | undefined>;
  google_maps_uri: string;
  google_review_uri: string;
  selected_allergens: AllergyTag[];
  score_summary: PlaceScoreSummary;
  restaurant_fit_score?: number | null;
  restaurant_fit_label?: string | null;
  restaurant_fit_reason?: string | null;
  evidence: ReviewEvidence[];
  review_snippets: PlaceReviewSnippet[];
  review_source_summary?: ReviewSourceSummary;
  photos: PlacePhoto[];
  explanation: string;
  decision_brief: PlaceDecisionBrief;
  menu: PlaceMenu | null;
  recommended_items: RecommendedMenuItem[];
  community_reviews: CommunityReview[];
  agent_recommendation?: AgentRecommendationResult | null;
}

export interface SearchResponse {
  query: string;
  center: LatLng;
  allergens: AllergyTag[];
  places: PlaceSummary[];
}

export interface HybridSearchResult {
  id: string;
  restaurant_id: string;
  restaurant_name?: string | null;
  dish_name?: string | null;
  menu_section?: string | null;
  source_type: AgentSourceType;
  source_url?: string | null;
  source_timestamp?: string | null;
  confidence: number;
  raw_text: string;
  citation_label: string;
  citation_text: string;
  matched_allergens: AllergyTag[];
  retrieval_mode: "keyword" | "hybrid" | "vector" | string;
  can_support_low_risk: boolean;
}

export interface NearbyPlaceSuggestion {
  place: PlaceSummary;
  confidence: number;
  evidence_status: "scanned" | "scan_needed" | "scan_running" | "scan_failed";
  scan_job_id?: string | null;
  scan_priority_rank?: number | null;
  scan_priority_score?: number | null;
  restaurant_fit_score?: number | null;
  general_match_score?: number | null;
  general_match_label?: string | null;
  restaurant_fit_label:
    | "Strong candidate, still verify"
    | "Better candidate, still verify"
    | "Some options, needs verification"
    | "Limited options"
    | "High concern"
    | "Menu scan needed"
    | "Good candidate to ask about"
    | "Needs verification"
    | "Scan needed"
    | "Limited fit / scan needed";
  intent_match?: boolean | null;
  intent_note?: string | null;
  menu_item_count: number;
  matched_allergen_items: number;
  avoid_count: number;
  needs_check_count: number;
  possible_lower_risk_count: number;
  insufficient_info_count: number;
  possible_item_names?: string[];
  avoid_item_names?: string[];
  evidence_quality: number;
  evidence_count: number;
  evidence: HybridSearchResult[];
  risk_note: string;
  reason: string;
  next_action: string;
}

export interface NearbySuggestionResponse {
  answer: string;
  retrieval_mode: string;
  ranking_mode: "allergy_fit" | "general_discovery";
  places: NearbyPlaceSuggestion[];
  evidence: HybridSearchResult[];
  missing_information: string[];
  recommended_questions: string[];
  scan_needed_places: PlaceSummary[];
  top_scan_candidates: PlaceSummary[];
  scan_job_ids: string[];
  search_intent?: string | null;
}

export type PlaceDetailState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; data: PlaceDetailsResponse }
  | { status: "error"; message: string };
