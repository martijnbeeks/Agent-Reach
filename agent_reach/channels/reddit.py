# -*- coding: utf-8 -*-
"""Reddit browser actions supplement public web search and reading.

Public posts and some comments can be available through an agent's existing
web tools without Reddit login. A blocked anonymous JSON endpoint does not
establish a site-wide login requirement. These actions use managed browser
sessions; request authentication only when the selected path requires it.

Each action tries OpenCLI (Reddit's JSON endpoints) first, then reads the
visible old.reddit.com page in the same browser (`_reddit_pages`), because
Reddit blocks the JSON endpoints on some networks while the pages still load.
"""

import os
import re
import threading
import time

from agent_reach.backends.opencli import opencli_browser, opencli_call, opencli_items
from agent_reach.runtime.result import BAD_INPUT, TIMEOUT, UPSTREAM_BROKEN, ReachError

from . import _reddit_pages as pages
from .base import Action, Channel, Param

_REDDIT_LOGIN_HINT = (
    "Ask the user to log in to reddit.com in the Agent Reach dedicated browser. Never log in on their behalf."
)
_SUBREDDIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{1,20}")
_CLOSE_SECONDS = 10.0


def _reddit(args, timeout: float) -> list:
    items = opencli_items(opencli_call("reddit", list(args), timeout=timeout, login_hint=_REDDIT_LOGIN_HINT))
    if not items:
        raise ReachError(UPSTREAM_BROKEN, "OpenCLI finished but returned no items")
    return items


def _page_html(url: str, timeout: float) -> str:
    """Open `url` in a tab of the Agent Reach browser and return the rendered HTML."""
    deadline = time.monotonic() + timeout
    session = f"reach-reddit-{os.getpid()}-{threading.get_ident()}"

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReachError(TIMEOUT, "Reddit page did not load in time")
        return remaining

    try:
        opencli_browser("reddit", ["--session", session, "open", url], timeout=left())
        data = opencli_browser("reddit", ["--session", session, "eval", "document.documentElement.outerHTML"],
                               timeout=left())
    finally:
        try:
            opencli_browser("reddit", ["--session", session, "close"], timeout=_CLOSE_SECONDS)
        except ReachError:
            pass  # the page result stands; an unreleased tab lease ends with OpenCLI's session
    html = data.get("data") if isinstance(data, dict) else None
    if not isinstance(html, str) or not html:
        raise ReachError(UPSTREAM_BROKEN, "The Reddit page returned no HTML")
    return html


def _with_comments(post: dict, comments: list) -> dict:
    if comments:
        lines = [post["text"] or ""] + [f"\n@{c['author'] or ''}: {c['text'] or ''}" for c in comments]
        post["text"] = "\n".join(lines).strip()
        post["raw"] = {"post": post["raw"], "comments": [c["raw"] for c in comments]}
    return post


class RedditChannel(Channel):
    name = "reddit"
    description = "Reddit 帖子和评论"

    level = "optional"
    login_url = "https://www.reddit.com/login/"
    tools = ("opencli",)
    prereqs = ("node", "chrome")

    def setup_problems(self):
        from agent_reach.runtime.browser_binding import setup_problems

        return setup_problems(self.name)

    actions = (
        Action(
            "search",
            "Search public posts first; --backend opencli or old-reddit uses site search",
            (
                Param("query", "what to search for"),
                Param("limit", "max results", required=False, type=int, default=5),
                Param("sort", "relevance, hot, top, new or comments", required=False, default="relevance"),
            ),
            (("opencli", "_opencli_search"), ("old-reddit", "_page_search")),
            live_test={"query": "python", "limit": 2},
            public_web=("reddit.com/r/",),
        ),
        Action(
            "read",
            "Read public page text first; --backend opencli or old-reddit returns the post and top comments",
            (Param("target", "post link or id"),),
            (("opencli", "_opencli_read"), ("old-reddit", "_page_read")),
            public_web=("reddit.com/r/", "redd.it/"),
        ),
        Action(
            "subreddit",
            "Posts in a subreddit",
            (
                Param("name", "subreddit name without r/, e.g. python"),
                Param("limit", "max posts", required=False, type=int, default=5),
                Param("sort", "hot, new, top, rising or controversial", required=False, default="hot"),
            ),
            (("opencli", "_opencli_subreddit"), ("old-reddit", "_page_subreddit")),
            live_test={"name": "python", "limit": 2},
        ),
    )

    def can_handle(self, url: str) -> bool:
        from agent_reach.utils.url import host_matches

        return host_matches(url, "reddit.com", "redd.it")

    def validate_params(self, action, params):
        sorts = {"search": pages.SEARCH_SORTS, "subreddit": pages.LISTING_SORTS}.get(action)
        if sorts and params["sort"] not in sorts:
            raise ReachError(BAD_INPUT, f"sort must be one of: {', '.join(sorts)}")
        if action == "subreddit":
            params["name"] = params["name"].removeprefix("r/")
            if not _SUBREDDIT_RE.fullmatch(params["name"]):
                raise ReachError(BAD_INPUT, "name must be a subreddit name such as python")
        if action == "read" and not (self.can_handle(params["target"]) or params["target"].isalnum()):
            raise ReachError(BAD_INPUT, "target must be a reddit.com post link or post id")

    # ── unified entry backends ──

    def _opencli_search(self, *, query: str, limit: int, sort: str, timeout: float) -> list:
        return _reddit(["search", query, "--limit", str(limit), "--sort", sort], timeout)[:limit]

    def _opencli_read(self, *, target: str, timeout: float) -> list:
        items = _reddit(["read", target, "--limit", "20"], timeout)
        return [_with_comments(items[0], items[1:])]

    def _opencli_subreddit(self, *, name: str, limit: int, sort: str, timeout: float) -> list:
        return _reddit(["subreddit", name, "--limit", str(limit), "--sort", sort], timeout)[:limit]

    def _page_search(self, *, query: str, limit: int, sort: str, timeout: float) -> list:
        return pages.parse_search(_page_html(pages.search_url(query, sort, limit), timeout), limit)

    def _page_read(self, *, target: str, timeout: float) -> list:
        post, comments = pages.parse_post(_page_html(pages.post_url(target), timeout))
        return [_with_comments(post, comments)]

    def _page_subreddit(self, *, name: str, limit: int, sort: str, timeout: float) -> list:
        return pages.parse_listing(_page_html(pages.listing_url(name, sort, limit), timeout), limit)
