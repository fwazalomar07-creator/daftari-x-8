"""بحث ويب (جوجل ثم DuckDuckGo) بلا مكتبات خارجية — للمساعد عند الحاجة لأسعار أو مواصفات."""
from __future__ import annotations

import base64
import html as html_lib
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
TIMEOUT = 8.0               # لكل محرك؛ مع أربعة محركات احتياطية لا ينتظر المستخدم طويلاً

Fetch = Callable[[str, bytes | None, dict[str, str], float], tuple[int, str]]


class SearchError(Exception):
    pass


def default_fetch(url: str, body: bytes | None, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        return e.code, raw.decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SearchError(f"تعذّر الاتصال بمحرك البحث: {e}") from e


def _headers() -> dict[str, str]:
    return {"User-Agent": UA, "Accept-Language": "ar,en;q=0.8", "Accept": "text/html,application/xhtml+xml"}


def _clean(text: str) -> str:
    text = html_lib.unescape(re.sub(r"\s+", " ", text or "")).strip()
    return text[:400]


def _unwrap_google_href(href: str) -> str:
    if href.startswith("/url?"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        return (qs.get("q") or qs.get("url") or [href])[0]
    return href


class _GoogleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_a = False
        self._href = ""
        self._title: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a":
            href = d.get("href") or ""
            if href.startswith("/url?") or href.startswith("http"):
                self._in_a, self._href, self._title = True, href, []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            title = _clean("".join(self._title))
            url = _unwrap_google_href(self._href)
            if title and url.startswith("http") and "google." not in urllib.parse.urlparse(url).netloc:
                if not any(r["url"] == url for r in self.results):
                    self.results.append({"title": title, "url": url, "snippet": ""})
            self._in_a = False

    def handle_data(self, data):
        if self._in_a:
            self._title.append(data)


class _DdgParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_a = False
        self._in_snip = False
        self._href = ""
        self._buf: list[str] = []
        self._snip: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "")
        if tag == "a" and "result__a" in cls:
            self._in_a, self._href, self._buf = True, d.get("href") or "", []
        elif tag in ("a", "div") and "result__snippet" in cls:
            self._in_snip, self._snip = True, []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            title = _clean("".join(self._buf))
            url = self._unwrap_ddg(self._href)
            if title and url.startswith("http"):
                self.results.append({"title": title, "url": url, "snippet": ""})
            self._in_a = False
        if self._in_snip and tag in ("a", "div"):
            if self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = _clean("".join(self._snip))
            self._in_snip = False

    def handle_data(self, data):
        if self._in_a:
            self._buf.append(data)
        if self._in_snip:
            self._snip.append(data)

    @staticmethod
    def _unwrap_ddg(href: str) -> str:
        if "uddg=" in href:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            return urllib.parse.unquote((qs.get("uddg") or [href])[0])
        return href


def _unwrap_bing_href(href: str) -> str:
    """روابط Bing قد تكون وسيطاً: https://www.bing.com/ck/a?…&u=a1<base64url-of-real-url>"""
    if "bing.com/ck/a" in href:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        u = (qs.get("u") or [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return href
    return href


class _BingParser(HTMLParser):
    """نتائج Bing: <li class="b_algo"><h2><a href>العنوان</a></h2> … <p>المقتطف</p></li>"""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_li = False
        self._in_h2 = False
        self._in_a = False
        self._in_p = False
        self._href = ""
        self._title: list[str] = []
        self._snip: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "li" and "b_algo" in (d.get("class") or ""):
            self._in_li, self._href, self._title, self._snip = True, "", [], []
        elif self._in_li and tag == "h2":
            self._in_h2 = True
        elif self._in_li and self._in_h2 and tag == "a" and not self._href:
            self._href, self._in_a = d.get("href") or "", True
        elif self._in_li and tag == "p" and not self._snip:
            self._in_p = True

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_a = False
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "p" and self._in_p:
            self._in_p = False
        elif tag == "li" and self._in_li:
            url, title = _unwrap_bing_href(self._href), _clean("".join(self._title))
            if title and url.startswith("http") and not any(r["url"] == url for r in self.results):
                self.results.append({"title": title, "url": url, "snippet": _clean("".join(self._snip))})
            self._in_li = False

    def handle_data(self, data):
        if self._in_a:
            self._title.append(data)
        elif self._in_p:
            self._snip.append(data)


class _MojeekParser(HTMLParser):
    """Mojeek: <a class="title" href=…>العنوان</a> … <p class="s">المقتطف</p> (لا يحجب الطلبات الآلية عادةً)."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_a = False
        self._in_snip = False
        self._href = ""
        self._buf: list[str] = []
        self._snip: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class") or ""
        if tag == "a" and "title" in cls.split():
            self._in_a, self._href, self._buf = True, d.get("href") or "", []
        elif tag == "p" and "s" in cls.split():
            self._in_snip, self._snip = True, []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            title = _clean("".join(self._buf))
            if title and self._href.startswith("http") and not any(r["url"] == self._href for r in self.results):
                self.results.append({"title": title, "url": self._href, "snippet": ""})
            self._in_a = False
        elif tag == "p" and self._in_snip:
            if self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = _clean("".join(self._snip))
            self._in_snip = False

    def handle_data(self, data):
        if self._in_a:
            self._buf.append(data)
        elif self._in_snip:
            self._snip.append(data)


def _search_bing(query: str, limit: int, fetch: Fetch) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en", "count": max(8, limit)})
    status, body = fetch(url, None, _headers(), TIMEOUT)
    if status != 200:
        raise SearchError(f"Bing ردّ بحالة {status}")
    p = _BingParser()
    p.feed(body)
    return p.results[:limit]


def _search_mojeek(query: str, limit: int, fetch: Fetch) -> list[dict[str, str]]:
    url = "https://www.mojeek.com/search?" + urllib.parse.urlencode({"q": query})
    status, body = fetch(url, None, _headers(), TIMEOUT)
    if status != 200:
        raise SearchError(f"Mojeek ردّ بحالة {status}")
    p = _MojeekParser()
    p.feed(body)
    return p.results[:limit]


def _search_google(query: str, limit: int, fetch: Fetch) -> list[dict[str, str]]:
    url = "https://www.google.com/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "ar", "num": max(5, limit), "pws": "0"})
    status, body = fetch(url, None, _headers(), TIMEOUT)
    if status != 200:
        raise SearchError(f"جوجل ردّ بحالة {status}")
    p = _GoogleParser()
    p.feed(body)
    return p.results[:limit]


def _search_ddg(query: str, limit: int, fetch: Fetch) -> list[dict[str, str]]:
    url = "https://html.duckduckgo.com/html/"
    payload = urllib.parse.urlencode({"q": query}).encode()
    headers = {**_headers(), "Content-Type": "application/x-www-form-urlencoded"}
    status, body = fetch(url, payload, headers, TIMEOUT)
    if status != 200:
        raise SearchError(f"DuckDuckGo ردّ بحالة {status}")
    p = _DdgParser()
    p.feed(body)
    return p.results[:limit]


def search_web(query: str, max_results: int = 6, fetch: Fetch | None = None) -> dict[str, Any]:
    """يبحث بالتتابع في DuckDuckGo ثم Bing ثم Mojeek ثم جوجل حتى تظهر نتائج. يرفع SearchError إن فشلت كلها."""
    q = (query or "").strip()
    if not q:
        raise SearchError("أدخل عبارة بحث.")
    n = max(1, min(int(max_results or 6), 10))
    do = fetch or default_fetch
    errors: list[str] = []
    # الترتيب: المحركات التي تتسامح مع الطلبات الآلية أولاً؛ جوجل آخراً لأنه غالباً يُرجع صفحة «فعّل JavaScript»
    for engine, fn in (("duckduckgo", _search_ddg), ("bing", _search_bing), ("mojeek", _search_mojeek), ("google", _search_google)):
        try:
            hits = fn(q, n, do)
        except SearchError as e:
            errors.append(f"{engine}: {e}")
            continue
        if hits:
            return {"engine": engine, "query": q, "results": hits}
        errors.append(f"{engine}: لا نتائج")
    raise SearchError("تعذّر البحث: " + "؛ ".join(errors))
