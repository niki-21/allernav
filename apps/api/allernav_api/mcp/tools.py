"""Typed MCP adapters. All retrieval and scoring stays in shared services."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

from langsmith import tracing_context
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .. import agent_service, service
from ..google_places import GooglePlacesClient, GooglePlacesError
from ..models import (
    AllergyProfile, AllergyTag, AnalyzeRestaurantRequest, HybridSearchRequest,
    HybridSearchResponse, LatLng, PlaceMenu, RecommendationResult,
    RecommendDishesRequest, RestaurantContext, SearchRequest, SearchResponse,
)
from .serializers import RestaurantDetails, restaurant_details

RestaurantId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]
RestaurantName = Annotated[str, Field(min_length=1, max_length=200)]
Query = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
Allergens = Annotated[list[AllergyTag], Field(max_length=9)]
RequiredAllergens = Annotated[list[AllergyTag], Field(min_length=1, max_length=9)]
Preference = Annotated[str, Field(min_length=1, max_length=80)]
T = TypeVar("T")
logger = logging.getLogger(__name__)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


async def invoke(tool_name: str, operation: Callable[[], Awaitable[T]], *,
                 restaurant_id: str | None = None, allergen_count: int = 0) -> T:
    """Keep blocking legacy provider work off the MCP loop; never log its payloads."""
    def run() -> T:
        # Existing graph tracing captures profiles and evidence. MCP only logs metadata.
        with tracing_context(enabled=False):
            return asyncio.run(operation())

    logger.info("interface=mcp tool_name=%s restaurant_id=%s allergen_count=%d",
                tool_name, restaurant_id, allergen_count)
    try:
        return await asyncio.to_thread(run)
    except GooglePlacesError:
        raise ToolError("external_provider_failure: Restaurant provider could not complete the request. Check the restaurant ID and retry.") from None
    except Exception:
        # Do not include exception text: provider errors may contain credentials or URLs.
        raise ToolError("service_unavailable: AllerNav could not complete this request. Retry later or check server configuration.") from None


def places_client() -> GooglePlacesClient:
    client = GooglePlacesClient()
    if not client.api_key:
        raise ToolError("provider_not_configured: Google Places credentials are required for this tool.")
    return client


def register_tools(server: MCPServer) -> None:
    @server.tool(annotations=READ_ONLY)
    async def search_restaurants(
        query: Query,
        latitude: Annotated[float, Field(ge=-90, le=90)] | None = None,
        longitude: Annotated[float, Field(ge=-180, le=180)] | None = None,
        max_results: Annotated[int, Field(ge=1, le=20)] = 12,
    ) -> SearchResponse:
        """Find restaurants through Google Places. Use before requesting details or analysis.
        Ratings describe reviews, not allergy safety. Read-only; may incur provider charges.
        Supply both coordinates or neither; omission uses AllerNav's configured service default.
        """
        if (latitude is None) != (longitude is None):
            raise ToolError("invalid_coordinates: Supply both latitude and longitude, or neither.")
        center = LatLng(lat=latitude, lng=longitude) if latitude is not None else None
        client = places_client()
        return await invoke("search_restaurants", lambda: service.search_places_service(
            SearchRequest(query=query, center=center, max_results=max_results), client))

    @server.tool(annotations=READ_ONLY)
    async def get_restaurant_details(restaurant_id: RestaurantId, allergens: Allergens = []) -> RestaurantDetails:
        """Get restaurant identity, website, stored-menu availability and optional allergen analysis.
        Reviews are not proof of safety. Read-only; queries Google Places but never refreshes menus.
        """
        client = places_client()
        value = await invoke("get_restaurant_details", lambda: service.get_place_details_service(
            restaurant_id, allergens, client), restaurant_id=restaurant_id, allergen_count=len(allergens))
        return restaurant_details(value)

    @server.tool(annotations=READ_ONLY)
    async def get_restaurant_menu(restaurant_id: RestaurantId, allergens: Allergens = []) -> PlaceMenu:
        """Read stored dishes, prices when available, allergen labels and source provenance.
        Missing menus remain missing; absence of allergen terms does not prove safety.
        Read-only: does not crawl, refresh or upload menus.
        """
        return await invoke("get_restaurant_menu", lambda: service.get_place_menu_service(
            restaurant_id, allergens), restaurant_id=restaurant_id, allergen_count=len(allergens))

    @server.tool(annotations=READ_ONLY)
    async def analyze_restaurant(
        restaurant_id: RestaurantId, allergens: RequiredAllergens,
        restaurant_name: RestaurantName | None = None,
        sensitivity: Preference = "careful", preparation_preference: Preference = "verify",
    ) -> RecommendationResult:
        """Analyze stored menu evidence through LangGraph and the deterministic allergen engine.
        Use for overall risk, dish results, provenance, missing information and staff questions.
        Preserves the safety gate and insufficient_evidence; never guarantees food is safe.
        Read-only: no menu crawling. At least one selected allergen is required.
        """
        payload = AnalyzeRestaurantRequest(
            restaurant_id=restaurant_id, restaurant_name=restaurant_name,
            profile=AllergyProfile(allergens=allergens, sensitivity=sensitivity, prep_preference=preparation_preference),
            context=RestaurantContext(restaurant_id=restaurant_id, restaurant_name=restaurant_name),
        )
        return await invoke("analyze_restaurant", lambda: agent_service.analyze_restaurant_service(payload),
                            restaurant_id=restaurant_id, allergen_count=len(allergens))

    @server.tool(annotations=READ_ONLY)
    async def search_menu_evidence(
        restaurant_id: RestaurantId, query: Query, allergens: Allergens,
        top_k: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> HybridSearchResponse:
        """Retrieve relevant menu evidence with sources and matched allergens for one restaurant.
        Uses existing Azure hybrid retrieval or local fallback when Azure is unconfigured.
        Relevance is not safety confidence; an empty result means no matching evidence.
        Read-only; configured providers may incur charges. No crawling or index writes.
        """
        payload = HybridSearchRequest(restaurant_id=restaurant_id, query=query, allergens=allergens, top=top_k)
        return await invoke("search_menu_evidence", lambda: service.hybrid_search_service(payload),
                            restaurant_id=restaurant_id, allergen_count=len(allergens))

    @server.tool(annotations=READ_ONLY)
    async def recommend_dishes(
        restaurant_id: RestaurantId, allergens: RequiredAllergens,
        restaurant_name: RestaurantName | None = None,
    ) -> RecommendationResult:
        """Return stored dish assessments and cautions through the existing recommendation workflow.
        Lower risk still needs verification; retain avoid and insufficient-evidence results and staff questions.
        Read-only; no crawling. Does not certify dishes as allergy-safe.
        """
        payload = RecommendDishesRequest(
            profile=AllergyProfile(allergens=allergens),
            context=RestaurantContext(restaurant_id=restaurant_id, restaurant_name=restaurant_name),
        )
        return await invoke("recommend_dishes", lambda: agent_service.recommend_dishes_service(payload),
                            restaurant_id=restaurant_id, allergen_count=len(allergens))
