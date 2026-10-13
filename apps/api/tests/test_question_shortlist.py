"""Question-triggered discovery is bounded; cached evidence and risk rules are reused."""
import asyncio
from unittest.mock import AsyncMock, patch

from allernav_api.models import (
    AllergyTag, HybridSearchResponse, LatLng, MenuItem, MenuRefreshJob,
    MenuSection, MenuSource, NearbySuggestionRequest, PlaceListItem, SourceType,
)
from allernav_api.rag_service import (
    deterministic_answer, explanation_prompt, scan_priority_score, suggest_nearby_places_service,
)


def candidates(count=8):
    return [PlaceListItem(id=f"p{i}", name=f"Restaurant {i}", location=LatLng(lat=25, lng=55),
                         rating=4.9 - i * .1, user_rating_count=1000,
                         website_url=f"https://p{i}.example") for i in range(count)]


def request(**changes):
    values = dict(question="Where should I eat?", center=LatLng(lat=25, lng=55),
                  candidate_places=candidates(), max_places=8, allergens=[AllergyTag.DAIRY],
                  conversation=[{"role":"user", "content":"Where should I eat?"}])
    return NearbySuggestionRequest(**(values | changes))


def run(payload, source=None):
    async def job(place_id, **kwargs):
        return MenuRefreshJob(id=f"job-{place_id}", place_id=place_id, status="queued", message="Queued", created_at="2026-10-08T00:00:00Z")
    with patch("allernav_api.rag_service.load_menu_source", return_value=source) as load, patch(
        "allernav_api.rag_service.load_cached_reviews", return_value=[]
    ), patch("allernav_api.rag_service.hybrid_search_menu", return_value=HybridSearchResponse(query="food")), patch(
        "allernav_api.service.create_menu_refresh_job", new=AsyncMock(side_effect=job)
    ) as refresh, patch("allernav_api.rag_service.generate_azure_openai_answer", new=AsyncMock(return_value=None)), patch(
        "allernav_api.rag_service.generate_gemini_answer", return_value=None
    ):
        result = asyncio.run(suggest_nearby_places_service(payload))
        return result, load.call_args_list, refresh.await_args_list


def test_shortlist_is_ranked_before_loading_menus_and_capped_at_five():
    result, loads, scans = run(request(candidate_places=list(reversed(candidates())), allow_background_scan=True))
    assert {item.place.id for item in result.places} == {f"p{i}" for i in range(5)}
    assert {call.args[0] for call in loads} == {f"p{i}" for i in range(5)}
    assert len(scans) == 5
    assert len(result.scan_job_ids) == 5
    assert "checking menus at 5" in result.answer


def test_review_count_matters_more_than_a_tiny_perfect_rating():
    sparse = candidates(1)[0].model_copy(update={"rating":5, "user_rating_count":3})
    established = sparse.model_copy(update={"rating":4.7, "user_rating_count":800})
    assert scan_priority_score(established, None) > scan_priority_score(sparse, None)


def test_general_dining_also_scans_shortlist_without_inventing_allergy_scores():
    result, _, scans = run(request(allergens=[], allow_background_scan=True))
    assert len(scans) == 5
    assert result.ranking_mode == "general_discovery"
    assert all(item.restaurant_fit_score is None for item in result.places)
    assert result.recommended_questions == []


def test_stored_menus_are_reused_and_dishes_keep_prices_sources_and_risk_labels():
    source = MenuSource(source_type=SourceType.OFFICIAL_MENU, source_url="https://menu.example/menu", reliability=.95,
                        sections=[MenuSection(title="Lunch", items=[MenuItem(name="Cheese pasta", description="Milk, butter and wheat pasta", price="AED 40")])])
    payload = request(allow_background_scan=True)
    result, _, scans = run(payload, source)
    assert scans == []
    dish = result.places[0].menu_dishes[0]
    assert dish.price == "AED 40"
    assert dish.source_url == source.source_url
    assert dish.risk_label == "avoid"
    prompt = explanation_prompt(payload, result.places, result.evidence, result.missing_information, result.recommended_questions)
    assert prompt["places"][0]["menu_dishes"][0]["risk_label"] == "avoid"
    assert "Cheese pasta" not in result.answer  # The deterministic fallback must not recommend it.


def test_general_answer_contains_actual_dishes_and_available_prices():
    source = MenuSource(sections=[MenuSection(title="Lunch", items=[MenuItem(name="Rice Bowl", price="AED 30")])])
    result, _, scans = run(request(allergens=[], allow_background_scan=True), source)
    assert not scans
    assert "Rice Bowl — AED 30" in result.answer
    assert "Restaurant 0" in result.answer


def test_followup_refresh_reads_results_without_starting_more_jobs():
    result, _, scans = run(request(allow_background_scan=False))
    assert not scans
    assert not result.scan_job_ids
    assert all(item.restaurant_fit_score is None for item in result.places)


def test_clearest_evidence_means_eligible_dishes_not_allergen_examples():
    source = MenuSource(source_type=SourceType.OFFICIAL_MENU, reliability=.95,
        sections=[MenuSection(title="Dinner", items=[
            MenuItem(name="Grilled Salmon", description="Salmon fillet", confirmed_allergens=[AllergyTag.FISH]),
            MenuItem(name="Blue Cheese Wings", description="Chicken with blue cheese", confirmed_allergens=[AllergyTag.DAIRY]),
            MenuItem(name="Steamed Rice", description="Rice and water, no sauces"),
        ])])
    result, _, _ = run(request(question="Which nearby restaurants have the clearest menu evidence for my allergies?", allergens=[AllergyTag.FISH, AllergyTag.DAIRY]), source)
    assert "Grilled Salmon" not in result.answer
    assert "Blue Cheese Wings" not in result.answer
    assert "Steamed Rice" in result.answer
    assert "confirm" in result.answer.lower()


def test_all_avoid_menu_does_not_become_a_recommendation():
    source = MenuSource(sections=[MenuSection(title="Dinner", items=[
        MenuItem(name="Salmon", description="Fish fillet", confirmed_allergens=[AllergyTag.FISH])])])
    result, _, _ = run(request(question="Which nearby restaurants have the clearest evidence?", allergens=[AllergyTag.FISH]), source)
    assert "Salmon" not in result.answer
    assert "does not support a suitable recommendation" in result.answer
