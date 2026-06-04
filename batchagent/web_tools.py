from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from typing import Any

from .models import BatchConfig


class WebToolError(RuntimeError):
    pass


def web_fetch(config: BatchConfig, url_input: str, prompt: str = "", max_chars: int | None = None) -> dict[str, Any]:
    url = _normalize_http_url(url_input)
    limit = _bounded_limit(max_chars if max_chars is not None else config.web_max_chars, 1000, 80000)
    response_url, status, content_type, raw = _fetch_text(url, config.web_timeout_seconds)
    text = html_to_text(raw) if "html" in content_type.lower() else raw
    clipped = text[:limit]
    return {
        "url": url,
        "final_url": response_url,
        "status": status,
        "ok": 200 <= status < 300,
        "content_type": content_type,
        "title": extract_title(raw) if "html" in content_type.lower() else "",
        "prompt": prompt,
        "content": clipped,
        "truncated": len(text) > len(clipped),
    }


def web_search(config: BatchConfig, query: str, limit: int = 5, domains: list[str] | None = None) -> dict[str, Any]:
    clean_limit = max(1, min(int(limit), 10))
    domain_filter = ""
    if domains:
        clean_domains = [domain.strip() for domain in domains if domain.strip()]
        if clean_domains:
            domain_filter = " " + " OR ".join(f"site:{domain}" for domain in clean_domains)
    search_query = query.strip() + domain_filter
    if not search_query.strip():
        raise WebToolError("query is required")
    encoded = urllib.parse.quote(search_query)
    urls = [
        "https://duckduckgo.com/html/?q=" + encoded,
        "https://lite.duckduckgo.com/lite/?q=" + encoded,
    ]
    status = 0
    raw = ""
    url = urls[0]
    results: list[dict[str, str]] = []
    for candidate in urls:
        url = candidate
        _response_url, status, _content_type, raw = _fetch_text(candidate, config.web_timeout_seconds)
        results = parse_duckduckgo_results(raw)
        if results:
            break
    results = results[:clean_limit]
    return {
        "query": query,
        "provider": "duckduckgo-html",
        "status": status,
        "url": url,
        "results": results,
        "sources": [result["url"] for result in results],
    }


def html_to_text(value: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", value, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:h1|h2|h3|p|li|tr|div|section|article)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_title(value: str) -> str:
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", value, flags=re.IGNORECASE)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()


def parse_duckduckgo_results(value: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    pattern = re.compile(r"<a\s+([^>]*\bclass=[\"'][^\"']*result__(?:a|link)[^\"']*[\"'][^>]*)>([\s\S]*?)</a>", re.IGNORECASE)
    for match in pattern.finditer(value):
        attrs, raw_title = match.groups()
        raw_url = _extract_href(attrs)
        if not raw_url:
            continue
        tail = value[match.start() : match.start() + 1500]
        snippet_match = re.search(r'class=["\']result__(?:snippet|body)["\'][^>]*>([\s\S]*?)</(?:a|div|td)>', tail, flags=re.IGNORECASE)
        url = _decode_duckduckgo_url(html.unescape(raw_url))
        if not url.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "title": html_to_text(raw_title),
                "url": url,
                "snippet": html_to_text(snippet_match.group(1)) if snippet_match else "",
            }
        )
    if results:
        return results

    lite_pattern = re.compile(r"<a\s+([^>]*\bclass=[\"'][^\"']*result-link[^\"']*[\"'][^>]*)>([\s\S]*?)</a>", re.IGNORECASE)
    for match in lite_pattern.finditer(value):
        attrs, raw_title = match.groups()
        raw_url = _extract_href(attrs)
        if not raw_url:
            continue
        tail = value[match.end() : match.end() + 1500]
        snippet_match = re.search(r'class=["\']result-snippet["\'][^>]*>([\s\S]*?)</td>', tail, flags=re.IGNORECASE)
        url = _decode_duckduckgo_url(html.unescape(raw_url))
        if not url.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "title": html_to_text(raw_title),
                "url": url,
                "snippet": html_to_text(snippet_match.group(1)) if snippet_match else "",
            }
        )
    return results


def _extract_href(attrs: str) -> str:
    match = re.search(r"href=[\"']([^\"']+)[\"']", attrs, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _fetch_text(url: str, timeout_seconds: int) -> tuple[str, int, str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BatchAgent/0.1 (+https://local.batchagent)",
            "Accept": "text/html,text/plain,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=max(1, timeout_seconds)) as response:
        raw = response.read()
        content_type = response.headers.get("content-type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
        return response.url, response.status, content_type, text


def _normalize_http_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise WebToolError("only http and https URLs are supported")
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return urllib.parse.urlunparse(parsed)


def _decode_duckduckgo_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    params = urllib.parse.parse_qs(parsed.query)
    uddg = params.get("uddg")
    if uddg and uddg[0]:
        return urllib.parse.unquote(uddg[0])
    return value


def _bounded_limit(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))
