# TOSEC-PIX Magazine Indexer

A local, incremental pipeline that turns scanned PDF/CBZ/ZIP publications (or directories
of page images) into a traceable index of game listings in PostgreSQL. It is deliberately a
metadata indexer, not a chatbot or vector-search system.

The first milestone supports:

- content-hashed, non-destructive PDF, CBZ/ZIP, and image-directory ingestion (including
  TOSEC ZIPs that wrap a PDF);
- bounded per-page JPEG caches, PDF text-layer reuse, and English/French Tesseract OCR;
- cheap multilingual keyword/layout discovery of contents and index pages;
- candidate-only, Pydantic-validated extraction through llama.cpp's OpenAI-compatible endpoint;
- durable provenance, versions, raw model responses, errors, and retryable stage state;
- structured-name and OCR-substring search with source file and page locations.

## Setup on EndeavourOS

Install Python and Tesseract language data. Package names can change slightly with the
Arch repositories, but a typical installation is:

```bash
sudo pacman -S python tesseract tesseract-data-eng tesseract-data-fra
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Start PostgreSQL and apply the schema:

```bash
docker compose up -d postgres
alembic upgrade head
```

The database volume is managed by Docker. Derived page images go to
`.tosec-index-cache/pages/<document-sha256>/`; originals are only read and their resolved
paths are retained. If identical content is found at more than one path, it is indexed once
and every observed source path is retained in document metadata. PDF and archived page images
are capped at `TOSEC_PAGE_IMAGE_MAX_DIMENSION` during cache creation, preventing unusual PDF
page metadata from creating enormous working images. OCR has a separate lower size ceiling and
maps detected word boxes back into cached-image coordinates.

## Local model server

Use an instruction-tuned 7B/8B GGUF in a quantization that leaves context/KV-cache room in
8 GB VRAM (Q4_K_M is a practical starting point). Models are never downloaded by this
project. For a recent llama.cpp build, a representative command is:

```bash
llama-server \
  --model /path/to/your-instruct-model.Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 \
  --ctx-size 4096 --n-gpu-layers 99
```

Set `TOSEC_LLM_MODEL` to the name reported by that server if it requires a particular
model identifier and set `TOSEC_LLM_MODEL_VERSION` to the GGUF/model release you run.
Recent llama.cpp versions support `json_schema`; use
`TOSEC_LLM_RESPONSE_FORMAT=json_object` for an older build, or `off` only for debugging.
The response is Pydantic-validated in every mode.

Tesseract is CPU-based, so no OCR model competes with llama.cpp for VRAM. Run the stages
separately as shown below; a future PaddleOCR backend can explicitly stop the llama server
while using GPU OCR.

## Default index workflow

```bash
# One source or an arbitrary collection root (directories are recursively discovered)
tosec-index scan '/data/TOSEC-PIX/Amiga Power (1992)(Issue 10).pdf'

# Resumable batch stages
tosec-index ocr --pending
tosec-index discover-indexes --document 1
tosec-index extract-indexes --document 1

# Or run those three resumable stages for one document
tosec-index index --document 1

tosec-index status
tosec-index search 'Dungeon Master'
```

Search returns structured index subjects by default, avoiding incidental names in advertisements
and prose. Add `--include-ocr` only when you deliberately want optional deep-extraction records
and broad raw-text matches.

`discover-indexes` uses cached OCR text and word coordinates only; it prints every high-scoring
page with its score and reasons. `extract-indexes` runs a focused Tesseract layout pass and the
local model only on selected pages. Use `--source-page 29 --force` to debug one candidate.
`TOSEC_INDEX_EXTRACTION_STRATEGY` selects `text` or `vision`; vision supplies both the original
page and OCR evidence to a compatible llama.cpp model/projector.

By default only `reviews` and `previews` are accepted from listing pages. This conservative
boundary avoids unstable guesses from news blurbs, advertisements, captions, and hardware
features. Set `TOSEC_INDEX_SECTIONS` explicitly if broader sections are wanted later; the setting
is included in extraction provenance, so changing it creates a distinct result version.

For debugging, select one imported document by numeric ID, exact filename, or original
path. A rerun creates new versioned results rather than overwriting old provenance:

```bash
tosec-index ocr --document 1 --force
tosec-index discover-indexes --document 1
tosec-index extract-indexes --document 1 --force --verbose
```

The older broad page-by-page extractor remains available as explicit deep mode:

```bash
tosec-index extract --document 1
```

It is never invoked automatically when no usable index is found. Such a document is marked
`requires_deep_processing` so the decision can be made separately.

`TOSEC_INPUT_DIRS` may be a JSON list such as `["/data/TOSEC-PIX", "/data/books"]`; then
`tosec-index scan` needs no argument. `TOSEC_INCLUDE_PATH_KEYWORDS` filters against every
path component, which is useful when archives live beneath category directories but their
own filenames do not say “magazine” or “manual”. Use `scan --all-categories` to bypass the
filter, `scan --dry-run` to inspect matches without hashing, or `scan --limit 1` for a safe
first import; a limit also stops the directory walk as soon as it is reached. See
[.env.example](.env.example) for all runtime settings. Prompts are intentionally editable in
[prompts/extract_index_entries.txt](prompts/extract_index_entries.txt) and the optional deep-mode
[prompts/extract_mentions.txt](prompts/extract_mentions.txt); prompt content and its JSON schema
are hashed together, so edits create a new extraction version automatically.

## Pipeline and provenance

Each document has a content hash and ordered pages. Each OCR attempt points to exactly one
page and records engine, version, languages, confidence, word boxes (when Tesseract supplies
them), text, status, and error. A usable PDF text layer is stored as a successful
`pdf-text` result with no synthetic boxes.

Each index discovery run retains scores, classifications, and reasons for every physical page.
Index entries separately retain the source index image page and the printed destination page;
the latter is not converted to a physical target unless it can be verified. Candidate extraction
runs store strategy, model, prompt version, exact OCR input, raw response, errors, and duration.

Each optional deep extraction attempt points to one OCR result and stores model name, prompt fingerprint,
raw response, validation error, and timestamps. A mention keeps the printed spelling,
page, exact supporting OCR substring, context, confidence, and uncertainty. `entities`
only groups identical normalized spellings and types; it does **not** resolve them to a
canonical SoftwareDb identity.

OCR and extraction commit after every page. A process interruption therefore loses at most
the in-flight page; rerunning `--pending` skips successful page/model/prompt combinations.

When a title appears on both a general contents page and a dedicated review index, search returns
one result per publication and prefers the dedicated index pointer.

At present search uses portable PostgreSQL `ILIKE` substring matching. This is adequate for
the vertical slice and avoids committing to a language-specific full-text configuration;
a trigram or full-text index is an incremental migration once corpus size makes it useful.

## Tests

```bash
pytest
ruff check .
```

The regression tests use SQLite only as an isolated test store. One exercises the complete
PDF → text-layer OCR → mocked local-model extraction → search path; another verifies CBZ
page ordering and content deduplication. Production and migrations target PostgreSQL.
