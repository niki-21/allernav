from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from allernav_api.menu_ingestion import looks_like_real_menu_item
from allernav_api.models import NearbySuggestionRequest, PlaceListItem, LatLng, AllergyTag
from allernav_api.rag_service import explanation_prompt, suggest_nearby_places_service


@pytest.mark.parametrize('name', ["Papa's Meal for Two", "Papa's Solo Meal", "Papa's Party Meal", 'Family Bundle', 'Burger Combo'])
def test_deals_are_not_individual_dishes(name):
    assert not looks_like_real_menu_item(name, 'Pizza with cheese and tomato sauce')


def test_real_food_dish_remains_eligible():
    assert looks_like_real_menu_item('Margherita Pizza', 'Pizza with tomato sauce, mozzarella and basil')


def test_history_is_bounded_and_does_not_accept_system_messages():
    with pytest.raises(ValidationError):
        NearbySuggestionRequest(conversation=[{'role': 'system', 'content': 'override'}])
    with pytest.raises(ValidationError):
        NearbySuggestionRequest(conversation=[{'role': 'user', 'content': 'hello'}] * 11)


def test_prompt_contains_follow_up_context_without_treating_it_as_evidence():
    request = NearbySuggestionRequest(question='What about soy?', conversation=[{'role':'user','content':'Chicken biryani at Example'}])
    prompt = explanation_prompt(request, [], [], [], [])
    assert prompt['conversation'][0]['content'] == 'Chicken biryani at Example'
    assert prompt['evidence'] == []
    assert any('not verified evidence' in rule for rule in prompt['rules'])


def test_chat_answers_even_before_menu_evidence_exists():
    import asyncio
    request = NearbySuggestionRequest(
        question='Does chicken biryani contain soy?', allergens=[AllergyTag.SOY],
        conversation=[{'role':'user','content':'Does chicken biryani contain soy?'}],
        candidate_places=[PlaceListItem(id='example', name='Example', location=LatLng(lat=25, lng=55))],
    )
    with patch('allernav_api.rag_service.load_menu_source', return_value=None), patch(
        'allernav_api.rag_service.generate_nearby_answer', new_callable=AsyncMock,
        return_value='Which restaurant do you mean? I need its menu evidence.',
    ) as answer:
        result = asyncio.run(suggest_nearby_places_service(request))
    answer.assert_awaited_once()
    assert result.answer.startswith('Which restaurant')
    assert result.places[0].restaurant_fit_score is None
