"""Exercise the public MCP contract with the official in-process client; no paid APIs."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from allernav_api import agent_graph, azure_search, service
from allernav_api.google_places import GooglePlacesError
from allernav_api.mcp import tools
from allernav_api.mcp.server import create_server
from allernav_api.models import (
    AllergyTag, LatLng, MenuItem, MenuSection, MenuSource, PlaceListItem,
    PlaceMenu, SearchResponse, SourceType,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "AZURE_SEARCH_ENDPOINT",
                "AZURE_SEARCH_API_KEY", "GOOGLE_MAPS_API_KEY", "GOOGLE_PLACES_API_KEY",
                "NEXT_PUBLIC_GOOGLE_MAPS_API_KEY", "LANGSMITH_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("ALLERNAV_MENU_DB", str(tmp_path / "menu.sqlite"))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network or menu refresh")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr(agent_graph, "ingest_menu_from_website", forbidden)
    monkeypatch.setattr(agent_graph, "get_fixture_context", forbidden)


def call(name, arguments):
    async def run():
        async with Client(create_server()) as client:
            return await client.call_tool(name, arguments)
    return asyncio.run(run())


def output(result):
    assert not result.is_error, result.content
    return result.structured_content


def test_discovery_and_schema():
    async def run():
        async with Client(create_server()) as client:
            return await client.list_tools()
    listing = asyncio.run(run())
    registered = {tool.name: tool for tool in listing.tools}
    assert set(registered) == {"search_restaurants", "get_restaurant_details", "get_restaurant_menu",
                               "analyze_restaurant", "search_menu_evidence", "recommend_dishes"}
    for tool in registered.values():
        assert tool.output_schema
        assert tool.annotations.read_only_hint is True
    schema = registered["search_restaurants"].input_schema
    assert schema["properties"]["max_results"]["maximum"] == 20
    assert "query" in schema["required"]


def test_search_reuses_service(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-placeholder")
    mock = AsyncMock(return_value=SearchResponse(query="cafe", center=LatLng(lat=25, lng=55), allergens=[],
        places=[PlaceListItem(id="place", name="Cafe", location=LatLng(lat=25, lng=55), rating=4.5)]))
    monkeypatch.setattr(service, "search_places_service", mock)
    data = output(call("search_restaurants", {"query": "cafe", "latitude": 25, "longitude": 55, "max_results": 2}))
    assert data["places"][0]["id"] == "place"
    assert mock.call_args.args[0].max_results == 2
    assert "test-placeholder" not in json.dumps(data)


@pytest.mark.parametrize("arguments", [{"query":"cafe", "latitude":25}, {"query":"cafe", "latitude":91,"longitude":55},
                                        {"query":"cafe","max_results":21}, {"query":"  "}])
def test_invalid_search(arguments):
    assert call("search_restaurants", arguments).is_error


def test_menu_provenance_enum_serialization(monkeypatch):
    menu = PlaceMenu(place_id="place", status="ready", source_url="https://restaurant.example/menu",
        sections=[MenuSection(title="Lunch", items=[MenuItem(name="Cheese pasta", price="AED 40",
            confirmed_allergens=[AllergyTag.DAIRY], risk_label="avoid", source_url="https://restaurant.example/menu")])])
    mock = AsyncMock(return_value=menu)
    monkeypatch.setattr(service, "get_place_menu_service", mock)
    data = output(call("get_restaurant_menu", {"restaurant_id":"place", "allergens":["dairy"]}))
    assert data == menu.model_dump(mode="json")
    assert mock.call_args.args[1] == [AllergyTag.DAIRY]


def test_missing_menu():
    data = output(call("get_restaurant_menu", {"restaurant_id":"missing"}))
    assert data["status"] == "missing"
    assert data["sections"] == []


@pytest.mark.parametrize("name", ["analyze_restaurant", "recommend_dishes"])
def test_insufficient_evidence_no_fixture_fallback(name):
    data = output(call(name, {"restaurant_id":"demo-bagel", "allergens":["dairy"]}))
    assert data["overall_risk"] == "insufficient_evidence"
    assert data["missing_information"]
    assert data["trace"]["routed_to_safety_gate"]
    assert data["profile"]["allergens"] == ["dairy"]


@pytest.mark.parametrize("name", ["analyze_restaurant", "recommend_dishes"])
def test_real_deterministic_analysis_preserved(monkeypatch, name):
    source = MenuSource(source_type=SourceType.OFFICIAL_MENU, source_url="https://restaurant.example/menu",
        reliability=0.95, sections=[MenuSection(title="Lunch", items=[MenuItem(
            name="Cheese pasta", description="Wheat pasta, milk, butter and parmesan.", confirmed_allergens=[AllergyTag.DAIRY])])])
    monkeypatch.setattr(agent_graph, "load_menu_source", lambda _: source)
    data = output(call(name, {"restaurant_id":"place", "allergens":["dairy"]}))
    assert data["overall_risk"] == "high"
    assert data["recommended_action"] == "avoid"
    assert data["evidence"][0]["source_url"] == source.source_url
    assert data["trace"]["routed_to_safety_gate"]
    assert data["recommended_questions"]


@pytest.mark.parametrize("name", ["get_restaurant_menu", "get_restaurant_details", "analyze_restaurant",
                                  "recommend_dishes", "search_menu_evidence"])
def test_invalid_allergen(name):
    args = {"restaurant_id":"place", "allergens":["invented-allergen"]}
    if name == "search_menu_evidence": args["query"] = "pasta"
    assert call(name, args).is_error


@pytest.mark.parametrize("name", ["analyze_restaurant", "recommend_dishes"])
def test_analysis_requires_allergens(name):
    assert call(name, {"restaurant_id":"place", "allergens":[]}).is_error


def test_hybrid_local_fallback(monkeypatch):
    source = MenuSource(source_type=SourceType.OFFICIAL_MENU, source_url="https://restaurant.example/menu",
        sections=[MenuSection(title="Lunch", items=[MenuItem(name="Cheese pasta", description="Pasta with milk")])])
    monkeypatch.setattr(azure_search, "load_menu_source", lambda _: source)
    data = output(call("search_menu_evidence", {"restaurant_id":"place", "query":"pasta", "allergens":["dairy"],"top_k":1}))
    assert len(data["results"]) == 1
    result = data["results"][0]
    assert result["source_url"] == source.source_url
    assert result["restaurant_id"] == "place"
    assert result["dish_name"] == "Cheese pasta"
    assert result["matched_allergens"] == ["dairy"]


def test_empty_retrieval():
    assert output(call("search_menu_evidence", {"restaurant_id":"missing", "query":"pasta", "allergens":[]}))["results"] == []


@pytest.mark.parametrize("exception", [GooglePlacesError("credential=DO_NOT_EXPOSE"), RuntimeError("token=DO_NOT_EXPOSE")])
def test_provider_errors_are_sanitized(monkeypatch, exception, caplog):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-placeholder")
    monkeypatch.setattr(service, "search_places_service", AsyncMock(side_effect=exception))
    result = call("search_restaurants", {"query":"cafe"})
    assert result.is_error
    assert "DO_NOT_EXPOSE" not in str(result.content) + caplog.text


def test_unconfigured_provider():
    result = call("search_restaurants", {"query":"cafe"})
    assert result.is_error
    assert "provider_not_configured" in str(result.content)


def test_details_projection(monkeypatch):
    from allernav_api.models import PlaceDetailsResponse
    value = PlaceDetailsResponse.model_construct(
        id="place", name="Cafe", address="Dubai", location=LatLng(lat=25,lng=55), rating=4.5,
        user_rating_count=10, primary_type="restaurant", website_uri="https://restaurant.example",
        selected_allergens=[AllergyTag.DAIRY], menu=PlaceMenu(place_id="place"), agent_recommendation=None,
        review_snippets=[{"author":"PRIVATE_NAME"}])
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-placeholder")
    monkeypatch.setattr(service, "get_place_details_service", AsyncMock(return_value=value))
    data = output(call("get_restaurant_details", {"restaurant_id":"place", "allergens":["dairy"]}))
    assert data["restaurant"]["website_url"] == value.website_uri
    assert not data["menu_available"]
    assert data["selected_allergens"] == ["dairy"]
    assert "PRIVATE_NAME" not in json.dumps(data)
    assert "not an allergy-safety rating" in data["rating_context"]
