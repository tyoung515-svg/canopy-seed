"""
research_engine.py — Iterative Web Research Engine

WHY THIS EXISTS:
Big Brain needs external context to give good answers about unfamiliar
domains. This engine fills that gap without requiring the user to provide it.
When Big Brain emits RESEARCH: <query>, this engine finds and synthesizes
the answer automatically.

DESIGN DECISIONS:
- DuckDuckGo HTML endpoint (no API key needed, no rate-limit account)
- Flash Lite for all synthesis calls — cheapest capable model, keeps cost negligible
- 500-char trim per source — enough for synthesis, not enough to blow token budget
- Parallel fetch with graceful skip on failure — never blocks the conversation

OWNED BY: Agent CS1 (Anti/Gemini Pro) — Canopy Seed V1, 2026-02-25
REVIEWED BY: Claude Sonnet 4.6 (Orchestrator)
"""

import asyncio
import logging
import re
import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Import the dataclass from context_builder to avoid circular imports
# (if needed, define a local ResearchEntry instead)
try:
    from core.context_builder import ResearchEntry
except ImportError:
    from dataclasses import dataclass, field
    @dataclass
    class ResearchEntry:
        query: str
        summary: str
        citations: list
        timestamp: str = field(default_factory=lambda: datetime.datetime.now().isoformat())

DDG_URL = "https://html.duckduckgo.com/html/"
FETCH_TIMEOUT = 8.0
MAX_CONTENT_PER_SOURCE = 500


class ResearchEngine:
    """
    Iterative research engine for Canopy Seed's Big Brain.
    Given a natural language research query, fans out web searches,
    fetches top results, and synthesizes a concise summary with citations.
    """

    def __init__(self, ai_backend, settings):
        self.ai_backend = ai_backend
        self.settings = settings
        self.max_queries = getattr(
            getattr(settings, 'canopy', None), 'research_queries_per_request', 4
        )

    async def research(self, query: str) -> ResearchEntry:
        """
        Main entry point.
        1. Generate targeted search queries from the main query
        2. Fetch top results for each
        3. Synthesize into a summary with citations
        Returns a ResearchEntry.
        """
        logger.info(f"Researching: {query}")

        # Step 1: Generate targeted queries
        queries = await self._generate_search_queries(query)
        if not queries:
            queries = [query]  # Fallback: use original query directly

        # Step 2: Fetch results in parallel
        fetch_tasks = [self._fetch_ddg(q) for q in queries[:self.max_queries]]
        results_nested = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        # Flatten and filter out failures
        results = []
        for r in results_nested:
            if isinstance(r, Exception):
                continue
            if isinstance(r, list):
                results.extend(r)

        if not results:
            return ResearchEntry(
                query=query,
                summary="No results found for this research query.",
                citations=[]
            )

        # Step 3: Synthesize
        summary, citations = await self._synthesize(query, results)
        return ResearchEntry(query=query, summary=summary, citations=citations)

    async def _generate_search_queries(self, query: str) -> list:
        """Ask Flash Lite to generate targeted search queries."""
        prompt = (
            f"Generate {self.max_queries} specific web search queries to research: \"{query}\"\n"
            "Return ONLY the queries, one per line, no numbering, no explanation."
        )
        try:
            raw = await self.ai_backend._gemini_complete(
                system="You generate targeted web search queries. Be specific and varied.",
                messages=[{"role": "user", "content": prompt}],
                model="gemini-2.5-flash"
            )
            lines = [l.strip() for l in raw.strip().split('\n') if l.strip()]
            return lines[:self.max_queries]
        except Exception as e:
            logger.warning(f"Query generation failed: {e} — falling back to original query")
            return [query]

    async def _fetch_ddg(self, query: str) -> list:
        """
        Fetch DuckDuckGo HTML search results for a query.
        Returns list of {url, title, snippet} dicts.
        SAFETY: DuckDuckGo HTML does not require login or API key.
        """
        results = []
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                resp = await client.post(
                    DDG_URL,
                    data={"q": query, "b": "", "kl": ""},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; CanopySeed/1.0)"}
                )
                html = resp.text
                # Parse result snippets from DDG HTML response
                # DDG HTML wraps results in <a class="result__a"> and <a class="result__snippet">
                urls = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"', html)
                snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)

                for i, url in enumerate(urls[:2]):  # Top 2 per query
                    snippet = re.sub(r'<[^>]+>', '', snippets[i]) if i < len(snippets) else ""
                    results.append({
                        "url": url,
                        "title": url,
                        "content": snippet[:MAX_CONTENT_PER_SOURCE]
                    })
        except Exception as e:
            logger.warning(f"DDG fetch failed for '{query}': {e}")

        return results

    async def _synthesize(self, query: str, results: list) -> tuple:
        """
        Synthesize search results into a 2-3 sentence summary with citations.
        Uses Flash Lite for cost control.
        Returns (summary_str, citations_list).
        """
        sources_text = "\n".join(
            f"Source {i+1} ({r['url']}): {r['content']}"
            for i, r in enumerate(results[:8])  # Cap at 8 sources
        )
        citations = [r["url"] for r in results[:8]]

        prompt = (
            f"Research question: {query}\n\n"
            f"Sources:\n{sources_text}\n\n"
            "Write a 2-3 sentence summary answering the research question based on these sources. "
            "Be factual and concise. Do not add opinions."
        )
        try:
            summary = await self.ai_backend._gemini_complete(
                system="You synthesize web search results into concise factual summaries.",
                messages=[{"role": "user", "content": prompt}],
                model="gemini-2.5-flash"
            )
            return summary.strip(), citations
        except Exception as e:
            logger.error(f"Synthesis failed: {e}")
            return f"Research completed but synthesis failed: {e}", citations
