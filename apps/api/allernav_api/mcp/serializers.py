"""Agent-facing projections; risk and evidence models remain authoritative."""
from pydantic import BaseModel

from ..models import AllergyTag, PlaceDetailsResponse, PlaceListItem, RecommendationResult


class RestaurantDetails(BaseModel):
    restaurant: PlaceListItem
    selected_allergens: list[AllergyTag]
    menu_status: str
    menu_available: bool
    menu_source_url: str | None
    evidence_summary: str
    analysis: RecommendationResult | None
    rating_context: str = "Google review rating, not an allergy-safety rating."


def restaurant_details(value: PlaceDetailsResponse) -> RestaurantDetails:
    """Exclude review authors, community profiles, diagnostics and provider payloads."""
    menu = value.menu
    return RestaurantDetails(
        restaurant=PlaceListItem(
            id=value.id, name=value.name, address=value.address, location=value.location,
            rating=value.rating, user_rating_count=value.user_rating_count,
            primary_type=value.primary_type, website_url=value.website_uri,
        ),
        selected_allergens=value.selected_allergens,
        menu_status=menu.status if menu else "missing",
        menu_available=bool(menu and any(section.items for section in menu.sections)),
        menu_source_url=menu.source_url if menu else None,
        evidence_summary=(value.agent_recommendation.summary if value.agent_recommendation
                          else "No allergen analysis requested. Menu availability does not establish safety."),
        analysis=value.agent_recommendation,
    )
