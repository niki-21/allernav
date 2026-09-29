from unittest.mock import patch
from fastapi.testclient import TestClient
from app import app
from allernav_api.document_intelligence import DocumentExtraction

client = TestClient(app)

def test_upload_rejects_wrong_type_and_signature():
    assert client.post("/api/menu-upload", content=b"hello", headers={"content-type": "text/plain"}).status_code == 415
    assert client.post("/api/menu-upload", content=b"fake", headers={"content-type": "application/pdf"}).status_code == 415

def test_upload_is_size_bounded():
    response = client.post("/api/menu-upload", content=b"x" * (3 * 1024 * 1024 + 1), headers={"content-type": "application/pdf"})
    assert response.status_code == 413

def test_upload_returns_food_without_persisting_shared_evidence():
    with patch("allernav_api.menu_upload.AzureDocumentIntelligenceClient") as factory, patch(
        "allernav_api.menu_ingestion.save_menu_source"
    ) as save:
        factory.return_value.configured = True
        factory.return_value.extract_from_bytes.return_value = DocumentExtraction(
            content="Margherita Pizza 45 AED\nTomato sauce, mozzarella and basil",
            content_type="application/pdf", extraction_method="azure_document_intelligence", page_count=1, confidence=0.9,
        )
        result = client.post("/api/menu-upload", content=b"%PDF-test", headers={"content-type": "application/pdf"})
    assert result.status_code == 200
    assert "sections" in result.json()
    save.assert_not_called()
