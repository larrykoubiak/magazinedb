from __future__ import annotations

from statistics import median
from typing import Any


def layout_lines(boxes: list[dict[str, Any]] | None) -> list[str]:
    """Rebuild approximate visual rows from OCR word coordinates.

    Tesseract's normal text is block-ordered, which often separates a title column
    from its page-number column. Visual rows are more useful for index pages.
    """
    words = [box for box in (boxes or []) if str(box.get("text", "")).strip()]
    if not words:
        return []
    heights = [float(box["height"]) for box in words if float(box.get("height", 0)) > 0]
    tolerance = max(5.0, median(heights) * 0.55) if heights else 8.0
    rows: list[list[Any]] = []
    for box in sorted(words, key=lambda item: (item["y"] + item["height"] / 2, item["x"])):
        center = box["y"] + box["height"] / 2
        closest = min(rows, key=lambda row: abs(row[0] - center), default=None)
        if closest is None or abs(closest[0] - center) > tolerance:
            rows.append([center, [box]])
        else:
            closest[1].append(box)
            closest[0] = sum(
                word["y"] + word["height"] / 2 for word in closest[1]
            ) / len(closest[1])
    result: list[str] = []
    for _center, row_words in sorted(rows, key=lambda row: row[0]):
        row_words.sort(key=lambda item: item["x"])
        result.append(" ".join(str(item["text"]).strip() for item in row_words))
    return result


def probable_page_reference_rows(
    boxes: list[dict[str, Any]] | None, page_width: int | None, maximum: int
) -> int:
    words = [box for box in (boxes or []) if str(box.get("text", "")).strip()]
    if not words or not page_width:
        return 0
    # Repeat the same visual grouping while retaining coordinates.
    heights = [float(box["height"]) for box in words if float(box.get("height", 0)) > 0]
    tolerance = max(5.0, median(heights) * 0.55) if heights else 8.0
    rows: list[list[Any]] = []
    for box in sorted(words, key=lambda item: (item["y"] + item["height"] / 2, item["x"])):
        center = box["y"] + box["height"] / 2
        closest = min(rows, key=lambda row: abs(row[0] - center), default=None)
        if closest is None or abs(closest[0] - center) > tolerance:
            rows.append([center, [box]])
        else:
            closest[1].append(box)
            closest[0] = sum(
                word["y"] + word["height"] / 2 for word in closest[1]
            ) / len(closest[1])
    count = 0
    for _center, row_words in rows:
        numeric = []
        for word in row_words:
            token = str(word["text"]).strip().strip(".,;:()[]")
            if token.isdigit() and 1 <= int(token) <= maximum:
                numeric.append(word)
        has_title = sum(any(char.isalpha() for char in str(word["text"])) for word in row_words) >= 1
        if has_title and any(word["x"] >= page_width * 0.45 for word in numeric):
            count += 1
    return count
