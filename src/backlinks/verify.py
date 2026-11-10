"""Independent backlink verification. It ignores what a submission reported and looks at the live published page:
is it reachable, does it still carry the content, and does it contain a real HTML link to our site? A link counts
only if it is an ordinary <a href> that resolves to one of the expected addresses on our own domain."""
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; BacklinkVerifier/1.0; +https://hypnotreatment.uk)"
MAX_BYTES = 2_000_000
NOT_FOLLOWED_RELS = {"nofollow", "sponsored", "ugc"}


@dataclass
class FetchResult:
    status: int | None          # None when the request itself failed (DNS, timeout, refused)
    final_url: str
    text: str
    error: str | None = None
    headers: dict | None = None   # lower-case response headers (only X-Robots-Tag is used)


def http_fetch(url: str, timeout: int = 20) -> FetchResult:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return FetchResult(resp.status, resp.geturl(), resp.read(MAX_BYTES).decode("utf-8", "replace"),
                               headers={k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as e:
        return FetchResult(e.code, url, "", f"HTTP {e.code}")
    except Exception as e:  # DNS failure, timeout, refused, TLS...
        return FetchResult(None, url, "", f"{type(e).__name__}: {e}")


def bare_host(url_or_host: str) -> str:
    host = urlparse(url_or_host).netloc or urlparse("//" + url_or_host).netloc
    return host.lower().removeprefix("www.").split(":")[0]


def _path(url: str) -> str:
    return (urlparse(url).path or "/").rstrip("/") or "/"


def _is_hidden(tag) -> bool:
    """A link a visitor cannot see (display:none, hidden, noscript) is not a real backlink."""
    for el in [tag, *tag.parents]:
        if el.name in ("noscript", "template") or el.has_attr("hidden"):
            return True
        style = (el.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
    return False


def extract_links(html: str, page_url: str) -> list[dict]:
    """Every ordinary, visible HTML hyperlink on the page, with its resolved address, anchor text and rel attributes."""
    soup = BeautifulSoup(html, "html.parser")
    base = soup.find("base", href=True)
    base_url = urljoin(page_url, base["href"]) if base else page_url
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        rel = sorted({t.lower() for t in (a.get("rel") or [])})
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        kind = "text_link"
        if not text:
            img = a.find("img")
            text = f"[image: {img.get('alt', '').strip()}]" if img else ""
            kind = "image_link" if img else "empty_link"
        links.append({"href": href, "resolved": urljoin(base_url, href), "anchor": text, "rel": rel, "kind": kind,
                      "follow": not (set(rel) & NOT_FOLLOWED_RELS), "hidden": _is_hidden(a)})
    return links


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2}


def content_present(html: str, expected_text: str) -> bool:
    """The published page still carries our content: most of the title's words appear in its visible text."""
    want = _words(expected_text)
    if not want:
        return True
    have = _words(visible_text(html))
    return len(want & have) / len(want) >= 0.7


def page_indexing(html: str, headers: dict | None = None) -> dict:
    """What the page tells search engines: a link on a page marked noindex is not indexed, and one on a page marked nofollow is
    not followed, however plain the link itself is. Reads <meta name="robots|googlebot"> and the X-Robots-Tag header.
    {"indexable": bool, "page_nofollow": bool, "directives": [...]}"""
    directives: list[str] = []
    for m in BeautifulSoup(html or "", "html.parser").find_all("meta"):
        if (m.get("name") or "").strip().lower() in ("robots", "googlebot"):
            directives += [d.strip().lower() for d in (m.get("content") or "").split(",") if d.strip()]
    header = (headers or {}).get("x-robots-tag")
    if header:
        directives += [d.strip().lower() for d in re.sub(r"^\s*(googlebot|bingbot)\s*:", "", header, flags=re.I).split(",") if d.strip()]
    noindex = "noindex" in directives or "none" in directives
    nofollow = "nofollow" in directives or "none" in directives
    return {"indexable": not noindex, "page_nofollow": nofollow, "directives": sorted(set(directives) & {"noindex", "nofollow", "none", "noarchive"})}


def counts_for_seo(link_follow: bool, indexing: dict | None) -> bool | None:
    """True only when search engines may index the page AND follow its links; None when the page was never checked."""
    if not indexing:
        return None
    return bool(link_follow and indexing["indexable"] and not indexing["page_nofollow"])


def verify_backlink(page_url: str, expected_urls: list[str], canonical_hosts: list[str], expected_text: str | None = None,
                    fetch=http_fetch, now_iso: str = "") -> dict:
    """Returns the evidence record. outcome is one of:
      verified         an ordinary, visible link to one of expected_urls is on the live page
      link_absent      the page is up but carries no such link (or the content is gone)
      page_gone        the page returns 404/410 (permanent)
      page_unreachable the page could not be loaded now (timeout, 5xx, blocked): may be transient
    """
    hosts = {bare_host(h) for h in canonical_hosts}
    expected_paths = {_path(u) for u in expected_urls}
    page = fetch(page_url)
    result = {"checked_at": now_iso, "page_url": page_url, "page_status": page.status, "page_final_url": page.final_url,
              "error": page.error, "content_present": None, "matched_link": None, "site_links_found": [],
              "plain_text_mention": False, "destination": None, "outcome": None, "reason": None}

    if page.status in (404, 410):
        result.update(outcome="page_gone", reason=f"the page returns HTTP {page.status}")
        return result
    if page.status is None or page.status >= 400 or not page.text:
        result.update(outcome="page_unreachable", reason=page.error or f"HTTP {page.status}, empty body")
        return result

    result["indexing"] = page_indexing(page.text, page.headers)
    if expected_text is not None:
        result["content_present"] = content_present(page.text, expected_text)

    site_links = [l for l in extract_links(page.text, page.final_url) if bare_host(l["resolved"]) in hosts]
    result["site_links_found"] = [{k: l[k] for k in ("resolved", "anchor", "rel", "follow", "kind", "hidden")} for l in site_links]
    matches = [l for l in site_links if not l["hidden"] and _path(l["resolved"]) in expected_paths]
    result["plain_text_mention"] = any(h in visible_text(page.text).lower() for h in hosts) and not site_links

    if result["content_present"] is False:
        result.update(outcome="link_absent", reason="the page no longer carries the published content")
        return result
    if not matches:
        reason = ("it links to our site, but not to an expected page" if site_links else
                  "the site is only mentioned as plain text, not linked" if result["plain_text_mention"] else
                  "no link to our site is on the page")
        result.update(outcome="link_absent", reason=reason)
        return result

    best = next((l for l in matches if l["follow"]), matches[0])
    result["matched_link"] = {k: best[k] for k in ("href", "resolved", "anchor", "rel", "follow", "kind")}
    dest = fetch(best["resolved"])
    final_host = bare_host(dest.final_url) if dest.final_url else ""
    result["destination"] = {"status": dest.status, "final_url": dest.final_url, "redirected": dest.final_url != best["resolved"],
                             "final_host_is_ours": final_host in hosts}
    result.update(outcome="verified", reason="an ordinary HTML link to the expected page is live")
    return result
