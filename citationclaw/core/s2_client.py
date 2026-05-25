"""Semantic Scholar API client for academic metadata.

API docs: https://api.semanticscholar.org/
Free tier: 1 req/s without key, higher with API key.
Unique fields: h_index, influentialCitationCount.
"""
import asyncio
from typing import Optional, List
from urllib.parse import quote

from citationclaw.core.http_utils import make_async_client

BASE_URL = "https://api.semanticscholar.org/graph/v1"

_s2_global_sem: Optional[asyncio.Semaphore] = None  # Initialized per-client


class S2Client:
    def __init__(self, api_key: Optional[str] = None):
        global _s2_global_sem
        self._client = make_async_client(timeout=30.0)
        self._has_key = bool(api_key)
        if api_key:
            self._client.headers["x-api-key"] = api_key
            self._rate_delay = 0.4  # With key: ~2.5 req/s per slot
            if _s2_global_sem is None:
                _s2_global_sem = asyncio.Semaphore(2)  # 2 concurrent × 0.4s = ~5 req/s
        else:
            self._rate_delay = 1.1  # Free tier: 1 req/s
            if _s2_global_sem is None:
                _s2_global_sem = asyncio.Semaphore(1)

    async def search_paper(self, title: str) -> Optional[dict]:
        url = self._build_search_url(title)
        for attempt in range(3):
            async with _s2_global_sem:
                await asyncio.sleep(self._rate_delay)
                try:
                    resp = await self._client.get(url)
                except Exception:
                    return None
            if resp.status_code == 429:
                # Rate limited — back off and retry
                await asyncio.sleep(3 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("data", [])
            if not results:
                return None
            return self._parse_paper(results[0])
        return None  # All retries exhausted

    @staticmethod
    def _titles_match(query: str, result: str, threshold: float = 0.45) -> bool:
        """Check title similarity by word overlap.

        Threshold lowered from 0.7 to 0.45 (PaperRadar uses no validation at all).
        Handles: Chinese titles, abbreviations, minor variations.
        """
        import re as _re
        _stop = {'a', 'an', 'the', 'of', 'in', 'on', 'for', 'and', 'or', 'to',
                 'with', 'by', 'is', 'are', 'from', 'at', 'as', 'its', 'via', 'using'}

        # If query contains Chinese chars, S2 may return English translation
        # → accept if any significant word matches (very lenient for cross-language)
        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in query)
        if has_cjk:
            # Extract any English/pinyin words from both
            q_eng = set(_re.findall(r'[a-zA-Z]{3,}', query.lower()))
            r_eng = set(_re.findall(r'[a-zA-Z]{3,}', result.lower()))
            if q_eng and r_eng:
                return len(q_eng & r_eng) >= 1  # Any shared English word
            return True  # Can't compare → accept (let user verify)

        q_words = set(_re.sub(r'[^\w\s]', ' ', query.lower()).split()) - _stop
        r_words = set(_re.sub(r'[^\w\s]', ' ', result.lower()).split()) - _stop
        if not q_words:
            return True
        if len(q_words) <= 3:
            return len(q_words & r_words) >= 1
        return len(q_words & r_words) / len(q_words) >= threshold

    async def search_by_url(self, paper_url: str) -> Optional[dict]:
        """Search S2 by external URL (paper_link from GS).

        S2 supports: /paper/URL:{encoded_url}?fields=...
        This works for IEEE, arXiv, ACM, Springer etc. URLs.
        """
        if not paper_url:
            return None
        fields = "title,year,authors,citationCount,influentialCitationCount,externalIds,openAccessPdf,venue,publicationVenue,journal"
        encoded = quote(paper_url, safe='')
        url = f"{BASE_URL}/paper/URL:{encoded}?fields={fields}"
        async with _s2_global_sem:
            await asyncio.sleep(self._rate_delay)
            try:
                resp = await self._client.get(url)
            except Exception:
                return None
        if resp.status_code != 200:
            return None
        try:
            return self._parse_paper(resp.json())
        except Exception:
            return None

    async def get_author(self, author_id: str) -> Optional[dict]:
        url = f"{BASE_URL}/author/{author_id}?fields=name,hIndex,citationCount,affiliations"
        async with _s2_global_sem:
            await asyncio.sleep(self._rate_delay)
            resp = await self._client.get(url)
        if resp.status_code != 200:
            return None
        return self._parse_author(resp.json())

    def _build_search_url(self, title: str) -> str:
        # NOTE: Do NOT include authors.affiliations — it causes S2 to return empty author names!
        # Affiliations are supplemented later from OpenAlex or PDF extraction.
        fields = "title,year,authors,citationCount,influentialCitationCount,externalIds,isOpenAccess,openAccessPdf,venue,publicationVenue,journal"
        return f"{BASE_URL}/paper/search?query={quote(title)}&limit=1&fields={fields}"

    def _parse_paper(self, paper: dict) -> dict:
        authors = []
        for author in paper.get("authors", []):
            # Don't request authors.affiliations — it breaks author name!
            # Affiliations supplemented from OpenAlex or PDF later.
            authors.append({
                "name": author.get("name", ""),
                "s2_id": author.get("authorId", ""),
                "affiliation": "",
            })
        ext_ids = paper.get("externalIds", {}) or {}
        pdf_info = paper.get("openAccessPdf") or {}
        venue = (paper.get("venue", "")
                 or (paper.get("publicationVenue") or {}).get("name", "")
                 or (paper.get("journal") or {}).get("name", ""))

        # PDF URL fallback chain (PaperRadar-style: construct at metadata stage)
        arxiv_id = ext_ids.get("ArXiv", "")
        doi = ext_ids.get("DOI", "")
        pdf_url = pdf_info.get("url", "")
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        if not pdf_url and doi:
            pdf_url = f"https://doi.org/{doi}"

        return {
            "title": paper.get("title", ""),
            "year": paper.get("year"),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "cited_by_count": paper.get("citationCount", 0),
            "influential_citation_count": paper.get("influentialCitationCount", 0),
            "s2_id": paper.get("paperId", ""),
            "authors": authors,
            "pdf_url": pdf_url,
            "venue": venue,
            "_external_ids": ext_ids,
            "source": "s2",
        }

    def _parse_author(self, author: dict) -> dict:
        affiliations = author.get("affiliations", [])
        return {
            "name": author.get("name", ""),
            "s2_id": author.get("authorId", ""),
            "h_index": author.get("hIndex", 0),
            "citation_count": author.get("citationCount", 0),
            "affiliation": affiliations[0] if affiliations else "",
            "source": "s2",
        }

    async def close(self):
        await self._client.aclose()
