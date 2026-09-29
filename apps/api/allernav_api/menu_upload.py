"""Read a user-supplied menu without modifying shared restaurant evidence."""
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from .document_intelligence import AzureDocumentIntelligenceClient
from .menu_ingestion import menu_source_from_document_extraction

MAX_UPLOAD_BYTES = 3 * 1024 * 1024


def read_uploaded_menu(content: bytes, content_type: str) -> dict:
    signatures = {
        "application/pdf": content.startswith(b"%PDF-"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
    }
    if not signatures.get(content_type):
        raise HTTPException(415, "Choose a valid JPEG, PNG or PDF.")
    client = AzureDocumentIntelligenceClient()
    if not client.configured:
        raise HTTPException(503, "Menu reader is unavailable.")
    try:
        extraction = client.extract_from_bytes(content, content_type=content_type)
    except Exception:
        raise HTTPException(502, "Menu reader could not process this file.") from None
    if not extraction:
        raise HTTPException(422, "No readable menu text found.")
    source = menu_source_from_document_extraction(
        extraction, source_url="", document_url="", fallback_content_type=content_type,
    )
    return {"sections": [section.model_dump(mode="json") for section in source.sections]}


async def upload_menu_endpoint(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if content_type not in {"application/pdf", "image/png", "image/jpeg"}:
        raise HTTPException(415, "Choose a JPEG, PNG or PDF.")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File exceeds 3 MB.")
        content.extend(chunk)
    return await run_in_threadpool(read_uploaded_menu, bytes(content), content_type)
