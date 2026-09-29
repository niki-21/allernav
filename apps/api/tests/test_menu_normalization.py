from __future__ import annotations

import unittest
from unittest.mock import patch

from allernav_api.azure_search import build_index_documents
from allernav_api.menu_normalization import _price_grounded, extract_english_menu_page
from allernav_api.models import MenuSource, SourceType


class MenuNormalizationTests(unittest.TestCase):
    def test_price_requires_a_complete_number_not_a_substring(self) -> None:
        self.assertFalse(_price_grounded("AED 40", "Chicken Curry AED 140"))
        self.assertFalse(_price_grounded("AED 123", "Rice 12\nSoup 3"))
        self.assertTrue(_price_grounded("AED 40", "Chicken Curry AED 40"))

    def test_website_text_uses_same_grounding_and_keeps_missing_prices_absent(self) -> None:
        sections = extract_english_menu_page(
            ocr_text="Chicken Curry - chicken with coconut sauce",
            source_url="https://restaurant.example/menu", source_page=1, ocr_confidence=None,
            source_kind="website text",
            invoker=lambda messages: {"sections": [{"title": "Mains", "items": [
                {"name": "Chicken Curry", "description": "chicken with coconut sauce", "price": None},
                {"name": "Invented Fish", "price": "AED 99"},
            ]}]},
        )
        self.assertEqual([item.name for item in sections[0].items], ["Chicken Curry"])
        self.assertIsNone(sections[0].items[0].price)

    def test_keeps_only_english_ocr_grounded_dishes(self) -> None:
        ocr = """
        APPETIZERS
        $14.95 CHICKEN SATAY
        Thai Marinated Chicken Satay with Peanut Sauce
        ปอเปี๊ยะสด
        """

        sections = extract_english_menu_page(
            ocr_text=ocr,
            source_url="https://images.example/page-1.jpg",
            source_page=1,
            ocr_confidence=0.91,
            invoker=lambda _messages: {
                "sections": [
                    {
                        "title": "Appetizers",
                        "items": [
                            {
                                "name": "Chicken Satay",
                                "description": "Thai Marinated Chicken Satay with Peanut Sauce",
                                "price": "$14.95",
                            },
                            {"name": "ปอเปี๊ยะสด", "description": None, "price": None},
                            {"name": "Invented Curry", "description": "Not in OCR", "price": "$99"},
                        ],
                    }
                ]
            },
        )

        self.assertEqual(len(sections), 1)
        self.assertEqual([item.name for item in sections[0].items], ["Chicken Satay"])
        self.assertIn("Peanut Sauce", sections[0].items[0].description or "")
        self.assertEqual(sections[0].items[0].source_page, 1)
        documents = build_index_documents(
            restaurant_id="forever-thai",
            restaurant_name="Forever Thai",
            source=MenuSource(
                source_type=SourceType.OFFICIAL_MENU,
                source_url="https://www.foreverthaibushwick.com/menu",
                sections=sections,
            ),
        )
        self.assertIn("peanut", documents[0]["allergens"])

    def test_retries_once_after_invalid_structured_output(self) -> None:
        responses = iter([{"sections": "invalid"}, {"sections": [{"title": "Mains", "items": []}]}])
        calls = 0

        def invoke(_messages):  # noqa: ANN202
            nonlocal calls
            calls += 1
            return next(responses)

        result = extract_english_menu_page(
            ocr_text="MAINS",
            source_url="https://images.example/page.jpg",
            source_page=1,
            ocr_confidence=0.8,
            invoker=invoke,
        )

        self.assertEqual(result, [])
        self.assertEqual(calls, 2)

    def test_normalization_runnable_includes_langsmith_context(self) -> None:
        captured = {}

        def traced(**kwargs):  # noqa: ANN003, ANN202
            captured.update(kwargs["metadata"])
            return kwargs["func"](kwargs["value"])

        with patch("allernav_api.menu_normalization.invoke_traced_runnable", side_effect=traced):
            extract_english_menu_page(
                ocr_text="DINNER\nRice Bowl - rice and greens",
                source_url="https://restaurant.example/menu.jpg",
                source_page=1,
                ocr_confidence=0.9,
                restaurant_id="restaurant-123",
                invoker=lambda _messages: {
                    "sections": [
                        {
                            "title": "Dinner",
                            "items": [{"name": "Rice Bowl", "description": "rice and greens"}],
                        }
                    ]
                },
            )

        self.assertEqual(captured["restaurant_id"], "restaurant-123")
        self.assertEqual(captured["source_url"], "https://restaurant.example/menu.jpg")


if __name__ == "__main__":
    unittest.main()
