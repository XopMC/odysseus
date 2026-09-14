"""Search must preserve configured engines and distinguish evidence from failure."""

import asyncio

import httpx
import pytest

from services.search import core, providers
from src.agent_tools.web_tools import WebSearchTool


class SearchResponse:
    def __init__(self, results=(), *, html=""):
        self.results = list(results)
        self.text = html
        self.is_success = True

    def raise_for_status(self):
        pass

    def json(self):
        return {"results": self.results}


@pytest.mark.parametrize("text", [
    "No search results found. Tried: searxng:empty.",
    "Web search is disabled by the administrator.",
    "Web search failed — all providers errored or returned empty.",
])
def test_agent_search_without_sources_is_not_success(monkeypatch, text):
    import src.search

    monkeypatch.setattr(src.search, "comprehensive_web_search", lambda *a, **kw: (text, []))
    result = asyncio.run(WebSearchTool().execute("Aori protocol contracts", {}))
    assert result["exit_code"] == 1
    assert text in result["error"]
    assert "no sources" in result["error"].lower()
    assert result["untrusted_content"] is True


def test_agent_search_with_snippets_but_failed_page_fetch_is_still_evidence(monkeypatch):
    import src.search

    sources = [{"url": "https://docs.python.org/3/library/asyncio-task.html", "title": "Tasks"}]
    monkeypatch.setattr(src.search, "comprehensive_web_search", lambda *a, **kw: (
        "Searched 1 results, fetched 0 pages\nSnippet: TaskGroup", sources,
    ))
    result = asyncio.run(WebSearchTool().execute("Python TaskGroup", {}))
    assert result["exit_code"] == 0
    assert "fetched 0 pages" in result["output"]
    assert "<!-- SOURCES:" in result["output"]
    assert "docs.python.org" in result["output"]


def test_empty_searxng_does_not_silently_reenable_other_engines(monkeypatch):
    seen = []

    def get(url, **kwargs):
        seen.append(dict(kwargs["params"]))
        return SearchResponse()

    monkeypatch.setattr(providers, "_GENERAL_ENGINES", "duckduckgo web,yandex")
    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://search.test")
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {})
    monkeypatch.setattr(providers.httpx, "get", get)
    assert providers.searxng_search_api("Aori protocol contracts") == []
    assert seen
    assert all(p.get("engines") == "duckduckgo web,yandex" for p in seen)
    assert all(p["q"] == "Aori protocol contracts" for p in seen)
    assert all(p["safesearch"] == "2" for p in seen)


def test_searxng_html_fallback_keeps_engine_and_safesearch_selection(monkeypatch):
    seen = []

    def get(url, **kwargs):
        params = dict(kwargs["params"])
        seen.append(params)
        if params.get("format") == "json":
            raise httpx.HTTPError("JSON not supported")
        return SearchResponse(html='<article class="result"><h3><a href="https://example.com">Result</a></h3></article>')

    monkeypatch.setattr(providers, "_GENERAL_ENGINES", "duckduckgo web,yandex")
    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://search.test")
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {"search_safesearch": "strict"})
    monkeypatch.setattr(providers.httpx, "get", get)
    result = providers.searxng_search_api("Aori protocol contracts")
    assert result[0]["url"] == "https://example.com"
    assert len(seen) == 2
    assert seen[1]["engines"] == "duckduckgo web,yandex"
    assert seen[1]["safesearch"] == "2"
    assert "format" not in seen[1]


@pytest.mark.parametrize("entrypoint", ["comprehensive_web_search", "searxng_search_results"])
def test_empty_provider_moves_to_fallback_without_duplicate_query(monkeypatch, tmp_path, entrypoint):
    calls = []

    def call(provider, *args):
        calls.append(provider)
        return []

    monkeypatch.setattr(core, "_get_search_settings", lambda: {
        "search_provider": "searxng", "search_fallback_chain": ["duckduckgo"],
    })
    monkeypatch.setattr(core, "_get_result_count", lambda: 5)
    monkeypatch.setattr(core, "_call_provider", call)
    monkeypatch.setattr(core, "_record_query", lambda *a, **kw: None)
    monkeypatch.setattr(core, "SEARCH_CACHE_DIR", tmp_path)
    getattr(core, entrypoint)("Aori protocol contracts")
    assert calls == ["searxng", "duckduckgo"]


def test_transient_provider_error_can_retry_then_use_fallback(monkeypatch):
    calls = []

    def call(provider, *args):
        calls.append(provider)
        if provider == "searxng":
            raise httpx.ConnectError("temporarily unavailable")
        return []

    monkeypatch.setattr(core, "_get_search_settings", lambda: {
        "search_provider": "searxng", "search_fallback_chain": ["duckduckgo"],
    })
    monkeypatch.setattr(core, "_get_result_count", lambda: 5)
    monkeypatch.setattr(core, "_call_provider", call)
    text, sources = core.comprehensive_web_search("Aori protocol contracts", return_sources=True)
    assert sources == []
    assert calls == ["searxng", "searxng", "duckduckgo"]
    assert "failed" in text.lower()


def test_unread_pages_are_explicitly_identified_as_snippets_only(monkeypatch):
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(core, "_get_result_count", lambda: 1)
    monkeypatch.setattr(core, "_call_provider", lambda *a: [{
        "url": "https://example.com", "title": "Example", "snippet": "Search snippet",
    }])
    monkeypatch.setattr(core, "fetch_webpage_content", lambda *a, **kw: {
        "success": False, "error": "HTTP 404", "content": "",
    })
    text, sources = core.comprehensive_web_search("example", max_pages=1, return_sources=True)
    assert sources == [{"url": "https://example.com", "title": "Example"}]
    assert "search snippets only" in text.lower()
    assert "not verified page content" in text.lower()
