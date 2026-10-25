from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOSEC_", env_file=".env", extra="ignore", case_sensitive=False
    )

    database_url: str = "postgresql+psycopg://tosec:tosec@localhost:5432/tosec_index"
    cache_dir: Path = Path(".tosec-index-cache")
    input_dirs: list[Path] = Field(default_factory=list)
    include_path_keywords: list[str] = Field(default_factory=list)
    pdf_dpi: int = 200
    page_image_max_dimension: int = 4000

    ocr_backend: str = "tesseract"
    ocr_languages: str = "eng+fra"
    tesseract_cmd: str = "tesseract"
    pdf_text_min_chars: int = 40
    ocr_max_image_dimension: int = 3000

    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_model: str = "local-model"
    llm_model_version: str = "unknown"
    llm_api_key: str = "not-needed"
    llm_timeout_seconds: float = 180
    llm_max_output_tokens: int = 4096
    llm_response_format: str = "json_schema"
    prompt_path: Path = Path("prompts/extract_mentions.txt")
    index_prompt_path: Path = Path("prompts/extract_index_entries.txt")
    index_discovery_threshold: float = 5.0
    index_candidate_reocr_psm: int = 4
    index_llm_max_output_tokens: int = 3072
    index_extraction_strategy: str = "text"

    def ensure_cache_dirs(self) -> None:
        (self.cache_dir / "pages").mkdir(parents=True, exist_ok=True)
