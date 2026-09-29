from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from allernav_api.models import (
    AllergyTag,
    IngestionTraceStep,
    HybridSearchResult,
    LatLng,
    MenuRefreshJob,
    NearbySuggestionRequest,
    NearbyPlaceSuggestion,
    PlaceListItem,
    PlaceReviewSnippet,
    SearchRequest,
)
from allernav_api.models import AllergyProfile, AnalyzeMenuRequest, MenuItem, MenuSection, MenuSource, SourceType
from allernav_api.agent_service import analyze_menu_service
from allernav_api.menu_ingestion import save_menu_source
from allernav_api.rag_service import (
    clean_llm_answer,
    generate_azure_openai_answer,
    generate_nearby_answer,
    suggest_nearby_places_service,
)
from fastapi.testclient import TestClient
from main import allowed_origins
from app import app
from allernav_api.service import create_menu_refresh_job, get_place_details_service, menu_refresh_mode, search_places_service


class FakePlacesClient:
    def search_places(self, query: str, center: LatLng, max_results: int = 12):  # noqa: ANN001
        return [
            {
                "id": "alpha",
                "name": "Alpha Cafe",
                "address": "123 Main St",
                "location": {"lat": center.lat, "lng": center.lng},
                "rating": 4.5,
                "user_rating_count": 42,
                "primary_type": "restaurant",
            }
        ]

    def get_place_details(self, place_id: str):  # noqa: ANN001
        return {
            "id": place_id,
            "name": "Alpha Cafe",
            "address": "123 Main St",
            "location": {"lat": 38.9, "lng": -77.0},
            "rating": 4.5,
            "user_rating_count": 42,
            "primary_type": "restaurant",
            "website_uri": "https://example.com",
            "editorial_summary": "Modern cafe",
            "reviews": [
                {
                    "review_id": "1",
                    "rating": 5,
                    "text": "The staff understood my peanut allergy and double checked the fryer.",
                    "publish_time": "2026-02-20T12:00:00Z",
                }
            ],
        }


class ApiTests(unittest.TestCase):
    def rag_explanation_inputs(self):  # noqa: ANN201
        payload = NearbySuggestionRequest(
            question="Which dish should I ask about?",
            allergens=[AllergyTag.PEANUT],
        )
        evidence = [
            HybridSearchResult(
                id="dish-1",
                restaurant_id="alpha",
                restaurant_name="Alpha Cafe",
                dish_name="Tomato Rice Bowl",
                source_type=SourceType.RESTAURANT_WEBSITE,
                source_url="https://example.com/menu",
                raw_text="Tomato Rice Bowl - rice, tomato, greens",
                citation_label="Alpha Cafe menu",
                citation_text="Tomato Rice Bowl - rice, tomato, greens",
            )
        ]
        suggestions = [
            NearbyPlaceSuggestion(
                place=PlaceListItem(
                    id="alpha",
                    name="Alpha Cafe",
                    location=LatLng(lat=38.9, lng=-77.0),
                ),
                confidence=0.6,
                menu_item_count=1,
                evidence=evidence,
                risk_note="Ingredient and preparation details need verification.",
            )
        ]
        return payload, suggestions, evidence, ["Cross-contact handling is unknown."], ["Ask staff about prep."]

    def test_azure_openai_chat_explanation_uses_langchain_trace_config(self) -> None:
        payload, suggestions, evidence, missing, questions = self.rag_explanation_inputs()
        metadata = {
            "restaurant_id": "alpha",
            "source_url": "https://example.com/menu",
            "item_count": 1,
            "retrieval_mode": "hybrid_keyword_semantic",
            "allergens": ["peanut"],
            "safety_gate": "verify_or_abstain",
        }
        env = {
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "test-key",
            "AZURE_OPENAI_CHAT_DEPLOYMENT": "test-chat",
            "AZURE_OPENAI_CHAT_API_VERSION": "2024-10-21",
        }
        chat_cls = MagicMock()
        langchain_openai = ModuleType("langchain_openai")
        langchain_openai.AzureChatOpenAI = chat_cls
        with patch.dict("os.environ", env, clear=True), patch.dict(
            "sys.modules", {"langchain_openai": langchain_openai}
        ):
            chat_cls.return_value.ainvoke = AsyncMock(
                return_value=SimpleNamespace(content="Possible lower-risk option; needs verification [E1].")
            )
            answer = asyncio.run(
                generate_azure_openai_answer(
                    payload,
                    suggestions,
                    evidence,
                    missing,
                    questions,
                    metadata=metadata,
                )
            )

        self.assertEqual(answer, "Possible lower-risk option; needs verification [E1].")
        config = chat_cls.return_value.ainvoke.await_args.kwargs["config"]
        self.assertEqual(config["run_name"], "AllerNav Azure OpenAI RAG Explanation")
        self.assertEqual(config["tags"], ["allernav", "rag", "azure-openai"])
        self.assertEqual(config["metadata"], metadata)

    def test_rag_explanation_falls_back_to_gemini(self) -> None:
        payload, suggestions, evidence, missing, questions = self.rag_explanation_inputs()
        with patch(
            "allernav_api.rag_service.generate_azure_openai_answer",
            new=AsyncMock(return_value=None),
        ), patch(
            "allernav_api.rag_service.generate_gemini_answer",
            return_value="Needs verification from staff [E1].",
        ) as gemini:
            answer = asyncio.run(generate_nearby_answer(payload, suggestions, evidence, missing, questions))

        self.assertEqual(answer, "Needs verification from staff [E1].")
        gemini.assert_called_once()

    def test_rag_explanation_falls_back_to_deterministic_answer(self) -> None:
        payload, suggestions, evidence, missing, questions = self.rag_explanation_inputs()
        with patch(
            "allernav_api.rag_service.generate_azure_openai_answer",
            new=AsyncMock(return_value=None),
        ), patch("allernav_api.rag_service.generate_gemini_answer", return_value=None):
            answer = asyncio.run(generate_nearby_answer(payload, suggestions, evidence, missing, questions))

        self.assertIn("verification leads", answer)
        self.assertNotIn("safe", answer.lower())

    def test_llm_safety_filter_rejects_safe_language(self) -> None:
        self.assertIsNone(clean_llm_answer("This dish is safe for a peanut allergy."))
        self.assertEqual(clean_llm_answer("This dish needs verification."), "This dish needs verification.")

    def test_health_requires_complete_azure_chat_configuration(self) -> None:
        base = {
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "test-key",
            "AZURE_OPENAI_CHAT_DEPLOYMENT": "test-chat",
        }
        with patch.dict("os.environ", base, clear=True):
            self.assertFalse(TestClient(app).get("/health").json()["environment"]["azure_openai_chat"])
        with patch.dict("os.environ", {**base, "AZURE_OPENAI_CHAT_API_VERSION": "2024-10-21"}, clear=True):
            self.assertTrue(TestClient(app).get("/health").json()["environment"]["azure_openai_chat"])

    def test_storage_debug_returns_sanitized_diagnostics(self) -> None:
        diagnostics = {
            "supabase_env_configured": True,
            "menu_records_read_ok": True,
            "menu_refresh_jobs_insert_ok": False,
            "last_supabase_error": {
                "status_code": 404,
                "table_name": "menu_refresh_jobs",
                "request_path": "/rest/v1/menu_refresh_jobs",
                "response_body": '{"code":"PGRST125"}',
                "postgrest_code": "PGRST125",
            },
        }
        with patch("app.supabase_store.storage_diagnostics", return_value=diagnostics):
            response = TestClient(app).get("/api/debug/storage")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), diagnostics)
        self.assertNotIn("service_role_key", response.text)

    def test_menu_refresh_endpoint_converts_unexpected_exception_to_failed_job(self) -> None:
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "route-secret"}), patch(
            "app.create_menu_refresh_job",
            new=AsyncMock(side_effect=RuntimeError("Refresh exploded token=route-secret")),
        ):
            response = TestClient(app).post(
                "/api/places/broken/menu-refresh",
                params={"restaurant_name": "Broken", "website_url": "https://example.com/menu"},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "failed")
        self.assertIn("RuntimeError: Refresh exploded", payload["message"])
        self.assertNotIn("route-secret", response.text)
        self.assertEqual(payload["trace"][0]["id"], "menu_ingestion_error")
        self.assertEqual(payload["trace"][0]["provider"], "fastapi")

    def test_place_menu_endpoint_returns_failed_model_on_unexpected_exception(self) -> None:
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "menu-secret"}), patch(
            "app.get_place_menu_service",
            new=AsyncMock(side_effect=RuntimeError("Menu load failed token=menu-secret")),
        ):
            response = TestClient(app).get("/api/places/broken/menu")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["place_id"], "broken")
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["sections"], [])
        self.assertNotIn("menu-secret", response.text)

    def test_menu_refresh_mode_defaults_and_local_override(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(menu_refresh_mode(), "auto")
        with patch.dict("os.environ", {"MENU_REFRESH_MODE": "local"}, clear=True):
            self.assertEqual(menu_refresh_mode(), "local")

    def test_auto_mode_falls_back_locally_when_durable_persistence_fails(self) -> None:
        fallback_job = MenuRefreshJob(
            id="local-fallback",
            place_id="alpha",
            status="failed",
            message="Local scan finished.",
            created_at="2026-06-29T00:00:00+00:00",
        )
        with patch.dict(
            "os.environ",
            {
                "MENU_REFRESH_MODE": "auto",
                "AZURE_SERVICE_BUS_SEND_CONNECTION_STRING": "configured",
            },
            clear=True,
        ), patch("allernav_api.service.supabase_store.configured", return_value=True), patch(
            "allernav_api.service.supabase_store.save_menu_refresh_job", return_value=False
        ), patch(
            "allernav_api.service.supabase_store.last_error",
            return_value="Supabase HTTP 400 Bad Request: missing column job_json",
        ), patch("allernav_api.service._discover_squarespace_image_set", return_value=None), patch(
            "allernav_api.service._create_local_menu_refresh_job", return_value=fallback_job
        ) as local_refresh:
            result = asyncio.run(
                create_menu_refresh_job(
                    "alpha",
                    restaurant_name="Alpha",
                    website_url="https://alpha.example",
                )
            )

        self.assertEqual(result.id, "local-fallback")
        local_refresh.assert_called_once()
        self.assertIn("Cloud job could not be saved", local_refresh.call_args.kwargs["fallback_detail"])
        self.assertIn("missing column job_json", local_refresh.call_args.kwargs["fallback_detail"])

    def test_queue_only_background_scan_never_runs_local_ingestion(self) -> None:
        with patch.dict("os.environ", {"MENU_REFRESH_MODE": "local"}, clear=True), patch(
            "allernav_api.service._create_local_menu_refresh_job"
        ) as local_refresh:
            result = asyncio.run(
                create_menu_refresh_job(
                    "alpha",
                    restaurant_name="Alpha",
                    website_url="https://alpha.example",
                    allow_local_fallback=False,
                )
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("durable menu refresh queue", result.message.lower())
        local_refresh.assert_not_called()

    def test_allowed_origins_include_localhost_defaults(self) -> None:
        origins = allowed_origins()
        self.assertIn("http://localhost:3000", origins)
        self.assertIn("http://127.0.0.1:3000", origins)

    def test_search_response_shape(self) -> None:
        payload = SearchRequest(
            query="ramen",
            center=LatLng(lat=38.9, lng=-77.0),
            allergens=[AllergyTag.PEANUT],
        )

        response = asyncio.run(search_places_service(payload, client=FakePlacesClient()))

        dumped = response.model_dump()
        self.assertEqual(dumped["query"], "ramen")
        self.assertEqual(dumped["center"]["lat"], 38.9)
        self.assertEqual(dumped["places"][0]["id"], "alpha")
        self.assertEqual(dumped["allergens"], ["peanut"])

    def test_search_works_without_selected_allergens(self) -> None:
        response = asyncio.run(
            search_places_service(
                SearchRequest(query="restaurants", center=LatLng(lat=38.9, lng=-77.0), allergens=[]),
                client=FakePlacesClient(),
            )
        )
        self.assertEqual(response.allergens, [])
        self.assertEqual(response.places[0].id, "alpha")

    def test_place_details_response_shape(self) -> None:
        with patch("allernav_api.service.load_cached_reviews", return_value=[]):
            response = asyncio.run(
                get_place_details_service(
                    "alpha",
                    allergens=[AllergyTag.PEANUT],
                    client=FakePlacesClient(),
                )
            )

        dumped = response.model_dump()
        self.assertEqual(dumped["id"], "alpha")
        self.assertEqual(dumped["selected_allergens"], ["peanut"])
        self.assertIn("score_summary", dumped)
        self.assertIsNone(dumped["restaurant_fit_score"])
        self.assertEqual(dumped["restaurant_fit_label"], "Menu scan needed")
        self.assertGreaterEqual(len(dumped["evidence"]), 1)
        self.assertIn("explanation", dumped)

    def test_place_details_without_allergens_uses_general_signals(self) -> None:
        with patch("allernav_api.service.load_cached_reviews", return_value=[]):
            response = asyncio.run(get_place_details_service("alpha", allergens=[], client=FakePlacesClient()))

        self.assertEqual(response.selected_allergens, [])
        self.assertIsNone(response.restaurant_fit_score)
        self.assertIsNone(response.restaurant_fit_label)
        self.assertIsNone(response.agent_recommendation)
        self.assertEqual(response.score_summary.evidence_status, "general")
        self.assertIn("No allergies selected", response.explanation)

    def test_place_details_uses_cached_apify_reviews_when_available(self) -> None:
        with patch(
            "allernav_api.service.load_cached_reviews",
            return_value=[
                PlaceReviewSnippet(
                    review_id="apify-1",
                    author_name="Reviewer",
                    rating=1,
                    text="I have a sesame allergy and had a reaction after cross-contact.",
                    publish_time="2026-02-22T12:00:00+00:00",
                )
            ],
        ):
            response = asyncio.run(
                get_place_details_service(
                    "alpha",
                    allergens=[AllergyTag.SESAME],
                    client=FakePlacesClient(),
                )
            )

        self.assertEqual(response.review_snippets[0].review_id, "apify-1")
        self.assertEqual(response.score_summary.verdict.value, "high_risk")
        self.assertTrue(any(item.review_id == "apify-1" for item in response.evidence))
        self.assertEqual(response.review_source_summary.expanded_review_status, "loaded")

    def test_missing_reviews_is_handled(self) -> None:
        class NoReviewClient(FakePlacesClient):
            def get_place_details(self, place_id: str):  # noqa: ANN001
                place = super().get_place_details(place_id)
                place["reviews"] = []
                return place

        with patch("allernav_api.service.load_cached_reviews", return_value=[]):
            response = asyncio.run(
                get_place_details_service(
                    "alpha",
                    allergens=[AllergyTag.PEANUT],
                    client=NoReviewClient(),
                )
            )

        self.assertEqual(response.score_summary.evidence_count, 0)
        self.assertEqual(response.score_summary.verdict.value, "use_caution")

    def test_analyze_menu_service_returns_source_backed_recommendation(self) -> None:
        response = asyncio.run(
            analyze_menu_service(
                AnalyzeMenuRequest(
                    restaurant_name="Demo Pasta",
                    profile=AllergyProfile(allergens=[AllergyTag.DAIRY]),
                    menu_sources=[
                        MenuSource(
                            source_type=SourceType.OFFICIAL_MENU,
                            source_url="https://example.com/menu",
                            reliability=0.9,
                            raw_text="Chicken Alfredo - pasta with cream sauce, butter, and parmesan",
                        )
                    ],
                )
            )
        )

        dumped = response.model_dump()
        self.assertEqual(dumped["overall_risk"], "high")
        self.assertEqual(dumped["recommended_action"], "avoid")
        self.assertGreaterEqual(len(dumped["evidence"]), 1)

    def test_menu_refresh_endpoint_stores_and_returns_menu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "menus.sqlite"
            os.environ["ALLERNAV_MENU_DB"] = str(db_path)

            def fake_ingest(**kwargs):  # noqa: ANN003, ANN202
                kwargs["trace"].append(
                    IngestionTraceStep(
                        id="source_discovery",
                        label="Discover menu sources",
                        status="complete",
                        detail="Found one test menu source.",
                        provider="test",
                    )
                )
                source = MenuSource(
                    source_type=SourceType.RESTAURANT_WEBSITE,
                    source_url=kwargs["website_url"],
                    reliability=0.8,
                    sections=[
                        MenuSection(
                            title="Bowls",
                            items=[MenuItem(name="Tomato Rice Bowl", description="Rice, tomato, greens.")],
                        )
                    ],
                )
                save_menu_source(
                    restaurant_id=kwargs["restaurant_id"],
                    restaurant_name=kwargs["restaurant_name"],
                    source=source,
                    db_path=db_path,
                )
                return source

            with patch("allernav_api.service.ingest_menu_from_website", side_effect=fake_ingest), patch(
                "allernav_api.service.MENU_INDEX_EXECUTOR.submit"
            ) as submit_index, patch("allernav_api.service.index_restaurant_menu") as inline_index:
                client = TestClient(app)
                refresh = client.post(
                    "/api/places/alpha/menu-refresh",
                    params={"restaurant_name": "Alpha", "website_url": "https://example.com/menu"},
                )
                menu = client.get("/api/places/alpha/menu", params=[("allergens", "peanut")])

            os.environ.pop("ALLERNAV_MENU_DB", None)

        self.assertEqual(refresh.status_code, 202)
        self.assertEqual(refresh.json()["status"], "running")
        self.assertEqual(refresh.json()["indexing_status"], "pending")
        self.assertEqual(refresh.json()["trace"][0]["id"], "source_discovery")
        trace_by_id = {step["id"]: step for step in refresh.json()["trace"]}
        self.assertEqual(trace_by_id["menu_extracted"]["status"], "complete")
        self.assertEqual(trace_by_id["search_index"]["status"], "pending")
        self.assertEqual(trace_by_id["deep_scan"]["status"], "running")
        submit_index.assert_called_once()
        inline_index.assert_not_called()
        self.assertEqual(menu.status_code, 200)
        self.assertEqual(menu.json()["sections"][0]["items"][0]["name"], "Tomato Rice Bowl")
        self.assertGreater(menu.json()["restaurant_fit_score"], 20)
        self.assertEqual(menu.json()["possible_lower_risk_count"], 1)

    def test_menu_refresh_reuses_recent_menu_until_force_refresh(self) -> None:
        cached = MenuSource(
            source_type=SourceType.RESTAURANT_WEBSITE,
            source_url="https://example.com/menu",
            source_timestamp="2099-01-01T00:00:00+00:00",
            reliability=0.9,
            sections=[MenuSection(title="Bowls", items=[MenuItem(name="Rice Bowl")])],
        )

        with patch("allernav_api.service.load_menu_source", return_value=cached), patch(
            "allernav_api.service.ingest_menu_from_website"
        ) as ingest:
            job = asyncio.run(
                create_menu_refresh_job(
                    "alpha",
                    restaurant_name="Alpha",
                    website_url="https://example.com/menu",
                )
            )

        self.assertEqual(job.status, "complete")
        self.assertEqual(job.message, "Using recently scanned menu evidence.")
        self.assertEqual(job.trace[0].id, "cache_check")
        ingest.assert_not_called()

    def test_force_refresh_bypasses_recent_menu_cache(self) -> None:
        cached = MenuSource(
            source_type=SourceType.RESTAURANT_WEBSITE,
            source_url="https://example.com/menu",
            source_timestamp="2099-01-01T00:00:00+00:00",
            reliability=0.9,
            sections=[MenuSection(title="Bowls", items=[MenuItem(name="Rice Bowl")])],
        )
        refreshed = cached.model_copy(update={"source_timestamp": "2099-01-02T00:00:00+00:00"})

        with patch.dict(os.environ, {"MENU_REFRESH_MODE": "local"}), patch(
            "allernav_api.service.load_menu_source", return_value=cached
        ), patch(
            "allernav_api.service.ingest_menu_from_website", return_value=refreshed
        ) as ingest, patch(
            "allernav_api.service.MENU_INDEX_EXECUTOR.submit"
        ):
            job = asyncio.run(
                create_menu_refresh_job(
                    "alpha",
                    restaurant_name="Alpha",
                    website_url="https://example.com/menu",
                    force_refresh=True,
                )
            )

        self.assertEqual(job.status, "running")
        self.assertTrue(ingest.call_args.kwargs["fast_only"])

    def test_menu_refresh_returns_sanitized_ingestion_error_job(self) -> None:
        with patch.dict(
            os.environ,
            {"MENU_REFRESH_MODE": "local", "AZURE_OPENAI_API_KEY": "super-secret-key"},
        ), patch(
            "allernav_api.service.load_menu_source", return_value=None
        ), patch(
            "allernav_api.service.supabase_store.configured", return_value=False
        ), patch(
            "allernav_api.service.ingest_menu_from_website",
            side_effect=RuntimeError("Parser crashed token=super-secret-key"),
        ):
            job = asyncio.run(
                create_menu_refresh_job(
                    "broken-menu",
                    restaurant_name="Broken Menu",
                    website_url="https://example.com/menu?token=private",
                )
            )

        self.assertEqual(job.status, "failed")
        self.assertIn("RuntimeError: Parser crashed", job.message)
        self.assertNotIn("super-secret-key", job.message)
        error_step = next(step for step in job.trace if step.id == "menu_ingestion_error")
        self.assertEqual(error_step.label, "Run menu discovery")
        self.assertEqual(error_step.status, "failed")
        self.assertEqual(error_step.provider, "fastapi")
        self.assertNotIn("super-secret-key", error_step.detail)

    def test_empty_fast_scan_remains_running_until_deep_scan_finishes(self) -> None:
        empty_source = MenuSource(
            source_type=SourceType.RESTAURANT_WEBSITE,
            source_url="https://example.com/menu",
            reliability=0.2,
            sections=[],
        )
        with patch.dict(os.environ, {"MENU_REFRESH_MODE": "local"}), patch(
            "allernav_api.service.load_menu_source", return_value=None
        ), patch(
            "allernav_api.service.ingest_menu_from_website", return_value=empty_source
        ), patch(
            "allernav_api.service.MENU_INDEX_EXECUTOR.submit"
        ):
            job = asyncio.run(
                create_menu_refresh_job(
                    "empty-fast-scan",
                    restaurant_name="Empty Fast Scan",
                    website_url="https://example.com/menu",
                )
            )

        self.assertEqual(job.status, "running")
        self.assertEqual(job.item_count, 0)
        self.assertIn("scan is running", job.message.lower())

    def test_menu_refresh_queues_squarespace_image_job_when_azure_is_configured(self) -> None:
        menu_html = "".join(
            f'<img data-image="https://images.example/Forever+Thai+Menu+May+2026_Page_{page}.jpg">'
            for page in range(1, 3)
        )
        with patch.dict("os.environ", {"AZURE_SERVICE_BUS_SEND_CONNECTION_STRING": "test-connection"}), patch(
            "allernav_api.service.supabase_store.configured", return_value=True
        ), patch(
            "allernav_api.service.supabase_store.save_menu_refresh_job", return_value=True
        ), patch(
            "allernav_api.service.supabase_store.load_menu_refresh_job", return_value=None
        ), patch(
            "allernav_api.service.fetch_html_url",
            side_effect=lambda url: (
                menu_html
                if url.endswith("/menu")
                else '<a href="/menu">Menu</a>'
                if url.rstrip("/") == "https://www.foreverthaibushwick.com"
                else None
            ),
        ), patch("allernav_api.service.enqueue_menu_refresh") as enqueue:
            client = TestClient(app)
            refresh = client.post(
                "/api/places/forever-thai/menu-refresh",
                params={
                    "restaurant_name": "Forever Thai",
                    "website_url": "https://www.foreverthaibushwick.com/",
                },
            )
            job = client.get(f"/api/menu-refresh-jobs/{refresh.json()['id']}")

        self.assertEqual(refresh.status_code, 202)
        self.assertEqual(refresh.json()["status"], "queued")
        self.assertEqual(refresh.json()["total_documents"], 2)
        self.assertEqual(refresh.json()["menu_version"], "May 2026")
        self.assertEqual(job.status_code, 200)
        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.args[0].website_url,
            "https://www.foreverthaibushwick.com/menu",
        )

    def test_nearby_rag_suggestions_retrieve_stored_menu_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "menus.sqlite"
            os.environ["ALLERNAV_MENU_DB"] = str(db_path)
            os.environ.pop("GEMINI_API_KEY", None)
            save_menu_source(
                restaurant_id="alpha",
                restaurant_name="Alpha Cafe",
                source=MenuSource(
                    source_type=SourceType.RESTAURANT_WEBSITE,
                    source_url="https://example.com/menu",
                    reliability=0.8,
                    sections=[
                        MenuSection(
                            title="Bowls",
                            items=[
                                MenuItem(name="Tomato Rice Bowl", description="Rice, tomato, greens."),
                                MenuItem(name="Peanut Noodles", description="Wheat noodles, peanut sauce."),
                            ],
                        )
                    ],
                ),
                db_path=db_path,
            )

            response = asyncio.run(
                suggest_nearby_places_service(
                    NearbySuggestionRequest(
                        question="Where should I start for peanut allergy?",
                        allergens=[AllergyTag.PEANUT],
                        candidate_place_ids=["alpha"],
                        candidate_places=[
                            PlaceListItem(
                                id="alpha",
                                name="Alpha Cafe",
                                location=LatLng(lat=38.9, lng=-77.0),
                            )
                        ],
                    ),
                )
            )
            os.environ.pop("ALLERNAV_MENU_DB", None)

        self.assertEqual(response.places[0].place.id, "alpha")
        self.assertGreaterEqual(len(response.evidence), 1)
        self.assertEqual(
            response.answer,
            f"Best current option: Alpha Cafe scores {response.places[0].restaurant_fit_score}. "
            "I found 1 item to ask about and 1 direct allergen match. "
            "Search this area to compare more restaurants.",
        )
        self.assertEqual(response.places[0].evidence_status, "scanned")
        self.assertGreater(response.places[0].restaurant_fit_score, 20)

    def test_nearby_rag_without_scanned_menu_returns_scan_needed_places(self) -> None:
        payload = NearbySuggestionRequest(
            question="Where should I start for peanut allergy?",
            allergens=[AllergyTag.PEANUT],
            candidate_places=[
                PlaceListItem(
                    id="alpha",
                    name="Alpha Cafe",
                    location=LatLng(lat=38.9, lng=-77.0),
                )
            ],
        )
        with patch("allernav_api.rag_service.load_menu_source", return_value=None), patch(
            "allernav_api.rag_service.hybrid_search_menu"
        ) as hybrid_search, patch("allernav_api.rag_service.generate_nearby_answer") as explanation:
            response = asyncio.run(suggest_nearby_places_service(payload))

        self.assertEqual(
            response.answer,
            "Only one candidate was available. Start by scanning Alpha Cafe before allergy comparison.",
        )
        self.assertEqual(response.missing_information, ["Some nearby places do not have scanned menu evidence yet."])
        self.assertEqual(response.places[0].evidence_status, "scan_needed")
        self.assertIsNone(response.places[0].restaurant_fit_score)
        self.assertEqual(response.places[0].scan_priority_rank, 1)
        self.assertEqual(response.scan_needed_places[0].id, "alpha")
        hybrid_search.assert_not_called()
        explanation.assert_not_called()

    def test_nearby_rag_evaluates_multiple_visible_candidate_ids(self) -> None:
        candidates = [
            PlaceListItem(id="alpha", name="Alpha Cafe", location=LatLng(lat=40.0, lng=-73.0)),
            PlaceListItem(id="bravo", name="Bravo Grill", location=LatLng(lat=40.01, lng=-73.01)),
            PlaceListItem(id="charlie", name="Charlie Deli", location=LatLng(lat=40.02, lng=-73.02)),
        ]
        sources = {
            "alpha": MenuSource(
                source_type=SourceType.RESTAURANT_WEBSITE,
                source_url="https://alpha.example/menu",
                sections=[MenuSection(title="Mains", items=[MenuItem(name="Rice Bowl", description="Rice and herbs")])],
            ),
            "bravo": MenuSource(
                source_type=SourceType.RESTAURANT_WEBSITE,
                source_url="https://bravo.example/menu",
                sections=[MenuSection(title="Mains", items=[MenuItem(name="Grilled Chicken", description="Chicken and vegetables")])],
            ),
        }

        def load_source(place_id: str):  # noqa: ANN202
            return sources.get(place_id)

        def search(payload):  # noqa: ANN001, ANN202
            return type(
                "SearchResponse",
                (),
                {
                    "results": [
                        HybridSearchResult(
                            id=f"{payload.restaurant_id}-evidence",
                            restaurant_id=payload.restaurant_id,
                            restaurant_name=next(place.name for place in candidates if place.id == payload.restaurant_id),
                            dish_name="Candidate dish",
                            source_type=SourceType.RESTAURANT_WEBSITE,
                            source_url=f"https://{payload.restaurant_id}.example/menu",
                            raw_text="Candidate dish with listed ingredients",
                            citation_label="Official menu",
                            citation_text="Candidate dish with listed ingredients",
                        )
                    ]
                },
            )()

        request_payload = NearbySuggestionRequest(
            question="Suggest nearby places",
            allergens=[AllergyTag.SESAME],
            candidate_place_ids=[place.id for place in candidates],
            candidate_places=candidates,
        )
        with patch("allernav_api.rag_service.load_menu_source", side_effect=load_source), patch(
            "allernav_api.rag_service.hybrid_search_menu", side_effect=search
        ), patch(
            "allernav_api.rag_service.generate_nearby_answer",
            new=AsyncMock(return_value="Compare Alpha Cafe and Bravo Grill using cited menu evidence."),
        ):
            response = asyncio.run(suggest_nearby_places_service(request_payload))

        self.assertEqual({place.place.id for place in response.places}, {"alpha", "bravo", "charlie"})
        scanned = [place for place in response.places if place.evidence_status == "scanned"]
        self.assertEqual(len(scanned), 2)
        self.assertIn("Best current option:", response.answer)
        self.assertIn("1 nearby place still needs menu scans.", response.answer)
        self.assertTrue(all(place.evidence_count == 1 for place in scanned))
        self.assertEqual(response.scan_needed_places[0].id, "charlie")
        self.assertTrue(all(place.reason for place in response.places))

    def test_nearby_rag_prioritizes_eight_unscanned_candidates_without_starting_jobs(self) -> None:
        center = LatLng(lat=40.74, lng=-73.99)
        candidates = [
            PlaceListItem(
                id=f"place-{index}",
                name=name,
                location=LatLng(lat=center.lat + index * 0.002, lng=center.lng),
                rating=4.9 - index * 0.1,
                user_rating_count=2000 - index * 150,
                primary_type="restaurant",
                website_url=f"https://place-{index}.example",
            )
            for index, name in enumerate(
                [
                    "L'Adresse NoMad",
                    "Hole In The Wall",
                    "Gramercy Tavern",
                    "Cafe Four",
                    "Cafe Five",
                    "Cafe Six",
                    "Cafe Seven",
                    "Cafe Eight",
                ]
            )
        ]
        payload = NearbySuggestionRequest(
            center=center,
            candidate_places=candidates,
            max_places=8,
            allow_background_scan=False,
            allergens=[AllergyTag.PEANUT],
        )
        with patch("allernav_api.rag_service.load_menu_source", return_value=None), patch(
            "allernav_api.service.create_menu_refresh_job", new=AsyncMock()
        ) as create_job, patch("allernav_api.rag_service.update_current_trace_metadata") as trace_metadata:
            response = asyncio.run(suggest_nearby_places_service(payload))

        self.assertEqual(
            response.answer,
            "I found 8 nearby restaurants in this area. None have scanned menu evidence yet, so I can't compare "
            "allergy fit. Start by scanning the top 3 candidates: L'Adresse NoMad, Hole In The Wall, and Gramercy Tavern.",
        )
        self.assertEqual([place.name for place in response.top_scan_candidates], [
            "L'Adresse NoMad",
            "Hole In The Wall",
            "Gramercy Tavern",
        ])
        self.assertTrue(all(item.restaurant_fit_score is None for item in response.places))
        self.assertEqual([item.scan_priority_rank for item in response.places[:3]], [1, 2, 3])
        create_job.assert_not_awaited()
        self.assertEqual(trace_metadata.call_args.kwargs["flow_stage"], "scan_needed")
        self.assertEqual(trace_metadata.call_args.kwargs["top_scan_candidates"], [
            "L'Adresse NoMad",
            "Hole In The Wall",
            "Gramercy Tavern",
        ])

    def test_nearby_rag_replaces_placeholder_place_name(self) -> None:
        payload = NearbySuggestionRequest(
            allergens=[AllergyTag.FISH],
            candidate_places=[
                PlaceListItem(id="native", name="Selected place", location=LatLng(lat=40, lng=-73))
            ],
        )
        with patch("allernav_api.rag_service.load_menu_source", return_value=None):
            response = asyncio.run(suggest_nearby_places_service(payload))

        self.assertEqual(response.places[0].place.name, "This restaurant")
        self.assertNotIn("Selected place", response.answer)

    def test_nearby_rag_starts_at_most_two_background_scans(self) -> None:
        candidates = [
            PlaceListItem(
                id=f"place-{index}",
                name=f"Place {index}",
                location=LatLng(lat=40 + index / 100, lng=-73),
                website_url=f"https://place-{index}.example",
            )
            for index in range(3)
        ]
        payload = NearbySuggestionRequest(
            candidate_places=candidates,
            allergens=[AllergyTag.SESAME],
            allow_background_scan=True,
        )
        jobs = [
            MenuRefreshJob(
                id=f"job-{index}",
                place_id=f"place-{index}",
                status="queued",
                message="Queued",
                created_at="2026-06-30T00:00:00Z",
            )
            for index in range(2)
        ]
        with patch("allernav_api.rag_service.load_menu_source", return_value=None), patch(
            "allernav_api.service.create_menu_refresh_job", new=AsyncMock(side_effect=jobs)
        ) as create_job:
            response = asyncio.run(suggest_nearby_places_service(payload))

        self.assertEqual(create_job.await_count, 2)
        self.assertTrue(all(call.kwargs["allow_local_fallback"] is False for call in create_job.await_args_list))
        self.assertEqual(sum(item.evidence_status == "scan_running" for item in response.places), 2)
        self.assertEqual(sum(item.evidence_status == "scan_needed" for item in response.places), 1)
        self.assertEqual(response.scan_job_ids, ["job-0", "job-1"])
        self.assertIn("I started menu scans for Place 0 and Place 1.", response.answer)

    def test_nearby_rag_marks_one_failed_background_scan_without_failing_request(self) -> None:
        candidates = [
            PlaceListItem(
                id=f"place-{index}",
                name=f"Place {index}",
                location=LatLng(lat=40 + index / 100, lng=-73),
                website_url=f"https://place-{index}.example",
            )
            for index in range(2)
        ]
        payload = NearbySuggestionRequest(
            candidate_places=candidates,
            allergens=[AllergyTag.SESAME],
            allow_background_scan=True,
        )
        successful_job = MenuRefreshJob(
            id="job-1",
            place_id="place-1",
            status="running",
            message="Menu found; deeper scan is running.",
            created_at="2026-06-30T00:00:00Z",
        )

        with patch("allernav_api.rag_service.load_menu_source", return_value=None), patch(
            "allernav_api.service.create_menu_refresh_job",
            new=AsyncMock(side_effect=[RuntimeError("durable persistence unavailable"), successful_job]),
        ):
            response = asyncio.run(suggest_nearby_places_service(payload))

        by_id = {item.place.id: item for item in response.places}
        self.assertEqual(by_id["place-0"].evidence_status, "scan_failed")
        self.assertIsNone(by_id["place-0"].scan_job_id)
        self.assertEqual(by_id["place-1"].evidence_status, "scan_running")
        self.assertEqual(response.scan_job_ids, ["job-1"])

    def test_nearby_rag_endpoint_returns_200_when_one_background_scan_raises(self) -> None:
        payload = {
            "candidate_places": [
                {
                    "id": f"place-{index}",
                    "name": f"Place {index}",
                    "location": {"lat": 40 + index / 100, "lng": -73},
                    "website_url": f"https://place-{index}.example",
                }
                for index in range(2)
            ],
            "allergens": ["sesame"],
            "allow_background_scan": True,
        }
        successful_job = MenuRefreshJob(
            id="job-1",
            place_id="place-1",
            status="running",
            message="Menu found; deeper scan is running.",
            created_at="2026-06-30T00:00:00Z",
        )

        with patch("allernav_api.rag_service.load_menu_source", return_value=None), patch(
            "allernav_api.service.create_menu_refresh_job",
            new=AsyncMock(side_effect=[RuntimeError("one scan failed"), successful_job]),
        ):
            response = TestClient(app).post("/api/rag/nearby-suggestions", json=payload)

        self.assertEqual(response.status_code, 200)
        by_id = {item["place"]["id"]: item for item in response.json()["places"]}
        self.assertEqual(by_id["place-0"]["evidence_status"], "scan_failed")
        self.assertEqual(by_id["place-1"]["evidence_status"], "scan_running")

    def test_nearby_rag_without_allergens_uses_general_discovery_ranking(self) -> None:
        center = LatLng(lat=40.0, lng=-73.0)
        candidates = [
            PlaceListItem(
                id="popular",
                name="Popular Cafe",
                location=LatLng(lat=40.001, lng=-73.0),
                rating=4.8,
                user_rating_count=1200,
                primary_type="restaurant",
            ),
            PlaceListItem(
                id="quiet",
                name="Quiet Cafe",
                location=LatLng(lat=40.02, lng=-73.0),
                rating=4.0,
                user_rating_count=20,
                primary_type="cafe",
            ),
        ]
        with patch("allernav_api.rag_service.load_menu_source", return_value=None):
            response = asyncio.run(
                suggest_nearby_places_service(
                    NearbySuggestionRequest(
                        question="Suggest nearby places",
                        center=center,
                        allergens=[],
                        candidate_places=candidates,
                    )
                )
            )

        self.assertEqual(response.ranking_mode, "general_discovery")
        self.assertEqual(response.places[0].place.id, "popular")
        self.assertIsNone(response.places[0].restaurant_fit_score)
        self.assertGreater(response.places[0].general_match_score, response.places[1].general_match_score)
        self.assertEqual(response.scan_needed_places, [])
        self.assertEqual(
            response.answer,
            "No allergies selected. I found 2 nearby restaurants and ranked them by rating, popularity, distance, and search intent.",
        )

if __name__ == "__main__":
    unittest.main()
