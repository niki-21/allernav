from __future__ import annotations

import asyncio
import json
import math
import os
import re
from typing import Any
from urllib import error, request

from .azure_search import hybrid_search_menu
from .apify_reviews import load_cached_reviews
from .langchain_tracing import (
    ainvoke_traced_runnable,
    invoke_traced_runnable,
    langchain_run_config,
    update_current_trace_metadata,
)
from .menu_ingestion import load_menu_source
from .models import (
    AllergyTag,
    HybridSearchRequest,
    HybridSearchResult,
    LatLng,
    MenuSource,
    NearbyPlaceSuggestion,
    NearbySuggestionRequest,
    NearbySuggestionResponse,
    PlaceListItem,
)
from .restaurant_scoring import has_confirmed_allergen_risk_review, score_restaurant_menu


INTENT_TERMS: dict[str, tuple[str, ...]] = {
    "french": ("french", "bistro", "brasserie", "patisserie"),
    "italian": ("italian", "pizzeria", "trattoria", "osteria"),
    "thai": ("thai",),
    "indian": ("indian",),
    "chinese": ("chinese", "dim sum"),
    "japanese": ("japanese", "ramen", "sushi", "izakaya"),
    "korean": ("korean",),
    "mexican": ("mexican", "taqueria", "taco"),
    "mediterranean": ("mediterranean",),
    "middle eastern": ("middle eastern", "lebanese", "persian"),
    "burger": ("burger", "american", "fast casual", "fast food"),
    "sushi": ("sushi", "japanese"),
    "brunch": ("brunch", "breakfast", "cafe"),
    "cafe": ("cafe", "cafes", "coffee", "coffee shop", "bakery"),
    "vegan": ("vegan",),
    "vegetarian": ("vegetarian",),
}

try:
    from langsmith import traceable
except ImportError:  # pragma: no cover - optional local dependency
    def traceable(*args: Any, **_kwargs: Any) -> Any:
        if args and callable(args[0]):
            return args[0]

        def decorator(func: Any) -> Any:
            return func

        return decorator


@traceable(name="AllerNav Nearby Hybrid RAG", run_type="chain")
async def suggest_nearby_places_service(
    payload: NearbySuggestionRequest,
) -> NearbySuggestionResponse:
    candidates = payload.candidate_places[: payload.max_places]
    if not payload.allergens:
        return build_general_nearby_response(payload, candidates)

    candidate_sources = [(place, load_menu_source(place.id)) for place in candidates]
    suggestions = list(
        await asyncio.gather(
            *(
                asyncio.to_thread(build_place_suggestion, place, payload, source)
                for place, source in candidate_sources
            )
        )
    )
    suggestions = assign_scan_priorities(suggestions, payload.center)
    if payload.allow_background_scan:
        suggestions = await start_background_scans(suggestions)
    suggestions.sort(key=suggestion_rank_key, reverse=True)

    scanned = [suggestion for suggestion in suggestions if suggestion.evidence_status == "scanned"]
    scan_needed = [suggestion for suggestion in suggestions if suggestion.evidence_status != "scanned"]
    evidence = [item for suggestion in scanned for item in suggestion.evidence]
    missing_information = build_missing_information(suggestions)
    questions = build_recommended_questions(payload.allergens)
    top_scan_candidates = scan_needed[:3]
    answer = build_nearby_summary(payload, len(candidates), scanned, scan_needed, top_scan_candidates)
    if scanned and evidence and re.search(r"\b(does|do|is|are|contain|contains|ingredient|allergen)\b", payload.question, re.IGNORECASE):
        answer = await generate_nearby_answer(payload, scanned[:3], evidence, missing_information, questions)
    retrieval_mode = "hybrid_keyword_semantic" if scanned else "scanned_menu_evidence_needed"
    trace_nearby_result(payload, suggestions, retrieval_mode, top_scan_candidates)
    scan_job_ids = [item.scan_job_id for item in suggestions if item.scan_job_id]

    return NearbySuggestionResponse(
        answer=answer,
        retrieval_mode=retrieval_mode,
        ranking_mode="allergy_fit",
        places=suggestions,
        evidence=evidence,
        missing_information=missing_information,
        recommended_questions=questions,
        scan_needed_places=[suggestion.place for suggestion in scan_needed],
        top_scan_candidates=[suggestion.place for suggestion in top_scan_candidates],
        scan_job_ids=scan_job_ids,
        search_intent=payload.query,
    )


def build_general_nearby_response(
    payload: NearbySuggestionRequest,
    candidates: list[PlaceListItem],
) -> NearbySuggestionResponse:
    suggestions = [build_general_place_suggestion(place, payload.center, payload.query) for place in candidates]
    suggestions.sort(
        key=lambda item: (item.intent_match is not False, item.general_match_score or 0),
        reverse=True,
    )
    update_current_trace_metadata(
        candidate_count=len(candidates),
        scanned_candidate_count=sum(item.menu_item_count > 0 for item in suggestions),
        scan_needed_count=0,
        selected_allergens=[],
        top_ranked_place=suggestions[0].place.name if suggestions else None,
        restaurant_fit_score=None,
        retrieval_mode="general_discovery",
        allow_background_scan=False,
        flow_stage="ranked_comparison",
        ranking_mode="general_discovery",
    )
    return NearbySuggestionResponse(
        answer=build_general_summary(payload, suggestions),
        retrieval_mode="general_discovery",
        ranking_mode="general_discovery",
        places=suggestions,
        evidence=[],
        missing_information=[],
        recommended_questions=[],
        scan_needed_places=[],
        top_scan_candidates=[],
        scan_job_ids=[],
        search_intent=payload.query,
    )


def build_general_summary(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
) -> str:
    if not suggestions:
        return "No allergies selected. I could not find restaurants in the current map area."
    label, _aliases = requested_intent(payload.query)
    if label and not any(item.intent_match for item in suggestions):
        return (
            f"No allergies selected. I found restaurants nearby, but they do not strongly match {label}. "
            f"Try searching this area again with {payload.query}."
        )
    return (
        f"No allergies selected. I found {len(suggestions)} nearby restaurant"
        f"{'s' if len(suggestions) != 1 else ''} and ranked them by rating, popularity, distance, and search intent."
    )


def build_general_place_suggestion(
    place: PlaceListItem,
    center: LatLng | None,
    query: str = "restaurants",
) -> NearbyPlaceSuggestion:
    source = load_menu_source(place.id)
    menu_item_count = sum(len(section.items) for section in source.sections) if source else 0
    score = general_discovery_score(place, center, menu_item_count > 0)
    label = general_discovery_label(place)
    intent_match, intent_note = candidate_intent_match(place, query)
    return NearbyPlaceSuggestion(
        place=place.model_copy(update={"name": display_place_name(place.name)}),
        confidence=round(score / 100, 2),
        evidence_status="scanned" if menu_item_count else "scan_needed",
        restaurant_fit_score=None,
        general_match_score=score,
        general_match_label=label,
        menu_item_count=menu_item_count,
        evidence_quality=round((source.reliability if source else 0), 2),
        evidence_count=0,
        evidence=[],
        risk_note="No allergies selected; ranked using general restaurant signals.",
        reason=(
            f"{place.rating:.1f} Google rating with {(place.user_rating_count or 0):,} reviews."
            if place.rating is not None
            else "Ranked by distance and restaurant relevance; Google rating is unavailable."
        ),
        next_action="Open the restaurant details or menu to explore this option.",
        intent_match=intent_match,
        intent_note=intent_note,
    )


def general_discovery_score(place: PlaceListItem, center: LatLng | None, menu_available: bool) -> float:
    rating_score = ((place.rating or 0) / 5) * 45
    review_score = min(25, math.log10((place.user_rating_count or 0) + 1) * 8)
    distance_score = 10.0 if center is None else max(0, 15 - distance_miles(place.location, center) * 5)
    place_type = (place.primary_type or "").lower()
    relevance_score = 10 if any(term in place_type for term in ("restaurant", "cafe", "bakery", "food", "bar")) else 0
    menu_score = 5 if menu_available else 0
    return round(min(100, rating_score + review_score + distance_score + relevance_score + menu_score), 2)


def general_discovery_label(place: PlaceListItem) -> str:
    if (place.rating or 0) >= 4.5 and (place.user_rating_count or 0) >= 100:
        return "Popular nearby option"
    if (place.rating or 0) >= 4.0:
        return "Well-rated nearby option"
    return "Nearby restaurant option"


def build_place_suggestion(
    place: PlaceListItem,
    payload: NearbySuggestionRequest,
    source: MenuSource | None,
) -> NearbyPlaceSuggestion:
    place = place.model_copy(update={"name": display_place_name(place.name)})
    intent_match, intent_note = candidate_intent_match(place, payload.query)
    try:
        cached_reviews = load_cached_reviews(place.id)
    except Exception:  # Review storage should not prevent menu-based ranking.
        cached_reviews = []
    confirmed_review_risk = has_confirmed_allergen_risk_review(
        [review.text for review in cached_reviews],
        payload.allergens,
    )
    fit = score_restaurant_menu(
        source,
        payload.allergens,
        confirmed_allergen_review=confirmed_review_risk,
        restaurant_context=f"{place.name} {place.primary_type or ''}",
    )

    evidence: list[HybridSearchResult] = []
    if source and fit.menu_item_count > 0:
        search_request = HybridSearchRequest(
            query=payload.question,
            restaurant_id=place.id,
            allergens=payload.allergens,
            top=payload.top_evidence,
        )
        search_response = invoke_traced_runnable(
            name="AllerNav Azure Search Retriever",
            value=search_request,
            func=hybrid_search_menu,
            metadata={
                "restaurant_id": place.id,
                "source_url": source.source_url,
                "item_count": fit.menu_item_count,
                "retrieval_mode": "hybrid",
                "allergens": [allergen.value for allergen in payload.allergens],
            },
        )
        evidence = search_response.results
    update_current_trace_metadata(
        restaurant_id=place.id,
        item_count=fit.menu_item_count,
        restaurant_fit_score=fit.score if fit.menu_item_count > 0 else None,
        restaurant_fit_label=fit.label,
        avoid_count=fit.avoid_count,
        needs_check_count=fit.needs_check_count,
        possible_lower_risk_count=fit.possible_lower_risk_count,
        insufficient_info_count=fit.insufficient_info_count,
        possible_item_names=list(fit.possible_item_names),
        avoid_item_names=list(fit.avoid_item_names),
        evidence_quality=fit.evidence_quality,
        retrieval_mode=(evidence[0].retrieval_mode if evidence else "hybrid_no_results"),
        allergens=[allergen.value for allergen in payload.allergens],
    )
    return NearbyPlaceSuggestion(
        place=place,
        confidence=round(fit.score / 100, 2) if fit.score is not None else 0,
        evidence_status="scanned" if fit.menu_item_count > 0 else "scan_needed",
        restaurant_fit_score=fit.score if fit.menu_item_count > 0 else None,
        restaurant_fit_label=fit.label,
        menu_item_count=fit.menu_item_count,
        matched_allergen_items=fit.avoid_count,
        avoid_count=fit.avoid_count,
        needs_check_count=fit.needs_check_count,
        possible_lower_risk_count=fit.possible_lower_risk_count,
        insufficient_info_count=fit.insufficient_info_count,
        evidence_quality=fit.evidence_quality,
        evidence_count=len(evidence),
        evidence=evidence,
        risk_note=fit.reason,
        reason=fit.reason,
        next_action=fit.next_action,
        intent_match=intent_match,
        intent_note=intent_note,
    )


def suggestion_rank_key(suggestion: NearbyPlaceSuggestion) -> tuple[int, int, float]:
    intent_rank = 1 if suggestion.intent_match is not False else 0
    if suggestion.evidence_status == "scanned":
        return (intent_rank, 2, suggestion.restaurant_fit_score or 0)
    return (intent_rank, 1, suggestion.scan_priority_score or 0)


def requested_intent(query: str) -> tuple[str | None, tuple[str, ...]]:
    normalized = query.lower().replace("_", " ")
    for label, aliases in INTENT_TERMS.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", normalized) for alias in aliases):
            return label, aliases
    return None, ()


def candidate_intent_match(place: PlaceListItem, query: str) -> tuple[bool | None, str | None]:
    label, aliases = requested_intent(query)
    if not label:
        return None, None
    haystack = f"{place.name} {place.primary_type or ''}".lower().replace("_", " ")
    matched = any(re.search(rf"\b{re.escape(alias)}\b", haystack) for alias in aliases)
    return matched, (
        f"Matches the requested {label} intent."
        if matched
        else f"Does not clearly match the requested {label} intent."
    )


def assign_scan_priorities(
    suggestions: list[NearbyPlaceSuggestion],
    center: LatLng | None,
) -> list[NearbyPlaceSuggestion]:
    scored = [
        (item, scan_priority_score(item.place, center))
        for item in suggestions
        if item.evidence_status != "scanned"
    ]
    scored.sort(key=lambda entry: (entry[0].intent_match is not False, entry[1]), reverse=True)
    priorities = {
        item.place.id: (rank, score)
        for rank, (item, score) in enumerate(scored, start=1)
    }
    return [
        item.model_copy(
            update={
                "scan_priority_rank": priorities[item.place.id][0],
                "scan_priority_score": priorities[item.place.id][1],
            }
        )
        if item.place.id in priorities
        else item
        for item in suggestions
    ]


def scan_priority_score(place: PlaceListItem, center: LatLng | None) -> float:
    rating_score = ((place.rating or 0) / 5) * 45
    review_score = min(25, math.log10((place.user_rating_count or 0) + 1) * 8)
    distance_score = 10.0 if center is None else max(0, 20 - distance_miles(place.location, center) * 5)
    place_type = (place.primary_type or "").lower()
    relevance_score = 10 if any(term in place_type for term in ("restaurant", "cafe", "bakery", "food", "bar")) else 0
    return round(min(100, rating_score + review_score + distance_score + relevance_score), 2)


def distance_miles(left: LatLng, right: LatLng) -> float:
    radius = 3958.8
    lat1, lat2 = math.radians(left.lat), math.radians(right.lat)
    lat_delta = math.radians(right.lat - left.lat)
    lng_delta = math.radians(right.lng - left.lng)
    value = math.sin(lat_delta / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(lng_delta / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


async def start_background_scans(
    suggestions: list[NearbyPlaceSuggestion],
) -> list[NearbyPlaceSuggestion]:
    eligible = [
        suggestion
        for suggestion in sorted(suggestions, key=lambda item: item.scan_priority_rank or 999)
        if suggestion.evidence_status == "scan_needed" and suggestion.place.website_url
    ][:2]
    if not eligible:
        return suggestions

    from .service import create_menu_refresh_job

    async def start_scan(suggestion: NearbyPlaceSuggestion) -> Any:
        try:
            return await create_menu_refresh_job(
                suggestion.place.id,
                restaurant_name=suggestion.place.name,
                website_url=suggestion.place.website_url,
                # Nearby comparisons must return pollable jobs promptly. Inline
                # discovery can consume the entire HTTP budget per restaurant.
                allow_local_fallback=False,
            )
        except Exception as exc:  # Keep one provider failure from failing the whole nearby request.
            return exc

    jobs = await asyncio.gather(
        *(start_scan(suggestion) for suggestion in eligible),
        return_exceptions=True,
    )
    updates = {suggestion.place.id: job for suggestion, job in zip(eligible, jobs, strict=True)}
    return [apply_scan_job(suggestion, updates.get(suggestion.place.id)) for suggestion in suggestions]


def apply_scan_job(suggestion: NearbyPlaceSuggestion, job: Any | None) -> NearbyPlaceSuggestion:
    if job is None:
        return suggestion
    if isinstance(job, BaseException):
        return suggestion.model_copy(
            update={
                "evidence_status": "scan_failed",
                "scan_job_id": None,
                "reason": "The background menu scan could not be started.",
                "risk_note": "The background menu scan could not be started.",
                "next_action": "Open the restaurant and retry its menu scan.",
            }
        )
    if job.status == "failed":
        return suggestion.model_copy(
            update={
                "evidence_status": "scan_failed",
                "scan_job_id": job.id,
                "reason": "The background menu scan could not be started.",
                "risk_note": "The background menu scan could not be started.",
                "next_action": "Open the restaurant and retry its menu scan.",
            }
        )
    return suggestion.model_copy(
        update={
            "evidence_status": "scan_running",
            "scan_job_id": job.id,
            "reason": "A background menu scan is running.",
            "risk_note": "A background menu scan is running.",
            "next_action": "Check again after the menu scan finishes.",
        }
    )


def build_nearby_summary(
    payload: NearbySuggestionRequest,
    candidate_count: int,
    scanned: list[NearbyPlaceSuggestion],
    scan_needed: list[NearbyPlaceSuggestion],
    top_scan_candidates: list[NearbyPlaceSuggestion],
) -> str:
    if candidate_count == 0:
        return "I could not find nearby candidates in the current map area."
    intent_label, _aliases = requested_intent(payload.query)
    all_suggestions = scanned + scan_needed
    if intent_label and all_suggestions and not any(item.intent_match for item in all_suggestions):
        return (
            f"I found restaurants nearby, but they do not strongly match {intent_label}. "
            f"Try searching this area again with {payload.query}."
        )
    if intent_label:
        matching_scanned = [item for item in scanned if item.intent_match]
        matching_scan_needed = [item for item in scan_needed if item.intent_match]
        if matching_scanned:
            scanned = matching_scanned
        elif matching_scan_needed:
            running = [item.place.name for item in matching_scan_needed if item.evidence_status == "scan_running"]
            if running:
                return (
                    f"I found {len(matching_scan_needed)} {intent_label} candidate"
                    f"{'s' if len(matching_scan_needed) != 1 else ''}. Menu scans are running for "
                    f"{format_names(running)} before allergy comparison."
                )
            return (
                f"I found {len(matching_scan_needed)} {intent_label} candidate"
                f"{'s' if len(matching_scan_needed) != 1 else ''}, but they need menu scans before allergy comparison."
            )
    if candidate_count == 1:
        only = scanned[0] if scanned else scan_needed[0]
        if only.evidence_status == "scanned":
            return (
                f"Best current option: {only.place.name} scores {only.restaurant_fit_score}. "
                f"I found {bucket_reason(only)}. Search this area to compare more restaurants."
            )
        return f"Only one candidate was available. Start by scanning {only.place.name} before allergy comparison."

    place_word = "restaurants"
    if not scanned:
        running = [item.place.name for item in scan_needed if item.evidence_status == "scan_running"]
        if running:
            return (
                f"I found {candidate_count} nearby {place_word} in this area. I started menu scans for "
                f"{format_names(running)}. I'll compare allergy fit once scanned evidence is ready."
            )
        names = [item.place.name for item in top_scan_candidates]
        return (
            f"I found {candidate_count} nearby {place_word} in this area. None have scanned menu evidence yet, "
            f"so I can't compare allergy fit. Start by scanning the top {len(names)} candidates: {format_names(names)}."
        )

    top = scanned[0]
    remaining = candidate_count - len(scanned)
    scan_sentence = (
        f" {remaining} nearby place{'s' if remaining != 1 else ''} still "
        f"{'need' if remaining != 1 else 'needs'} menu scans."
        if remaining
        else ""
    )
    return (
        f"Best current option: {top.place.name} scores {top.restaurant_fit_score}. "
        f"I found {bucket_reason(top)}."
        f"{scan_sentence}"
    )


def display_place_name(name: str | None) -> str:
    cleaned = (name or "").strip()
    if not cleaned or cleaned.lower() == "selected place":
        return "This restaurant"
    return cleaned


def bucket_reason(suggestion: NearbyPlaceSuggestion) -> str:
    possible = (
        f"{suggestion.possible_lower_risk_count} item"
        f"{'s' if suggestion.possible_lower_risk_count != 1 else ''} to ask about"
    )
    if suggestion.avoid_count:
        return f"{possible} and {suggestion.avoid_count} direct allergen match{'es' if suggestion.avoid_count != 1 else ''}"
    return f"{possible} and no direct selected-allergen matches"


def format_names(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else "the top candidate"
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def trace_nearby_result(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
    retrieval_mode: str,
    top_scan_candidates: list[NearbyPlaceSuggestion],
) -> None:
    scanned = [item for item in suggestions if item.evidence_status == "scanned"]
    top = scanned[0] if scanned else (suggestions[0] if suggestions else None)
    scan_started_count = sum(item.evidence_status == "scan_running" for item in suggestions)
    flow_stage = (
        "candidate_discovery"
        if not suggestions
        else "ranked_comparison"
        if scanned
        else "scan_started"
        if scan_started_count
        else "scan_needed"
    )
    update_current_trace_metadata(
        candidate_count=len(suggestions),
        scanned_candidate_count=len(scanned),
        scan_needed_count=len(suggestions) - len(scanned),
        scan_started_count=scan_started_count,
        top_scan_candidates=[item.place.name for item in top_scan_candidates],
        top_scan_priority_score=top.scan_priority_score if top and not scanned else None,
        flow_stage=flow_stage,
        selected_allergens=[allergen.value for allergen in payload.allergens],
        search_intent=payload.query,
        intent_match=top.intent_match if top else None,
        top_ranked_place=top.place.name if top else None,
        restaurant_fit_score=top.restaurant_fit_score if top else None,
        restaurant_fit_label=top.restaurant_fit_label if top else None,
        avoid_count=top.avoid_count if top else 0,
        needs_check_count=top.needs_check_count if top else 0,
        possible_lower_risk_count=top.possible_lower_risk_count if top else 0,
        insufficient_info_count=top.insufficient_info_count if top else 0,
        evidence_quality=top.evidence_quality if top else 0,
        retrieval_mode=retrieval_mode,
        allow_background_scan=payload.allow_background_scan,
        safety_gate="verify_or_abstain",
    )


def build_missing_information(suggestions: list[NearbyPlaceSuggestion]) -> list[str]:
    missing: list[str] = []
    if not suggestions:
        return ["No nearby candidates were available for retrieval."]
    if any(item.menu_item_count == 0 for item in suggestions):
        missing.append("Some nearby places do not have scanned menu evidence yet.")
    if any(item.menu_item_count > 0 for item in suggestions):
        missing.append("Menus rarely confirm cross-contact controls, shared fryer use, or ingredient substitutions.")
    return missing


def build_recommended_questions(allergens: list[AllergyTag]) -> list[str]:
    allergen_text = ", ".join(allergen.value.replace("_", " ") for allergen in allergens) or "my allergens"
    return [
        f"Can you confirm whether this dish contains {allergen_text} directly or through sauces, marinades, or garnish?",
        "Is it prepared on shared surfaces, grills, fryers, or utensils?",
        "Can staff check the current ingredient label or kitchen prep notes before I order?",
    ]


async def generate_nearby_answer(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
    evidence: list[HybridSearchResult],
    missing_information: list[str],
    questions: list[str],
) -> str:
    metadata = explanation_trace_metadata(payload, suggestions, evidence)

    async def explain(_input: dict[str, str]) -> str:
        llm_answer = await generate_azure_openai_answer(
            payload,
            suggestions,
            evidence,
            missing_information,
            questions,
            metadata=metadata,
        )
        if not llm_answer:
            llm_answer = generate_gemini_answer(payload, suggestions, evidence, missing_information, questions)
        answer = llm_answer or deterministic_answer(payload, suggestions, missing_information, questions)
        update_current_trace_metadata(
            retrieval_mode="hybrid_keyword_semantic",
            safety_gate="verify_or_abstain",
            item_count=sum(suggestion.menu_item_count for suggestion in suggestions),
        )
        return answer

    return await ainvoke_traced_runnable(
        name="AllerNav RAG Explanation",
        value={"question": payload.question},
        func=explain,
        metadata=metadata,
    )


def azure_openai_chat_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in (
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_CHAT_DEPLOYMENT",
            "AZURE_OPENAI_CHAT_API_VERSION",
        )
    )


def explanation_trace_metadata(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
    evidence: list[HybridSearchResult],
) -> dict[str, Any]:
    return {
        "restaurant_id": suggestions[0].place.id if len(suggestions) == 1 else None,
        "source_url": evidence[0].source_url if evidence else None,
        "item_count": sum(suggestion.menu_item_count for suggestion in suggestions),
        "retrieval_mode": "hybrid_keyword_semantic",
        "allergens": [allergen.value for allergen in payload.allergens],
        "safety_gate": "verify_or_abstain",
    }


async def generate_azure_openai_answer(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
    evidence: list[HybridSearchResult],
    missing_information: list[str],
    questions: list[str],
    *,
    metadata: dict[str, Any],
) -> str | None:
    if not azure_openai_chat_configured() or not suggestions:
        return None
    try:
        from langchain_openai import AzureChatOpenAI

        model = AzureChatOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", "").strip(),
            api_key=os.getenv("AZURE_OPENAI_API_KEY", "").strip(),
            azure_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "").strip(),
            api_version=os.getenv("AZURE_OPENAI_CHAT_API_VERSION", "").strip(),
            temperature=0.2,
            max_retries=1,
            timeout=14,
        )
        response = await model.ainvoke(
            explanation_messages(payload, suggestions, evidence, missing_information, questions),
            config=langchain_run_config(
                name="AllerNav Azure OpenAI RAG Explanation",
                tags=["allernav", "rag", "azure-openai"],
                metadata=metadata,
            ),
        )
    except Exception:  # noqa: BLE001 - provider failure must preserve the fallback chain
        return None
    return clean_llm_answer(extract_langchain_text(response))


@traceable(name="Gemini Nearby RAG Explanation", run_type="llm")
def generate_gemini_answer(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
    evidence: list[HybridSearchResult],
    missing_information: list[str],
    questions: list[str],
) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or not suggestions:
        return None

    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
    messages = explanation_messages(payload, suggestions, evidence, missing_information, questions)
    req = request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        method="POST",
        data=json.dumps(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": f"{messages[0][1]}\n\n{messages[1][1]}"}],
                    }
                ],
                "generationConfig": {"temperature": 0.2},
            }
        ).encode("utf-8"),
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("x-goog-api-key", api_key)
    try:
        with request.urlopen(req, timeout=14) as response:
            payload_json = json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
    except (error.HTTPError, error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    return clean_llm_answer(extract_gemini_text(payload_json))


def explanation_prompt(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
    evidence: list[HybridSearchResult],
    missing_information: list[str],
    questions: list[str],
) -> dict[str, Any]:
    return {
        "task": "Suggest nearby restaurants to evaluate for allergy-aware dining decision support.",
        "rules": [
            "Never use the word safe or claim that a place or dish has no allergy risk.",
            "Use cautious language: possible lower-risk, needs verification, ask staff, insufficient evidence.",
            "Use menu evidence as stronger evidence than reviews.",
            "Cite evidence ids like [E1] when discussing menu facts.",
            "Do not invent menu items, ingredients, policies, or reviews.",
            "If no menu evidence was retrieved, say menu refresh or OCR is needed before ranking candidates.",
            "Do not list internal confidence percentages or item counts unless they directly explain an evidence gap.",
        ],
        "question": payload.question,
        "selected_allergens": [allergen.value for allergen in payload.allergens],
        "places": [
            {
                "name": suggestion.place.name,
                "rating": suggestion.place.rating,
                "menu_item_count": suggestion.menu_item_count,
                "confidence": suggestion.confidence,
                "risk_note": suggestion.risk_note,
                "evidence_ids": [result.id for result in suggestion.evidence],
            }
            for suggestion in suggestions
        ],
        "evidence": [
            {
                "id": f"E{index + 1}",
                "document_id": result.id,
                "restaurant_name": result.restaurant_name,
                "dish_name": result.dish_name,
                "source_type": result.source_type.value,
                "text": result.raw_text,
                "matched_allergens": [allergen.value for allergen in result.matched_allergens],
            }
            for index, result in enumerate(evidence[:8])
        ],
        "missing_information": missing_information,
        "recommended_questions": questions,
        "output": "Maximum 80 words. One short paragraph and, only when useful, up to 2 concise bullets. No JSON.",
    }


def explanation_messages(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
    evidence: list[HybridSearchResult],
    missing_information: list[str],
    questions: list[str],
) -> list[tuple[str, str]]:
    prompt = explanation_prompt(payload, suggestions, evidence, missing_information, questions)
    return [
        (
            "system",
            "You are AllerNav, an evidence-backed dining decision-support assistant. "
            "Follow the supplied safety rules and cite only supplied evidence ids.",
        ),
        ("human", json.dumps(prompt)),
    ]


def extract_langchain_text(response: Any) -> str | None:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        joined = "".join(parts).strip()
        return joined or None
    return None


def extract_gemini_text(payload: dict[str, Any]) -> str | None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if text:
            return text
    return None


def clean_llm_answer(answer: str | None) -> str | None:
    if not answer:
        return None
    cleaned = answer.strip()
    if re.search(r"\bsafe\b", cleaned, flags=re.IGNORECASE):
        return None
    return cleaned


def deterministic_answer(
    payload: NearbySuggestionRequest,
    suggestions: list[NearbyPlaceSuggestion],
    missing_information: list[str],
    questions: list[str],
) -> str:
    if not suggestions:
        return (
            "I could not retrieve enough nearby place or menu evidence to suggest candidates. "
            "Try searching a specific cuisine or opening a place so AllerNav can capture menu evidence."
        )

    allergen_text = ", ".join(allergen.value.replace("_", " ") for allergen in payload.allergens) or "your allergens"
    place_names = ", ".join(suggestion.place.name for suggestion in suggestions[:3])
    has_menu_evidence = any(suggestion.menu_item_count > 0 or suggestion.evidence for suggestion in suggestions)
    if not has_menu_evidence:
        return (
            f"I found nearby {allergen_text} verification candidates, but none have stored menu evidence yet: "
            f"{place_names}. Open a place and refresh its menu or OCR source before treating it as a useful allergy lead. "
            f"Ask staff: {questions[0]}"
        )

    lines = [f"For {allergen_text}, use these as verification leads, not verified choices:"]
    for suggestion in suggestions[:3]:
        evidence_count = len(suggestion.evidence)
        if evidence_count > 0:
            evidence_text = f"{evidence_count} cited menu fragment{'s' if evidence_count != 1 else ''}"
        elif suggestion.menu_item_count > 0:
            evidence_text = "stored menu items, but weak retrieval for this question"
        else:
            evidence_text = "no stored menu evidence yet"
        lines.append(f"- {suggestion.place.name}: {evidence_text}. {suggestion.risk_note}")
    if missing_information:
        lines.append("Gap: " + missing_information[0])
    lines.append("Ask staff: " + questions[0])
    return "\n".join(lines)
