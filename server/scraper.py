"""Web scraper — fetch and extract paper content from journal webpages."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from file_processor import PaperDocument, _parse_sections, _extract_metadata

logger = logging.getLogger("paper-assistant.scraper")

# Browser-like User-Agent to avoid bot blocking
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Common journal domains — helps with site-specific extraction
JOURNAL_DOMAINS = {
    "nature.com",
    "science.org",
    "pnas.org",
    "cell.com",
    "nejm.org",
    "thelancet.com",
    "jama.com",
    "bmj.com",
    "springer.com",
    "wiley.com",
    "tandfonline.com",
    "acs.org",
    "rsc.org",
    "ieee.org",
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "pubmed.ncbi.nlm.nih.gov",
    "journals.plos.org",
    "frontiersin.org",
    "mdpi.com",
    "elifesciences.org",
}

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch a webpage and return its HTML content."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "DNT": "1",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


async def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    """Fetch a URL and return its raw bytes (e.g. a PDF document)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "DNT": "1",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.content


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip junk from extracted text."""
    if not text:
        return ""
    # Replace non-breaking spaces, collapse runs
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_paper_title(soup: BeautifulSoup) -> str:
    """Extract the paper title using multiple strategies.

    Strategy order:
    1. <meta name="citation_title"> — used by Nature, Science, etc.
    2. <meta property="og:title">
    3. <meta name="dc.title">
    4. <h1> with class containing 'title'
    5. First <h1>
    """
    # 1. Citation metadata (most reliable for journal sites)
    for meta_name in ("citation_title", "dc.title", "dc.Title"):
        meta = soup.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content", "").strip():
            return _clean_text(meta["content"])

    # 2. Open Graph
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content", "").strip():
        title = _clean_text(meta["content"])
        # Strip site name suffix, e.g. "Paper Title | Nature"
        title = re.split(r"\s*[|–—]\s*(?:Nature|Science|PNAS|Cell|NEJM|The Lancet|JAMA|BMJ|bioRxiv|medRxiv|arXiv|PLOS|eLife|Frontiers)", title)[0]
        return title.strip()

    # 3. Schema.org headline
    meta = soup.find("meta", itemprop="headline")
    if meta and meta.get("content", "").strip():
        return _clean_text(meta["content"])

    # 4. <h1> with title-like class
    h1 = soup.find("h1", class_=re.compile(r"title|heading|article-title", re.I))
    if h1:
        return _clean_text(h1.get_text())

    # 5. First <h1>
    h1 = soup.find("h1")
    if h1:
        return _clean_text(h1.get_text())

    return ""


def _extract_abstract(soup: BeautifulSoup) -> str:
    """Extract the abstract using multiple strategies."""
    # 1. Citation metadata
    for meta_name in ("citation_abstract", "dc.description", "description"):
        meta = soup.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content", "").strip():
            return _clean_text(meta["content"])

    # 2. Elements with abstract-related class/id
    for selector in [
        {"id": re.compile(r"abstract", re.I)},
        {"class_": re.compile(r"abstract|article-summary", re.I)},
        {"id": "Abs1"},  # common PubMed-derived structure
    ]:
        elem = soup.find(attrs=selector)
        if elem:
            # Try to find the abstract text block inside
            for tag in elem.find_all(["section", "div", "p"]):
                text = _clean_text(tag.get_text())
                if len(text) > 50:
                    return text[:5000]
            # Fall back to the whole element
            text = _clean_text(elem.get_text())
            # Strip the "Abstract" label
            text = re.sub(r"^Abstract\s*[-:.]?\s*", "", text, flags=re.I)
            if len(text) > 50:
                return text[:5000]

    # 3. <meta name="description"> as last resort
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content", "").strip():
        return _clean_text(meta["content"])[:2000]

    return ""


def _extract_body(soup: BeautifulSoup) -> str:
    """Extract the main body text, trying to exclude nav, ads, etc."""
    # Try common article content selectors first
    content_selectors = [
        # Semantic
        "article",
        'main',
        # Common journal/article class names
        '[class*="article-body"]',
        '[class*="article__body"]',
        '[class*="article-content"]',
        '[class*="main-content"]',
        '[class*="content--article"]',
        '[id*="article-body"]',
        '[id*="article-content"]',
        '[id*="main-content"]',
        # Nature
        ".c-article-body",
        ".article__body",
        # Science
        ".article__body",
        # PNAS
        ".article-content",
        # arXiv / bioRxiv
        ".ltx_page_content",
        ".fulltext",
        # Springer / Wiley
        ".c-article-section",
        ".article-section",
        # PLOS
        "#article-body",
        # eLife
        ".article-full__body",
        # MDPI
        ".html-body",
        # PubMed
        "#full-view-abstract",
    ]

    main_elem = None
    for selector in content_selectors:
        try:
            main_elem = soup.select_one(selector)
            if main_elem:
                break
        except Exception:
            continue

    if main_elem is None:
        # Fallback: extract from <body> with junk removed
        main_elem = soup.find("body")
        if main_elem is None:
            main_elem = soup

    # Remove non-content elements before extracting text
    if main_elem:
        for tag_name in ("nav", "footer", "header", "aside", "script", "style",
                         "noscript", "iframe", "form", "button"):
            for tag in main_elem.find_all(tag_name):
                tag.decompose()

        # Remove common non-content sections by class/id
        junk_patterns = [
            "nav", "menu", "sidebar", "footer", "header", "banner",
            "cookie", "advertisement", "ad-", "social", "share",
            "related", "recommend", "citation", "reference",
            "supplementary", "comment", "search", "breadcrumb",
        ]
        for pattern in junk_patterns:
            for tag in main_elem.find_all(class_=re.compile(pattern, re.I)):
                tag.decompose()
            for tag in main_elem.find_all(id=re.compile(pattern, re.I)):
                tag.decompose()

        # Extract paragraph text
        paragraphs = []
        for p in main_elem.find_all("p"):
            text = _clean_text(p.get_text())
            # Filter out short/nav paragraphs
            if len(text) > 20:
                paragraphs.append(text)

        if paragraphs:
            return "\n\n".join(paragraphs)

        # If no paragraphs found, get all visible text
        return _clean_text(main_elem.get_text())

    return ""


def extract_content(html: str, url: str) -> PaperDocument:
    """Parse HTML and extract paper content into a PaperDocument."""
    soup = BeautifulSoup(html, "lxml")

    title = _extract_paper_title(soup)
    abstract = _extract_abstract(soup)
    body = _extract_body(soup)

    # Build full text
    full_text_parts = []
    if title:
        full_text_parts.append(f"# {title}\n")
    if abstract:
        full_text_parts.append(f"## Abstract\n{abstract}\n")
    if body:
        full_text_parts.append(body)

    full_text = "\n\n".join(full_text_parts)

    # Parse sections from the body text
    sections = _parse_sections(body) if body else []

    # If no sections found, create a single "Body" section
    if not sections and body:
        from file_processor import Section
        sections = [Section(heading="Body", content=body)]

    # Derive domain from URL
    domain = urlparse(url).netloc.replace("www.", "")

    doc = PaperDocument(
        title=title or f"Scraped from {domain}",
        abstract=abstract,
        sections=sections,
        full_text=full_text,
        raw_format="html",
    )

    logger.info(
        "Scraped '%s': title=%s, abstract=%d chars, body=%d chars, %d sections",
        url,
        title[:80] if title else "N/A",
        len(abstract),
        len(body),
        len(sections),
    )

    return doc


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def scrape_url(url: str) -> PaperDocument:
    """Fetch and extract paper content from a URL."""
    html = await fetch_page(url)
    return extract_content(html, url)
