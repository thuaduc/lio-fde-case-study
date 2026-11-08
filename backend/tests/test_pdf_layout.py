import io

from pypdf import PdfWriter

from app.services.pdf_layout import extract_layout, is_pdf, layout_to_prompt_text, row_segments

HOLLIS = "QuoteA0492_23.Pdf"
GREENMANTLE = "QT-4120-Cust-14918.pdf"


def test_rows_keep_label_and_value(quote_bytes):
    layout = extract_layout(quote_bytes(HOLLIS))
    assert any("EIN: 94 1985704" in r.text for r in layout.rows)
    assert "EIN: 94 1985704" in layout_to_prompt_text(layout)


def test_row_ids_and_pages(quote_bytes):
    layout = extract_layout(quote_bytes(GREENMANTLE))
    assert layout.page_count == 2
    page2 = [r for r in layout.rows if r.page == 2]
    assert page2
    assert all(r.id.startswith("p2-") for r in page2)
    assert layout.rows[0].id == "p1-r001"


def test_boxes_are_fractions(quote_bytes):
    layout = extract_layout(quote_bytes(HOLLIS))
    assert layout.rows
    for r in layout.rows:
        b = r.box
        assert b.page == r.page
        for v in (b.x0, b.y0, b.x1, b.y1):
            assert 0 <= v <= 1
        assert b.x0 < b.x1 and b.y0 < b.y1


def test_rows_carry_word_positions(quote_bytes):
    layout = extract_layout(quote_bytes(HOLLIS))
    for r in layout.rows:
        assert r.words
        assert r.char_width > 0
        for w in r.words:
            assert 0 <= w.x0 < w.x1 <= 1
    assert "x0" not in layout_to_prompt_text(layout)


def test_row_segments_split_on_wide_gaps(quote_bytes):
    layout = extract_layout(quote_bytes(HOLLIS))
    row = next(r for r in layout.rows if "Moss panel" in r.text)
    segs = row_segments(row)
    assert len(segs) > 1
    assert any(s.startswith("Moss panel") for s in segs)


def test_blank_pdf_has_no_rows():
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    layout = extract_layout(buf.getvalue())
    assert layout.rows == []
    assert layout.page_count == 1
    assert layout_to_prompt_text(layout) == ""


def test_is_pdf():
    assert is_pdf(b"%PDF-1.7 ...")
    assert not is_pdf(b"hello")
    assert not is_pdf(b"")
