"""Tests for the tiered PDF parsing pipeline (Tier 0 Fast / Tier 1 Auto OCR).

Fixtures are generated at test time with PyMuPDF so the suite is self-contained
(no committed binary PDFs). The OCR-live tests skip when the optional OCR
dependency group isn't installed; the fallback / degradation / cache tests run
on the base install alone.
"""

import logging

import pytest

# Silence RapidOCR's chatty INFO logs during the test run.
logging.getLogger("RapidOCR").setLevel(logging.WARNING)

import file_processor  # noqa: E402
from file_processor import parse_pdf  # noqa: E402
import pdf_capabilities  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: generate PDFs with PyMuPDF
# ---------------------------------------------------------------------------

def _has_fitz() -> bool:
    try:
        import fitz  # noqa: F401
        return True
    except Exception:
        return False


needs_fitz = pytest.mark.skipif(not _has_fitz(), reason="PyMuPDF not installed")


def _make_native_pdf() -> bytes:
    """A PDF with a real text layer (Tier 0 should read it directly)."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), "Introduction to Comparative Genomics", fontsize=22)
    page.insert_text(
        (72, 140),
        "This sentence is long enough to exceed the deficient-page char threshold.",
        fontsize=14,
    )
    return doc.tobytes()


def _make_scanned_pdf() -> bytes:
    """An image-only 'scanned' PDF: text rendered to a pixmap, embedded as a
    full-page image. The native text layer is empty, so OCR is required."""
    import fitz
    src = fitz.open()
    sp = src.new_page()
    sp.insert_text((72, 240), "Scanned Page Text 456", fontsize=36)
    pix = sp.get_pixmap(dpi=200)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pix)
    return doc.tobytes()


def _make_mixed_pdf() -> bytes:
    """Page 1 = native text; page 2 = image-only. Auto should OCR only page 2."""
    import fitz
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 96), "Native first page with enough body text here.", fontsize=16)
    # page 2: image-only
    src = fitz.open()
    sp = src.new_page()
    sp.insert_text((72, 240), "Image Only Page 789", fontsize=36)
    pix = sp.get_pixmap(dpi=200)
    p2 = doc.new_page()
    p2.insert_image(p2.rect, pixmap=pix)
    return doc.tobytes()


def _figure_image_bytes(text: str = "Figure 1: Axis Title 42", size=(400, 300)) -> bytes:
    """A PNG image containing rendered text (a stand-in figure with text in it)."""
    import fitz
    fig = fitz.open()
    fp = fig.new_page(width=size[0], height=size[1])
    fp.insert_text((20, size[1] // 2), text, fontsize=24)
    return fp.get_pixmap(dpi=150).tobytes("png")


def _make_pdf_with_figure() -> bytes:
    """A PDF page with body text + an inline figure image that itself contains
    text. The page is NOT deficient (it has body text), so only per-image
    figure OCR — not page-level OCR — recovers the figure text."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Main body text sentence one here enough chars.", fontsize=14)
    page.insert_image(fitz.Rect(72, 100, 372, 250), stream=_figure_image_bytes())
    return doc.tobytes()


@pytest.fixture(autouse=True)
def _reset_caches():
    """Start each test with a clean capability cache + empty parse cache."""
    pdf_capabilities.reset_pdf_capabilities_cache()
    file_processor._PDF_PARSE_CACHE.clear()
    yield
    pdf_capabilities.reset_pdf_capabilities_cache()
    file_processor._PDF_PARSE_CACHE.clear()


# ---------------------------------------------------------------------------
# Tier 0 — Fast
# ---------------------------------------------------------------------------

@needs_fitz
def test_fast_native_extracts_text_without_ocr():
    doc = parse_pdf(_make_native_pdf(), "native.pdf", mode="fast")
    assert doc.parse_mode == "fast"
    assert "Introduction to Comparative Genomics" in doc.full_text
    assert doc.ocr_pages == []
    assert doc.ocr_deficient_pages == []


@needs_fitz
def test_fast_scanned_returns_no_text():
    # Fast never OCRs — an image-only page yields empty text.
    doc = parse_pdf(_make_scanned_pdf(), "scanned.pdf", mode="fast")
    assert doc.parse_mode == "fast"
    assert doc.full_text.strip() == ""
    assert doc.ocr_pages == []


# ---------------------------------------------------------------------------
# Tier 1 — Auto (OCR on deficient pages)
# ---------------------------------------------------------------------------

def _ocr_available() -> bool:
    return pdf_capabilities.get_pdf_capabilities(force=True)["auto"]


@needs_fitz
def test_auto_native_does_not_ocr():
    doc = parse_pdf(_make_native_pdf(), "native.pdf", mode="auto")
    assert doc.parse_mode == "auto"
    assert "Introduction to Comparative Genomics" in doc.full_text
    assert doc.ocr_pages == []  # nothing deficient → nothing OCR'd
    assert doc.ocr_deficient_pages == []


@needs_fitz
@pytest.mark.skipif(not _ocr_available(), reason="OCR deps not installed")
def test_auto_scanned_recovers_text_via_ocr():
    doc = parse_pdf(_make_scanned_pdf(), "scanned.pdf", mode="auto")
    assert doc.parse_mode == "auto"
    assert doc.ocr_pages == [1]
    assert doc.ocr_deficient_pages == []
    # OCR should recover the rendered string (digits are a stable signal).
    assert "456" in doc.full_text


@needs_fitz
@pytest.mark.skipif(not _ocr_available(), reason="OCR deps not installed")
def test_auto_mixed_ocrs_only_image_page():
    native_marker = "Native first page with enough body text here."
    doc = parse_pdf(_make_mixed_pdf(), "mixed.pdf", mode="auto")
    assert doc.parse_mode == "auto"
    # Native page preserved unchanged (no OCR).
    assert native_marker in doc.full_text
    assert 1 not in doc.ocr_pages
    # Image-only page OCR'd.
    assert 2 in doc.ocr_pages
    assert "789" in doc.full_text


# ---------------------------------------------------------------------------
# Degradation + failure fallback (run on base install too)
# ---------------------------------------------------------------------------

@needs_fitz
def test_auto_degrades_when_ocr_unavailable(monkeypatch):
    # Pretend the OCR group is missing.
    monkeypatch.setattr(
        pdf_capabilities, "_CAPS_CACHE",
        {"fast": True, "auto": False, "deep": False, "ocr_reason": "test-missing",
         "renderer": None, "install_hint": "Run: ./start.sh --ocr"},
    )
    monkeypatch.setattr(pdf_capabilities, "_OCR_ENGINE", None)
    monkeypatch.setattr(pdf_capabilities, "_OCR_ENGINE_TRIED", True)

    doc = parse_pdf(_make_scanned_pdf(), "scanned.pdf", mode="auto")
    # No crash; degraded to Fast-equivalent; the unreadable page is flagged.
    assert doc.parse_mode == "auto"
    assert doc.ocr_pages == []
    assert doc.ocr_deficient_pages == [1]
    assert doc.ocr_deficient_reason == "not_installed"
    assert doc.full_text.strip() == ""


@needs_fitz
def test_auto_falls_back_when_ocr_fails_per_page(monkeypatch):
    # OCR "available" but every page render raises — must keep Tier 0 and flag.
    class _BrokenEngine:
        def __call__(self, img):
            raise RuntimeError("simulated OCR failure")

    monkeypatch.setattr(pdf_capabilities, "get_ocr_engine", lambda: _BrokenEngine())
    # Force capabilities to report auto=True (renderer + engine present).
    monkeypatch.setattr(
        pdf_capabilities, "_CAPS_CACHE",
        {"fast": True, "auto": True, "deep": False, "ocr_reason": None,
         "renderer": "pymupdf", "install_hint": "Run: ./start.sh --ocr"},
    )

    doc = parse_pdf(_make_scanned_pdf(), "scanned.pdf", mode="auto")
    assert doc.parse_mode == "auto"
    assert doc.ocr_pages == []           # nothing recovered
    assert doc.ocr_deficient_pages == [1]  # the failed page is flagged
    assert doc.ocr_deficient_reason == "page_failed"


@needs_fitz
def test_auto_over_cap_flags_over_cap_reason(monkeypatch):
    # OCR is installed, but the doc has more deficient pages than the per-doc
    # cap — OCR is skipped entirely and the reason distinguishes this from
    # "not installed" (the UI wording differs for each).
    import config
    monkeypatch.setattr(config, "PDF_OCR_MAX_PAGES", 0)  # 1 deficient page > 0
    monkeypatch.setattr(config, "PDF_OCR_EMBEDDED_IMAGES", False)  # skip figure OCR
    monkeypatch.setattr(
        pdf_capabilities, "_CAPS_CACHE",
        {"fast": True, "auto": True, "deep": False, "ocr_reason": None,
         "renderer": "pymupdf", "install_hint": "Run: ./start.sh --ocr"},
    )

    doc = parse_pdf(_make_scanned_pdf(), "scanned.pdf", mode="auto")
    assert doc.parse_mode == "auto"
    assert doc.ocr_pages == []           # OCR skipped, nothing recovered
    assert doc.ocr_deficient_pages == [1]
    assert doc.ocr_deficient_reason == "over_cap"


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------

def test_capability_detection_shape():
    caps = pdf_capabilities.get_pdf_capabilities(force=True)
    assert caps["fast"] is True          # pdfplumber is a base dep
    assert caps["deep"] is False         # Docling deferred
    assert "auto" in caps and isinstance(caps["auto"], bool)
    assert "install_hint" in caps


def test_capability_cache_is_resettable():
    pdf_capabilities.get_pdf_capabilities(force=True)
    assert pdf_capabilities._CAPS_CACHE is not None
    pdf_capabilities.reset_pdf_capabilities_cache()
    assert pdf_capabilities._CAPS_CACHE is None


# ---------------------------------------------------------------------------
# Parse cache (decision 6)
# ---------------------------------------------------------------------------

@needs_fitz
def test_parse_cache_returns_same_object_for_same_input():
    content = _make_native_pdf()
    a = parse_pdf(content, "a.pdf", mode="fast")
    b = parse_pdf(content, "a.pdf", mode="fast")
    assert a is b  # cached


@needs_fitz
def test_parse_cache_separates_by_mode():
    content = _make_native_pdf()
    fast = parse_pdf(content, "a.pdf", mode="fast")
    auto = parse_pdf(content, "a.pdf", mode="auto")
    assert fast is not auto
    assert fast.parse_mode == "fast"
    assert auto.parse_mode == "auto"


@needs_fitz
def test_parse_cache_invalidates_on_parser_version_bump(monkeypatch):
    import config
    content = _make_native_pdf()
    v1 = parse_pdf(content, "a.pdf", mode="fast")
    monkeypatch.setattr(config, "PDF_PARSER_VERSION", config.PDF_PARSER_VERSION + 1)
    v2 = parse_pdf(content, "a.pdf", mode="fast")
    assert v1 is not v2  # version is part of the cache key


# ---------------------------------------------------------------------------
# Per-image figure OCR (auto mode only)
# ---------------------------------------------------------------------------

@needs_fitz
@pytest.mark.skipif(not _ocr_available(), reason="OCR deps not installed")
def test_auto_ocrs_embedded_figure_image():
    content = _make_pdf_with_figure()
    doc = parse_pdf(content, "fig.pdf", mode="auto")
    assert doc.parse_mode == "auto"
    # Body text preserved.
    assert "Main body text" in doc.full_text
    # Figure text recovered via per-image OCR (the page isn't deficient, so
    # only the figure-image OCR path could have produced this).
    assert "42" in doc.full_text
    assert "[Figure text (page 1, OCR):" in doc.full_text
    assert doc.figure_ocr_pages == [1]
    assert doc.figure_ocr_count == 1


@needs_fitz
def test_fast_mode_does_not_ocr_figures():
    content = _make_pdf_with_figure()
    doc = parse_pdf(content, "fig.pdf", mode="fast")
    assert doc.parse_mode == "fast"
    assert doc.figure_ocr_count == 0
    assert "42" not in doc.full_text
    # Body text still present.
    assert "Main body text" in doc.full_text


@needs_fitz
@pytest.mark.skipif(not _ocr_available(), reason="OCR deps not installed")
def test_figure_ocr_skips_tiny_images(monkeypatch):
    import config
    # Demand an image larger than the figure fixture, so it gets skipped.
    monkeypatch.setattr(config, "PDF_OCR_IMAGE_MIN_PIXELS", 10_000_000)
    content = _make_pdf_with_figure()
    doc = parse_pdf(content, "fig.pdf", mode="auto")
    assert doc.figure_ocr_count == 0
    assert "42" not in doc.full_text


@needs_fitz
@pytest.mark.skipif(not _ocr_available(), reason="OCR deps not installed")
def test_figure_ocr_respects_cap(monkeypatch):
    import config
    # Cap at 0 images → none OCR'd even though a figure is present.
    monkeypatch.setattr(config, "PDF_OCR_MAX_IMAGES", 0)
    content = _make_pdf_with_figure()
    doc = parse_pdf(content, "fig.pdf", mode="auto")
    assert doc.figure_ocr_count == 0
    assert "42" not in doc.full_text


# ---------------------------------------------------------------------------
# Google Doc routing (export-as-PDF → parse_pdf, no Drive needed)
# ---------------------------------------------------------------------------

@needs_fitz
@pytest.mark.skipif(not _ocr_available(), reason="OCR deps not installed")
def test_gdoc_routes_through_parse_pdf_with_figure_ocr():
    from drive_sync import _parse_downloaded
    pdf = _make_pdf_with_figure()
    file_dict = {
        "name": "My Notes.gdoc",
        "mimeType": "application/vnd.google-apps.document",
        "size": len(pdf),
    }
    downloaded = {"content_bytes": pdf.hex(), "mimeType": "application/pdf"}
    doc = _parse_downloaded(file_dict, downloaded, pdf_mode="auto")
    assert doc.parse_mode == "auto"
    assert doc.raw_format == "pdf"
    assert "Main body text" in doc.full_text
    assert "42" in doc.full_text  # figure OCR ran via the gdoc→PDF→parse_pdf path
    assert doc.figure_ocr_count == 1


@needs_fitz
def test_gdoc_comment_file_extracts_comments_from_parsed_text(monkeypatch):
    # A Google Doc comment file is now a PDF (content_bytes). The comment loop
    # must parse_pdf it and run extract_reviewer_comments on full_text.
    from drive_sync import _parse_downloaded
    from file_processor import extract_reviewer_comments
    # Build a PDF whose text layer holds a reviewer-comment block.
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    body = (
        "Reviewer 1, Comment 1: Please clarify the sample size used in figure 3 "
        "and justify the statistical test chosen for that comparison."
    )
    page.insert_text((72, 96), body, fontsize=12)
    pdf = doc.tobytes()
    file_dict = {
        "name": "Reviewer Comments.gdoc",
        "mimeType": "application/vnd.google-apps.document",
        "size": len(pdf),
    }
    downloaded = {"content_bytes": pdf.hex(), "mimeType": "application/pdf"}
    parsed = _parse_downloaded(file_dict, downloaded, pdf_mode="auto")
    comments = extract_reviewer_comments(parsed.full_text)
    assert len(comments) >= 1
    assert comments[0].reviewer == "Reviewer 1"
