"""PDF text rows with bounding boxes (page-fraction coordinates, origin top-left)."""
import io

import pdfplumber
from pydantic import BaseModel, Field


class Box(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class Word(BaseModel):
    text: str
    x0: float
    x1: float


class PdfRow(BaseModel):
    id: str
    page: int
    text: str
    box: Box
    # Word positions (page fractions) and the row's average glyph width, kept so
    # callers can split a row into segments at wide gaps. Not sent to the LLM.
    words: list[Word] = Field(default_factory=list)
    char_width: float = 0.0


class PdfLayout(BaseModel):
    rows: list[PdfRow]
    page_count: int


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _clamp(v: float) -> float:
    return min(1.0, max(0.0, v))


def _line_words(page_words: list[dict], line: dict, width: float) -> list[Word]:
    """The page's words whose vertical centre lies within the line, left to right."""
    inside = [
        w
        for w in page_words
        if line["top"] <= (w["top"] + w["bottom"]) / 2 <= line["bottom"]
        and w["x0"] >= line["x0"] - 1
        and w["x1"] <= line["x1"] + 1
    ]
    return [
        Word(text=w["text"], x0=_clamp(w["x0"] / width), x1=_clamp(w["x1"] / width))
        for w in sorted(inside, key=lambda w: w["x0"])
    ]


def extract_layout(pdf_bytes: bytes) -> PdfLayout:
    rows: list[PdfRow] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            width, height = float(page.width), float(page.height)
            n = 0
            page_words = page.extract_words()
            for line in page.extract_text_lines(layout=False, strip=True):
                text = (line.get("text") or "").strip()
                if not text:
                    continue
                n += 1
                glyphs = [c for c in line.get("chars", []) if not c["text"].isspace()]
                char_width = (
                    sum(c["x1"] - c["x0"] for c in glyphs) / len(glyphs) / width
                    if glyphs
                    else 0.0
                )
                rows.append(
                    PdfRow(
                        id=f"p{page_no}-r{n:03d}",
                        page=page_no,
                        text=text,
                        box=Box(
                            page=page_no,
                            x0=_clamp(line["x0"] / width),
                            y0=_clamp(line["top"] / height),
                            x1=_clamp(line["x1"] / width),
                            y1=_clamp(line["bottom"] / height),
                        ),
                        words=_line_words(page_words, line, width),
                        char_width=char_width,
                    )
                )
        return PdfLayout(rows=rows, page_count=len(pdf.pages))


def layout_to_prompt_text(layout: PdfLayout) -> str:
    return "\n".join(f"[{r.id}] {r.text}" for r in layout.rows)


def row_segments(row: PdfRow, gap_factor: float = 2.0) -> list[str]:
    """Split a row into text segments wherever the gap between neighbouring words
    exceeds gap_factor x the row's average character width."""
    segments: list[list[str]] = []
    prev: Word | None = None
    for w in row.words:
        if prev is None or (w.x0 - prev.x1) > gap_factor * row.char_width:
            segments.append([])
        segments[-1].append(w.text)
        prev = w
    return [" ".join(s) for s in segments]
