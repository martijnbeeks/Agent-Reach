# -*- coding: utf-8 -*-
"""Read Reddit's visible old.reddit.com pages into unified items.

Reddit answers the `.json` endpoints OpenCLI uses with a 403 "blocked by
network security" page on some networks, while the pages a logged-in person
sees still load. old.reddit.com is the page source: it is rendered on the
server, its post and comment markup has carried stable `data-*` attributes for
years, and it needs no scrolling or script execution to show 25+ results.
Parsing is pure (HTML text in, items out) so it is tested on saved pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterator, List, NoReturn, Optional, Tuple, Union
from urllib.parse import urlencode, urlsplit

from agent_reach.runtime.result import (
    NEED_LOGIN,
    NOT_FOUND,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
    unix_to_iso,
)

OLD = "https://old.reddit.com"
WWW = "https://www.reddit.com"
SEARCH_SORTS = ("relevance", "hot", "top", "new", "comments")
LISTING_SORTS = ("hot", "new", "top", "rising", "controversial")
COMMENT_LIMIT = 20

_BLOCK_RE = re.compile(r"blocked by network security", re.I)
_RATE_RE = re.compile(r"whoa there,? pardner|too many requests", re.I)
_EMPTY_RE = re.compile(r"there doesn.t seem to be anything here|no results found|page not found|"
                       r"this community has been banned|is a private community", re.I)
_POST_ID_RE = re.compile(r"[a-z0-9]{1,12}")
_POST_PATH_RE = re.compile(r"(?:/r/[A-Za-z0-9_]+)?/comments/[A-Za-z0-9]+(?:/[^/?#]*)?/?")
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                   "source", "track", "wbr"})
_BLOCK_TAGS = frozenset({"p", "li", "br", "div", "pre", "blockquote", "h1", "h2", "h3", "h4", "tr"})


def is_block_page(text: str) -> bool:
    """Reddit's "You've been blocked by network security" page, as HTML or reader text."""
    return bool(_BLOCK_RE.search(text or ""))


# ── a minimal DOM over html.parser ──

@dataclass(eq=False)
class _Node:
    tag: str
    attrs: dict
    children: List[Union["_Node", str]] = field(default_factory=list)

    def classes(self) -> set:
        return set((self.attrs.get("class") or "").split())

    def walk(self) -> Iterator["_Node"]:
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.walk()

    def find_all(self, pred: Callable[["_Node"], bool]) -> List["_Node"]:
        return [n for n in self.walk() if pred(n)]

    def find(self, pred: Callable[["_Node"], bool]) -> Optional["_Node"]:
        return next((n for n in self.walk() if pred(n)), None)

    def text(self) -> str:
        parts: List[str] = []

        def collect(node: "_Node") -> None:
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    if child.tag in _BLOCK_TAGS:
                        parts.append("\n")
                    collect(child)
        collect(self)
        lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(parts).split("\n"))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if self._stack[-1].tag not in {"script", "style"}:
            self._stack[-1].children.append(data)


def _parse(html: str) -> _Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


def _has_class(name: str) -> Callable[[_Node], bool]:
    return lambda n: name in n.classes()


def _is_thing(node: _Node, kind: str) -> bool:
    return "thing" in node.classes() and node.attrs.get("data-type") == kind


def _child(node: _Node, cls: str) -> Optional[_Node]:
    return next((c for c in node.children if isinstance(c, _Node) and cls in c.classes()), None)


def _www(path_or_url: str) -> str:
    if path_or_url.startswith("/"):
        return WWW + path_or_url
    parts = urlsplit(path_or_url)
    if parts.hostname in {"old.reddit.com", "reddit.com"}:
        return WWW + parts.path + (f"?{parts.query}" if parts.query else "")
    return path_or_url


def _iso(value: Optional[str]) -> Optional[str]:
    return value.replace("+00:00", "Z") if value else None


def _int(value: Optional[str]) -> Optional[int]:
    digits = re.sub(r"[^\d-]", "", value or "")
    return int(digits) if re.fullmatch(r"-?\d+", digits) else None


# ── pages ──

def _explain_empty(root: _Node, html: str, what: str) -> NoReturn:
    """Raise why a page has no results: block, rate limit, login wall, nothing there, or unknown layout."""
    if is_block_page(html):
        raise ReachError(UPSTREAM_BROKEN,
                         f"Reddit answered the {what} page with its \"blocked by network security\" page",
                         hint="Reddit applies this block per network; retry later. Logging in again does not help.")
    text = root.text()
    if _RATE_RE.search(text):
        raise ReachError(RATE_LIMITED, f"Reddit rate limited the {what} page")
    if _EMPTY_RE.search(text):
        raise ReachError(NOT_FOUND, f"Reddit shows nothing for this {what}")
    body = root.find(lambda n: n.tag == "body")
    login_form = root.find(lambda n: n.tag == "form" and "login" in n.attrs.get("action", "") + n.attrs.get("id", ""))
    if login_form and not (body and "loggedin" in body.classes()):
        raise ReachError(NEED_LOGIN, f"old.reddit.com asked for a login on the {what} page")
    raise ReachError(UPSTREAM_BROKEN, f"old.reddit.com {what} page layout was not recognized")


def _link_item(thing: _Node) -> dict:
    a = thing.attrs
    entry = _child(thing, "entry") or thing
    title = entry.find(lambda n: n.tag == "a" and "title" in n.classes())
    body = entry.find(_has_class("md"))
    permalink = a.get("data-permalink", "")
    external = a.get("data-url", "")
    stamp = _int(a.get("data-timestamp"))
    return make_item(
        title=title.text() if title else None,
        url=_www(permalink) if permalink else None,
        author=a.get("data-author"),
        published_at=unix_to_iso(stamp // 1000) if stamp else None,
        text=body.text() if body else (external if external.startswith("http") else None),
        raw={"id": a.get("data-fullname", "").removeprefix("t3_"), "subreddit": a.get("data-subreddit"),
             "score": _int(a.get("data-score")), "comments": _int(a.get("data-comments-count")),
             "permalink": permalink, "link_url": _www(external) if external else None,
             "stickied": "stickied" in thing.classes(), "source": "old.reddit.com"},
    )


def parse_listing(html: str, limit: int) -> List[dict]:
    """Posts of a subreddit listing page, promoted posts left out."""
    root = _parse(html)
    things = [t for t in root.walk() if _is_thing(t, "link") and t.attrs.get("data-promoted") != "true"]
    if not things:
        _explain_empty(root, html, "subreddit")
    return [_link_item(t) for t in things[:limit]]


def parse_search(html: str, limit: int) -> List[dict]:
    """Post results of an old.reddit.com search page."""
    root = _parse(html)
    results = root.find_all(_has_class("search-result-link"))
    if not results:
        _explain_empty(root, html, "search")
    items = []
    for r in results[:limit]:
        title = r.find(_has_class("search-title"))
        author = r.find(lambda n: n.tag == "a" and "author" in n.classes())
        when = r.find(lambda n: n.tag == "time")
        sub = r.find(_has_class("search-subreddit-link"))
        score = r.find(_has_class("search-score"))
        comments = r.find(_has_class("search-comments"))
        body = r.find(_has_class("md"))
        href = title.attrs.get("href", "") if title else ""
        items.append(make_item(
            title=title.text() if title else None,
            url=_www(href) if href else None,
            author=author.text() if author else None,
            published_at=_iso(when.attrs.get("datetime")) if when else None,
            text=body.text() if body else None,
            raw={"id": r.attrs.get("data-fullname", "").removeprefix("t3_"),
                 "subreddit": sub.text().removeprefix("r/") if sub else None,
                 "score": _int(score.text()) if score else None,
                 "comments": _int(comments.text()) if comments else None,
                 "source": "old.reddit.com"},
        ))
    return items


def _comment_things(table: _Node, depth: int = 0) -> Iterator[Tuple[_Node, int]]:
    """Comments of a `div.sitetable` in page order, each with its reply depth."""
    for thing in table.children:
        if isinstance(thing, _Node) and _is_thing(thing, "comment"):
            yield thing, depth
            replies = _child(thing, "child")
            nested = replies and _child(replies, "sitetable")
            if nested:
                yield from _comment_things(nested, depth + 1)


def parse_post(html: str, limit: int = COMMENT_LIMIT) -> Tuple[dict, List[dict]]:
    """The post and its first `limit` visible comments, replies included, in page order."""
    root = _parse(html)
    thing = root.find(lambda n: _is_thing(n, "link"))
    if thing is None:
        _explain_empty(root, html, "post")
    area = root.find(_has_class("commentarea"))
    table = area.find(_has_class("sitetable")) if area else None
    comments = []
    for c, depth in _comment_things(table) if table else ():
        entry = _child(c, "entry")
        body = entry.find(_has_class("md")) if entry else None
        if entry is None or body is None or not c.attrs.get("data-author"):
            continue  # deleted and removed comments keep a stub without author or text
        when = entry.find(lambda n: n.tag == "time")
        score = entry.find(lambda n: n.tag == "span" and {"score", "unvoted"} <= n.classes())
        comments.append(make_item(
            url=_www(c.attrs["data-permalink"]) if c.attrs.get("data-permalink") else None,
            author=c.attrs["data-author"],
            published_at=_iso(when.attrs.get("datetime")) if when else None,
            text=body.text(),
            raw={"id": c.attrs.get("data-fullname", "").removeprefix("t1_"), "depth": depth,
                 "score": _int(score.attrs.get("title")) if score else None, "source": "old.reddit.com"},
        ))
        if len(comments) >= limit:
            break
    return _link_item(thing), comments


# ── URLs ──

def _page_size(limit: int) -> int:
    """Ask for at least a full page: Reddit leaves out filtered and promoted posts after counting."""
    return min(max(limit, 25), 100)


def search_url(query: str, sort: str, limit: int) -> str:
    return f"{OLD}/search?" + urlencode({"q": query, "type": "link", "sort": sort, "t": "all",
                                         "limit": _page_size(limit)})


def listing_url(name: str, sort: str, limit: int) -> str:
    return f"{OLD}/r/{name}/{sort}/?" + urlencode({"limit": _page_size(limit)})


def post_url(target: str) -> str:
    """old.reddit.com comments URL for a post link (www, old, redd.it) or a bare post id.

    Other reddit links (share links, galleries) are valid for OpenCLI, so this backend
    reports them as unsupported here, not as bad input that would stop the fallback.
    """
    t = target.strip()
    if _POST_ID_RE.fullmatch(t.lower()):
        return _comments_url(f"/comments/{t.lower()}/")
    parts = urlsplit(t if "://" in t else "https://" + t)
    host = (parts.hostname or "").lower()
    short_id = parts.path.strip("/").lower()
    if host == "redd.it" and _POST_ID_RE.fullmatch(short_id):
        return _comments_url(f"/comments/{short_id}/")
    match = _POST_PATH_RE.match(parts.path)
    if (host == "reddit.com" or host.endswith(".reddit.com")) and match:
        return _comments_url(match.group(0).rstrip("/") + "/")
    raise ReachError(UPSTREAM_BROKEN, "old.reddit.com reads only /comments/<id> post links and post ids")


def _comments_url(path: str) -> str:
    return f"{OLD}{path}?" + urlencode({"sort": "top", "limit": COMMENT_LIMIT})
