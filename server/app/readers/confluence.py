"""Fetches a Confluence page (Cloud or Data Center) and reads its storage-format body into blocks."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from ..config import settings
from ..model import Block, Document, Run


class ConfluenceError(Exception):
    pass


def _client() -> httpx.Client:
    c = settings.confluence
    if not c.base_url:
        raise ConfluenceError("Confluence is not set up on the server (CONFLUENCE_BASE_URL).")
    headers = {"Accept": "application/json"}
    auth = None
    if c.pat:
        headers["Authorization"] = f"Bearer {c.pat}"
    elif c.email and c.api_token:
        auth = (c.email, c.api_token)
    else:
        raise ConfluenceError("Confluence credentials are missing (CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN, or CONFLUENCE_PAT).")
    return httpx.Client(base_url=c.base_url.rstrip("/"), headers=headers, auth=auth, timeout=30)


def resolve_page_id(client: httpx.Client, ref: str) -> str:
    """Accepts a page ID or any common Confluence page URL."""
    ref = ref.strip()
    if ref.isdigit():
        return ref
    u = urlparse(ref)
    m = re.search(r"/pages/(\d+)", u.path)
    if m:
        return m.group(1)
    q = parse_qs(u.query)
    if "pageId" in q:
        return q["pageId"][0]
    m = re.search(r"/display/([^/]+)/([^/?#]+)", u.path)
    if m:
        space, title = m.group(1), unquote_plus(m.group(2))
        r = client.get("/rest/api/content", params={"spaceKey": space, "title": title, "type": "page"})
        _check(r, ref)
        results = r.json().get("results", [])
        if results:
            return results[0]["id"]
        raise ConfluenceError(f"No page titled '{title}' in space {space}.")
    raise ConfluenceError(f"Not a recognisable Confluence page link: {ref}")


def _check(r: httpx.Response, ref: str) -> None:
    if r.status_code in (401, 403):
        raise ConfluenceError(f"Confluence refused access to {ref} ({r.status_code}). Check the service account can view this page.")
    if r.status_code == 404:
        raise ConfluenceError(f"Confluence page not found: {ref}")
    if r.status_code >= 400:
        raise ConfluenceError(f"Confluence error {r.status_code} for {ref}: {r.text[:200]}")


def fetch_page(ref: str) -> tuple[str, str]:
    """Returns (title, storage-format HTML)."""
    if settings.demo:
        from ..demo import demo_confluence_page

        return demo_confluence_page(ref)
    with _client() as client:
        page_id = resolve_page_id(client, ref)
        r = client.get(f"/rest/api/content/{page_id}", params={"expand": "body.storage"})
        _check(r, ref)
        j = r.json()
        return j.get("title", f"Page {page_id}"), j.get("body", {}).get("storage", {}).get("value", "")


CONTAINER_MACROS = {"info", "note", "warning", "tip", "panel", "expand", "section", "column", "details", "excerpt"}


def _runs(el: Tag | NavigableString, bold=False, italic=False, underline=False) -> list[Run]:
    if isinstance(el, NavigableString):
        t = str(el)
        return [Run(t, bold, italic, underline)] if t else []
    name = el.name or ""
    b = bold or name in ("strong", "b")
    i = italic or name in ("em", "i")
    u = underline or name == "u"
    if name == "br":
        return [Run("\n")]
    out: list[Run] = []
    for c in el.children:
        out.extend(_runs(c, b, i, u))
    return out


def _merge(runs: list[Run]) -> list[Run]:
    out: list[Run] = []
    for r in runs:
        r.text = re.sub(r"[ \t\r\n]+", " ", r.text) if r.text != "\n" else r.text
        if out and (out[-1].bold, out[-1].italic, out[-1].underline) == (r.bold, r.italic, r.underline):
            out[-1].text += r.text
        else:
            out.append(r)
    if out:
        out[0].text = out[0].text.lstrip()
        out[-1].text = out[-1].text.rstrip()
    return [r for r in out if r.text]


def _walk(el: Tag, blocks: list[Block], depth: int = 0) -> None:
    for c in el.children:
        if isinstance(c, NavigableString):
            if str(c).strip():
                blocks.append(Block("para", runs=[Run(str(c).strip())]))
            continue
        name = (c.name or "").lower()
        if re.fullmatch(r"h[1-6]", name):
            t = c.get_text(" ", strip=True)
            if t:
                blocks.append(Block("heading", runs=[Run(t)], level=int(name[1])))
        elif name == "p":
            runs = _merge(_runs(c))
            if runs:
                blocks.append(Block("para", runs=runs))
        elif name in ("ul", "ol"):
            for li in c.find_all("li", recursive=False):
                inline = [x for x in li.children if not (isinstance(x, Tag) and x.name in ("ul", "ol"))]
                runs = _merge([r for x in inline for r in _runs(x)])
                if runs:
                    blocks.append(Block("list", runs=runs, level=depth, ordered=name == "ol"))
                for sub in li.find_all(["ul", "ol"], recursive=False):
                    wrapper = BeautifulSoup("", "html.parser").new_tag("div")
                    wrapper.append(sub.__copy__())
                    _walk(wrapper, blocks, depth + 1)
        elif name == "table":
            rows = []
            for tr in c.find_all("tr"):
                rows.append([_merge(_runs(td)) for td in tr.find_all(["td", "th"], recursive=False)])
            if rows:
                blocks.append(Block("table", rows=rows))
        elif name == "ac:structured-macro":
            mname = (c.get("ac:name") or "").lower()
            body = c.find("ac:rich-text-body")
            if mname in CONTAINER_MACROS and body is not None:
                _walk(body, blocks, depth)
            elif mname in ("code", "noformat"):
                pt = c.find("ac:plain-text-body")
                if pt and pt.get_text(strip=True):
                    blocks.append(Block("para", runs=[Run(pt.get_text())]))
        elif name == "ac:task-list":
            for task in c.find_all("ac:task-body"):
                t = task.get_text(" ", strip=True)
                if t:
                    blocks.append(Block("list", runs=[Run(t)], level=depth))
        elif name in ("ac:layout", "ac:layout-section", "ac:layout-cell", "div", "section", "blockquote", "ac:rich-text-body"):
            _walk(c, blocks, depth)
        elif name in ("ac:image", "ac:link", "ri:attachment"):
            continue
        else:
            t = c.get_text(" ", strip=True)
            if t:
                blocks.append(Block("para", runs=[Run(t)]))


def read_confluence(ref: str) -> Document:
    title, storage = fetch_page(ref)
    soup = BeautifulSoup(f"<div>{storage}</div>", "html.parser")
    blocks: list[Block] = []
    _walk(soup.div, blocks)
    return Document(title=title, blocks=blocks, kind="confluence")
