"""Runtime capability detection for the PDF parsing ladder.

Tier 0 (Fast) is always available — pdfplumber is a base dependency. Tier 1
(Auto) requires the optional OCR group (PyMuPDF renderer + RapidOCR + ONNX
Runtime), installed via ``./start.sh --ocr``. Tier 2 (Deep / Docling) is
deferred.

Detection is import + initialize only: it never runs ``pip install`` and never
downloads models. (RapidOCR ships its ONNX models inside the wheel, so
initialization is fully local.) Results are cached for the process; tests can
clear the cache with :func:`reset_pdf_capabilities_cache`.
"""

import logging
from typing import Optional

logger = logging.getLogger("paper-assistant.pdf-capabilities")

# Process-wide caches. ``_OCR_ENGINE`` holds an initialized RapidOCR so the
# parser doesn't pay the ~1 s init cost on every page; ``_CAPS_CACHE`` holds
# the last computed capability dict so the endpoint is cheap to poll.
_OCR_ENGINE: Optional[object] = None
_OCR_ENGINE_TRIED: bool = False
_CAPS_CACHE: Optional[dict] = None


def get_ocr_engine():
    """Return a cached, initialized RapidOCR engine, or None if unavailable.

    Builds the engine once per process (RapidOCR init loads its ONNX models).
    Any import/init failure is logged once and remembered via
    ``_OCR_ENGINE_TRIED`` so we don't retry on every page.
    """
    global _OCR_ENGINE, _OCR_ENGINE_TRIED
    if _OCR_ENGINE_TRIED:
        return _OCR_ENGINE
    _OCR_ENGINE_TRIED = True
    try:
        # Silence RapidOCR's chatty INFO logs during init.
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        from rapidocr import RapidOCR  # type: ignore

        _OCR_ENGINE = RapidOCR()
        logger.info("OCR engine initialized (RapidOCR + ONNX Runtime)")
    except Exception as exc:  # pragma: no cover - depends on env
        _OCR_ENGINE = None
        logger.info("OCR engine unavailable: %s", exc)
    return _OCR_ENGINE


def _renderer_available() -> bool:
    """True if PyMuPDF (the page renderer for OCR) imports."""
    try:
        import fitz  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


def get_pdf_capabilities(force: bool = False) -> dict:
    """Detect available PDF parsing tiers.

    Returns a dict:
      ``fast``  — always True (pdfplumber, base dep).
      ``auto``  — True when the OCR renderer + engine both initialize.
      ``deep``  — always False (Docling deferred).
      ``ocr_reason`` — None when Auto is available, else a short why-not string
                       for the UI install hint.
      ``renderer`` — "pymupdf" when available, else None.
    """
    global _CAPS_CACHE
    if _CAPS_CACHE is not None and not force:
        return _CAPS_CACHE

    renderer_ok = _renderer_available()
    engine = get_ocr_engine() if renderer_ok else None
    auto_ok = renderer_ok and engine is not None

    if auto_ok:
        ocr_reason = None
    elif not renderer_ok:
        ocr_reason = "PyMuPDF not installed"
    else:
        ocr_reason = "RapidOCR/ONNX not installed"

    _CAPS_CACHE = {
        "fast": True,
        "auto": auto_ok,
        "deep": False,
        "ocr_reason": ocr_reason,
        "renderer": "pymupdf" if renderer_ok else None,
        # Human-readable install hint for the panel.
        "install_hint": "Run: ./start.sh --ocr",
    }
    return _CAPS_CACHE


def reset_pdf_capabilities_cache() -> None:
    """Clear cached capability/engine state (for tests)."""
    global _OCR_ENGINE, _OCR_ENGINE_TRIED, _CAPS_CACHE
    _OCR_ENGINE = None
    _OCR_ENGINE_TRIED = False
    _CAPS_CACHE = None
