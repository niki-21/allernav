from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from allernav_api.firecrawl_menu import FirecrawlError, _menu_candidate, _post, collect_firecrawl_menu
from allernav_api.models import MenuItem, MenuSection


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'test-key')
    monkeypatch.setenv('AZURE_OPENAI_ENDPOINT', 'https://example.invalid')
    monkeypatch.setenv('AZURE_OPENAI_API_KEY', 'test-key')
    monkeypatch.setenv('AZURE_OPENAI_CHAT_DEPLOYMENT', 'test-deployment')


def test_maps_collects_and_merges_sections_without_duplicate_dishes():
    def post(endpoint, payload, timeout):
        if endpoint == 'map':
            return {'success': True, 'links': [{'url':'https://example.com/menu/dinner'}, {'url':'https://other.com/menu'}]}
        return {'success': True, 'data': {'markdown':'Rice Bowl AED 25\nChicken Curry AED 40'}}
    outputs = [
        [MenuSection(title='Mains', items=[MenuItem(name='Rice Bowl', price='AED 25')])],
        [MenuSection(title='Mains', items=[MenuItem(name='Rice Bowl', price='AED 25'), MenuItem(name='Chicken Curry', description='Chicken with coconut curry sauce', price='AED 40')])],
    ]
    with patch('allernav_api.firecrawl_menu._post', side_effect=post) as request, patch(
        'allernav_api.firecrawl_menu.extract_english_menu_page', side_effect=outputs
    ) as normalize:
        result = collect_firecrawl_menu('https://example.com/menu')
    assert result and len(result.sections) == 1
    assert [item.name for item in result.sections[0].items] == ['Rice Bowl', 'Chicken Curry']
    assert result.page_count == 2
    assert normalize.call_args.kwargs['source_kind'] == 'website text'
    assert request.call_count == 3


def test_map_failure_still_scrapes_direct_url():
    with patch('allernav_api.firecrawl_menu._post', side_effect=[FirecrawlError('unavailable'), {'success':True, 'data':{'markdown':'Rice Bowl'}}]), patch(
        'allernav_api.firecrawl_menu.extract_english_menu_page', return_value=[MenuSection(title='Mains', items=[MenuItem(name='Rice Bowl')])]
    ):
        assert collect_firecrawl_menu('https://example.com/menu') is not None


def test_empty_markdown_does_not_call_model():
    with patch('allernav_api.firecrawl_menu._post', return_value={'success':True, 'data':{}}), patch(
        'allernav_api.firecrawl_menu.extract_english_menu_page'
    ) as normalize:
        assert collect_firecrawl_menu('https://example.com/menu') is None
        normalize.assert_not_called()


def test_disabled_provider_does_not_make_paid_calls(monkeypatch):
    monkeypatch.setenv('FIRECRAWL_ENABLED', 'false')
    with patch('allernav_api.firecrawl_menu._post') as request:
        assert collect_firecrawl_menu('https://example.com/menu') is None
        request.assert_not_called()


def test_discovery_stays_in_restaurant_location():
    assert _menu_candidate('https://example.com/dubai/menu/dinner', 'https://example.com/dubai')
    assert not _menu_candidate('https://example.com/london/menu', 'https://example.com/dubai')
    assert not _menu_candidate('https://example.com.evil.test/dubai/menu', 'https://example.com/dubai')
    assert not _menu_candidate('https://example.com/dubai/wine-menu', 'https://example.com/dubai')


def test_provider_errors_do_not_expose_credentials_or_response_body():
    exc = HTTPError('https://api.firecrawl.dev', 401, 'test-key', {}, io.BytesIO(b'test-key'))
    with patch('allernav_api.firecrawl_menu.request.urlopen', side_effect=exc):
        with pytest.raises(FirecrawlError, match='HTTP 401') as raised:
            _post('scrape', {'url':'https://example.com'}, 5)
    assert 'test-key' not in str(raised.value)


def test_fast_scan_never_calls_firecrawl(tmp_path):
    from allernav_api.menu_ingestion import ingest_menu_from_website
    with patch('allernav_api.menu_ingestion.collect_firecrawl_menu') as collect:
        ingest_menu_from_website(restaurant_id='test', restaurant_name='Test', website_url='https://example.com',
                                 fetch_html=lambda _: None, fast_only=True, deep_scan=True, db_path=tmp_path/'menu.sqlite')
    collect.assert_not_called()


def test_background_scan_saves_managed_menu(tmp_path):
    from allernav_api.menu_ingestion import ingest_menu_from_website
    from allernav_api.models import MenuSource, SourceType
    source = MenuSource(source_type=SourceType.RESTAURANT_WEBSITE, source_url='https://example.com/menu',
                        sections=[MenuSection(title='Mains', items=[MenuItem(name='Chicken Curry', description='Chicken with coconut curry sauce', price='AED 40')])])
    with patch('allernav_api.menu_ingestion.collect_firecrawl_menu', return_value=source), patch(
        'allernav_api.menu_ingestion.save_menu_source', return_value=True
    ) as save:
        result = ingest_menu_from_website(restaurant_id='test', restaurant_name='Test', website_url='https://example.com',
                                         deep_scan=True, db_path=tmp_path/'menu.sqlite')
    assert result.sections[0].items[0].price == 'AED 40'
    save.assert_called_once()
