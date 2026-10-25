from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Protocol

import httpx

from ..config import Settings
from .schemas import IndexPageExtraction


class InvalidIndexResponse(ValueError):
    def __init__(self, message: str, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


class IndexPageExtractor(Protocol):
    strategy: str

    def extract(
        self, prompt: str, physical_page: int, classification: str, text: str, image_path: Path
    ) -> tuple[IndexPageExtraction, str]: ...


def _json_text(content: str) -> str:
    content = content.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE
    )
    return fenced.group(1) if fenced else content


class _LlamaCppExtractor:
    strategy = "base"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _content(self, physical_page: int, classification: str, text: str, image_path: Path):
        raise NotImplementedError

    def extract(
        self, prompt: str, physical_page: int, classification: str, text: str, image_path: Path
    ) -> tuple[IndexPageExtraction, str]:
        payload = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "max_tokens": self.settings.index_llm_max_output_tokens,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": self._content(physical_page, classification, text, image_path),
                },
            ],
        }
        if self.settings.llm_response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "index_page_extraction",
                    "strict": True,
                    "schema": IndexPageExtraction.model_json_schema(),
                },
            }
        elif self.settings.llm_response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif self.settings.llm_response_format != "off":
            raise ValueError("TOSEC_LLM_RESPONSE_FORMAT must be json_schema, json_object, or off")
        with httpx.Client(
            base_url=self.settings.llm_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            timeout=self.settings.llm_timeout_seconds,
        ) as client:
            response = client.post("chat/completions", json=payload)
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
        try:
            parsed = IndexPageExtraction.model_validate(json.loads(_json_text(raw)))
        except Exception as exc:
            raise InvalidIndexResponse(str(exc), raw) from exc
        return parsed, raw


class LlamaCppTextIndexExtractor(_LlamaCppExtractor):
    strategy = "ocr_text"

    def _content(self, physical_page: int, classification: str, text: str, image_path: Path):
        return (
            f"PHYSICAL IMAGE PAGE: {physical_page}\n"
            f"DISCOVERY CLASSIFICATION: {classification}\n\n{text}"
        )


class LlamaCppVisionIndexExtractor(_LlamaCppExtractor):
    strategy = "vision"

    def _content(self, physical_page: int, classification: str, text: str, image_path: Path):
        mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return [
            {
                "type": "text",
                "text": (
                    f"Physical image page: {physical_page}. Discovery classification: "
                    f"{classification}. Extract the listings from this original page image. "
                    "The OCR evidence below is supplied to disambiguate small print and spelling. "
                    "Use the image as authority for layout.\n\n"
                    f"{text}"
                ),
            },
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ]


def make_extractor(settings: Settings, strategy: str) -> IndexPageExtractor:
    if strategy == "text":
        return LlamaCppTextIndexExtractor(settings)
    if strategy == "vision":
        return LlamaCppVisionIndexExtractor(settings)
    raise ValueError("Index extraction strategy must be 'text' or 'vision'")
