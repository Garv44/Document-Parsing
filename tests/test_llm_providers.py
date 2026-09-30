"""Provider switch: the Gemini backend of llm._parse, with a fake google-genai client (no network)."""
import asyncio
import base64
import importlib
import json
from types import SimpleNamespace

import pytest
from google.genai import errors, types

from app import config, dynamic_schema, llm
from app.doc_schemas import Classification

DOC = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                      "data": base64.b64encode(b"%PDF-1.4 fake").decode()}}


def response(payload, finish="STOP", block_reason=None):
    return SimpleNamespace(
        text=payload if isinstance(payload, str) or payload is None else json.dumps(payload),
        candidates=[SimpleNamespace(finish_reason=types.FinishReason[finish])],
        prompt_feedback=SimpleNamespace(block_reason=block_reason) if block_reason else None,
        usage_metadata=SimpleNamespace(prompt_token_count=120, candidates_token_count=30),
        model_version="gemini-2.5-flash-lite",
    )


class FakeGemini:
    """Stands in for genai.Client; `script` is a list of responses or exceptions, consumed in order."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def gemini(monkeypatch):
    def install(*script):
        fake = FakeGemini(script)
        monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
        monkeypatch.setattr(config, "LLM_MODEL", "gemini-2.5-flash-lite")
        monkeypatch.setattr(llm, "_gemini_client", fake)
        real_sleep = asyncio.sleep
        monkeypatch.setattr(llm.asyncio, "sleep", lambda s: real_sleep(0))  # no real back-off waits
        return fake
    return install


def client_error(code, msg="boom"):
    return errors.ClientError(code, {"error": {"code": code, "message": msg, "status": "X"}})


def test_classify_via_gemini(gemini):
    fake = gemini(response({"doc_type": "invoice", "display_name": "Invoice", "purpose": "Bill",
                            "confidence": 0.93, "reasoning": "Says invoice."}))
    result, usage = asyncio.run(llm.classify(DOC, []))
    assert isinstance(result, Classification) and result.doc_type == "invoice"
    assert usage == {"model": "gemini-2.5-flash-lite", "input_tokens": 120, "output_tokens": 30}

    call = fake.calls[0]
    assert call["model"] == "gemini-2.5-flash-lite"
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].response_json_schema["properties"].keys() >= {"doc_type", "confidence"}
    assert call["config"].system_instruction == llm.SYSTEM_PROMPT
    doc_part, text_part = call["contents"][0].parts
    assert doc_part.inline_data.mime_type == "application/pdf" and doc_part.inline_data.data == b"%PDF-1.4 fake"
    assert "Identify what kind of business document" in text_part.text


def test_dynamic_extraction_schema_is_sent_to_gemini(gemini):
    schema = dynamic_schema.sanitize_schema([
        {"key": "total", "label": "Total", "type": "number", "role": "total", "columns": []},
        {"key": "rows", "label": "Rows", "type": "table", "role": "none",
         "columns": [{"key": "item", "label": "Item", "type": "string", "role": "description"}]}])
    fake = gemini(response({"total": {"value": 12.5, "confidence": 0.9},
                            "rows": {"rows": [{"item": "A"}], "confidence": 0.8}}))
    raw, _ = asyncio.run(llm.extract_dynamic(DOC, schema, "Test doc"))
    assert raw["total"] == {"value": 12.5, "confidence": 0.9} and raw["rows"]["rows"] == [{"item": "A"}]
    sent = fake.calls[0]["config"].response_json_schema
    assert set(sent["required"]) == {"total", "rows"} and "$defs" in sent


def test_rejected_schema_falls_back_to_envelope(gemini):
    schema = dynamic_schema.sanitize_schema([{"key": "total", "label": "Total", "type": "number", "role": "total",
                                              "columns": []}])
    fake = gemini(client_error(400, "schema too complex"),
                  response({"fields": [{"key": "total", "value": "USD 9.00", "confidence": 0.7}], "tables": []}))
    raw, usage = asyncio.run(llm.extract_dynamic(DOC, schema, "Test doc"))
    assert raw["total"] == {"value": 9.0, "confidence": 0.7} and usage["fallback_format"] is True
    assert fake.calls[1]["config"].response_json_schema["title"] == "Envelope"


def test_rate_limit_and_server_errors_are_retried(gemini):
    ok = response({"doc_type": "receipt", "display_name": "Receipt", "purpose": "p", "confidence": 1,
                   "reasoning": "r"})
    fake = gemini(client_error(429), errors.ServerError(503, {"error": {"code": 503, "message": "busy"}}), ok)
    result, _ = asyncio.run(llm.classify(DOC, []))
    assert result.doc_type == "receipt" and len(fake.calls) == 3


@pytest.mark.parametrize("script, message", [
    ([client_error(401)], "Gemini API key missing or invalid"),
    ([client_error(404)], "not found. Check LLM_MODEL"),
    ([client_error(429)] * 4, "Rate limited by the Gemini API"),
    ([response(None, finish="MAX_TOKENS")], "cut off"),
    ([response(None, finish="SAFETY")], "declined"),
    ([response(None, block_reason="PROHIBITED_CONTENT")], "blocked"),
    ([response("not json at all")], "did not match the expected schema"),
    ([response({"doc_type": "invoice"})], "did not match the expected schema"),
])
def test_gemini_failures_become_llm_errors(gemini, script, message):
    gemini(*script)
    with pytest.raises(llm.LLMError, match=message):
        asyncio.run(llm.classify(DOC, []))


def test_missing_gemini_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(llm, "_gemini_client", None)
    with pytest.raises(llm.LLMError, match="GEMINI_API_KEY"):
        asyncio.run(llm.classify(DOC, []))


def test_provider_and_model_settings(monkeypatch):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "ANTHROPIC_MODEL", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)  # ignore a developer's local .env
    try:
        assert (importlib.reload(config).LLM_PROVIDER, config.LLM_MODEL) == ("anthropic", "claude-opus-5")
        monkeypatch.setenv("LLM_PROVIDER", "Gemini")
        monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
        c = importlib.reload(config)
        assert (c.LLM_PROVIDER, c.LLM_MODEL, c.GEMINI_API_KEY) == ("gemini", "gemini-2.5-flash-lite", "g-key")
        monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash")
        assert importlib.reload(config).LLM_MODEL == "gemini-2.5-flash"
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        with pytest.raises(ValueError, match="LLM_PROVIDER"):
            importlib.reload(config)
    finally:
        monkeypatch.undo()
        importlib.reload(config)
