from __future__ import annotations

import json
import re

import httpx

from ..config import Settings
from .schemas import PageExtraction


class InvalidModelResponse(ValueError):
    def __init__(self, message: str, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


def _response_format(settings: Settings) -> dict[str, object] | None:
    if settings.llm_response_format == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "page_extraction",
                "strict": True,
                "schema": PageExtraction.model_json_schema(),
            },
        }
    if settings.llm_response_format == "json_object":
        return {"type": "json_object"}
    if settings.llm_response_format == "off":
        return None
    raise ValueError("TOSEC_LLM_RESPONSE_FORMAT must be json_schema, json_object, or off")


def _json_text(content: str) -> str:
    content = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1) if fenced else content


def extract_page(
    settings: Settings, system_prompt: str, page_number: int, ocr_text: str
) -> tuple[PageExtraction, str]:
    payload: dict[str, object] = {
        "model": settings.llm_model,
        "temperature": 0,
        "max_tokens": settings.llm_max_output_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"PAGE NUMBER: {page_number}\n\nOCR TEXT:\n---\n{ocr_text}\n---",
            },
        ],
    }
    response_format = _response_format(settings)
    if response_format:
        payload["response_format"] = response_format
    with httpx.Client(
        base_url=settings.llm_base_url.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=settings.llm_timeout_seconds,
    ) as client:
        response = client.post("chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
    raw = body["choices"][0]["message"]["content"]
    try:
        parsed = PageExtraction.model_validate(json.loads(_json_text(raw)))
    except Exception as exc:
        raise InvalidModelResponse(str(exc), raw) from exc
    return parsed, raw
