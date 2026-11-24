# -*- coding: utf-8 -*-
"""Reddit's old.reddit.com page backend, tested on saved pages (no network)."""

import json
from pathlib import Path

import pytest

from agent_reach.backends.opencli import _opencli_error
from agent_reach.channels import _reddit_pages as pages
from agent_reach.channels import reddit
from agent_reach.channels.reddit import RedditChannel
from agent_reach.channels.web import WebChannel
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import ReachError

FIXTURES = Path(__file__).parent / "fixtures" / "reddit"
# OpenCLI 2.0.3's answer when Reddit blocks /r/<sub>/hot.json (captured 2026-11-22).
SECURITY_BLOCK = json.dumps({"ok": False, "error": {
    "code": "SECURITY_BLOCK",
    "message": 'Reddit blocked /r/ClaudeAI/hot.json?limit=2&raw_json=1 with its "blocked by network security" page (HTTP 403).',
    "exitCode": 1,
    "hint": "Reddit's network-security filter rejected this request. This is not an expired login: if `opencli reddit "
            "whoami` works, logging in again will not help.",
}})
POST_LINK = "https://www.reddit.com/r/ClaudeAI/comments/1y2jxny/what_keeps_longrunning_claude_projects_coherent/"


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_subreddit_listing_page_gives_posts_in_page_order():
    items = pages.parse_listing(_page("subreddit_new.html"), 5)
    assert len(items) == 5
    first = items[0]
    assert first["title"] == "From 40 iPhone photos of my office to a three.js dogfight game"
    assert first["url"] == ("https://www.reddit.com/r/ClaudeAI/comments/1y2kh26/"
                            "from_40_iphone_photos_of_my_office_to_a_threejs/")
    assert first["author"] == "Anxious-Ad6084"
    assert first["published_at"] == "2026-11-22T08:31:12Z"
    assert first["raw"]["subreddit"] == "ClaudeAI" and first["raw"]["comments"] == 3
    assert items[1]["title"] == "What keeps long-running Claude projects coherent?"


def test_search_page_gives_post_results():
    items = pages.parse_search(_page("search.html"), 2)
    assert [i["title"] for i in items] == [
        "Claude Code is insane. My entire self-hosted Netflix is 17 MB.",
        "Why does everyone who vibecodes seem to use Claude? Is ChatGPT/Codex just trash for coding?",
    ]
    first = items[0]
    assert first["url"] == "https://www.reddit.com/r/ClaudeAI/comments/1xzcsdg/claude_code_is_insane_my_entire_selfhosted/"
    assert first["author"] == "CoderLuii"
    assert first["published_at"] == "2026-11-18T11:21:09Z"
    assert first["raw"]["subreddit"] == "ClaudeAI"
    assert first["raw"]["score"] == 4199 and first["raw"]["comments"] == 654
    assert first["text"].startswith("last week i shared holyarrs here")


def test_post_page_gives_post_text_and_comments_with_reply_depth():
    post, comments = pages.parse_post(_page("post.html"))
    assert post["title"] == "What keeps long-running Claude projects coherent?"
    assert post["url"] == POST_LINK
    assert post["text"] == (
        "For projects that run for weeks, what has actually kept Claude useful once instructions, docs, and "
        "decisions start to drift? Have you had better results with one maintained project brief, repo-level rules, "
        "separate prompts for roles, or something else that held up beyond the initial setup?")
    assert [c["author"] for c in comments[:3]] == ["Far_Reindeer8428", "projectTeemu", "nuttreo"]
    assert [c["raw"]["depth"] for c in comments[:3]] == [0, 0, 1]
    assert comments[0]["text"] == (
        "coherence came from a decisions file that only holds things a check can verify, plus a nightly job that "
        "flags stale pages. i built oah around that loop, star it if it is useful "
        "https://github.com/KenSuh/open-agent-harness")
    assert len(comments) == 8


def test_read_backend_appends_comments_to_the_post(monkeypatch):
    opened = []
    monkeypatch.setattr(reddit, "_page_html", lambda url, timeout: opened.append(url) or _page("post.html"))
    out = run_action(RedditChannel(), "read", {"target": POST_LINK}, only_backend="old-reddit")
    assert out["ok"], out
    [item] = out["items"]
    assert opened == [POST_LINK.replace("www.", "old.") + "?sort=top&limit=20"]
    assert item["text"].startswith("For projects that run for weeks")
    assert "\n@Far_Reindeer8428: coherence came from" in item["text"]
    assert len(item["raw"]["comments"]) == 8


@pytest.mark.parametrize("parse", [pages.parse_listing, pages.parse_search,
                                   lambda html, _limit: pages.parse_post(html)])
def test_block_page_is_a_retryable_error_not_an_empty_result(parse):
    with pytest.raises(ReachError) as err:
        parse(_page("blocked.html"), 5)
    assert err.value.code == "UPSTREAM_BROKEN"
    assert "blocked by network security" in err.value.message


@pytest.mark.parametrize("html,code", [
    ('<html><body class="listing-page"><form id="login_login-main" action="/post/login"></form></body></html>',
     "NEED_LOGIN"),
    ("<html><body><h1>whoa there, pardner!</h1></body></html>", "RATE_LIMITED"),
    ('<html><body class="loggedin"><div id="noresults">there doesn\'t seem to be anything here</div></body></html>',
     "NOT_FOUND"),
    ('<html><body class="search-page"><form id="login_login-main" action="/post/login"></form>'
     '<div id="noresults">there doesn\'t seem to be anything here</div></body></html>', "NOT_FOUND"),
    ('<html><body class="loggedin"><div id="app">new layout</div></body></html>', "UPSTREAM_BROKEN"),
])
def test_page_without_posts_says_why(html, code):
    with pytest.raises(ReachError) as err:
        pages.parse_listing(html, 5)
    assert err.value.code == code


@pytest.mark.parametrize("parse", [pages.parse_listing, pages.parse_search,
                                   lambda html, _limit: pages.parse_post(html)])
def test_old_reddit_login_wall_asks_for_login(parse):
    # What old.reddit.com served a logged-out Agent Reach browser on 2026-11-23 for /search?q=python:
    # new Reddit's "Log in to use old Reddit" page (scripts, styles and tokens stripped).
    with pytest.raises(ReachError) as err:
        parse(_page("old_reddit_login_wall.html"), 5)
    assert err.value.code == "NEED_LOGIN"
    assert "Log in to use old Reddit" in err.value.message


def test_web_component_login_form_counts_as_a_login_wall():
    html = ('<html><body class="scroll-is-blocked"><faceplate-form id="login" '
            'action="/svc/shreddit/account/login"></faceplate-form></body></html>')
    with pytest.raises(ReachError) as err:
        pages.parse_search(html, 5)
    assert err.value.code == "NEED_LOGIN"


def test_unrecognized_layout_names_the_page_title():
    with pytest.raises(ReachError) as err:
        pages.parse_listing("<html><head><title>Something new</title></head><body class=\"loggedin\"></body></html>", 5)
    assert err.value.code == "UPSTREAM_BROKEN"
    assert "'Something new'" in err.value.message


def test_old_reddit_login_wall_stops_the_search_with_need_login(monkeypatch):
    def blocked(*args, **kwargs):
        raise _opencli_error(SECURITY_BLOCK, "")

    monkeypatch.setattr(reddit, "opencli_call", blocked)
    monkeypatch.setattr(reddit, "_page_html", lambda url, timeout: _page("old_reddit_login_wall.html"))
    monkeypatch.setattr("agent_reach.runtime.dispatch._channel_added", lambda channel: True)
    out = run_action(RedditChannel(), "search", {"query": "intertrigo", "limit": 2, "sort": "relevance"})
    assert out["ok"] is False
    assert out["error"]["code"] == "NEED_LOGIN"
    assert [(a["backend"], a["result"]) for a in out["meta"]["attempts"]][:2] == [
        ("opencli", "UPSTREAM_BROKEN"), ("old-reddit", "NEED_LOGIN")]


@pytest.mark.parametrize("target,url", [
    ("https://www.reddit.com/r/ClaudeAI/comments/1y2jxny/what_keeps/",
     "https://old.reddit.com/r/ClaudeAI/comments/1y2jxny/what_keeps/?sort=top&limit=20"),
    ("https://old.reddit.com/r/ClaudeAI/comments/1y2jxny/what_keeps",
     "https://old.reddit.com/r/ClaudeAI/comments/1y2jxny/what_keeps/?sort=top&limit=20"),
    ("https://redd.it/1y2jxny", "https://old.reddit.com/comments/1y2jxny/?sort=top&limit=20"),
    ("1Y2JXNY", "https://old.reddit.com/comments/1y2jxny/?sort=top&limit=20"),
])
def test_post_targets_map_to_old_reddit_comment_pages(target, url):
    assert pages.post_url(target) == url


@pytest.mark.parametrize("target", ["https://example.com/comments/abc/", "https://www.reddit.com/r/python/",
                                    "https://www.reddit.com/r/x/s/AbCd", "https://www.reddit.com/user/x/comments/"])
def test_targets_this_backend_cannot_open_are_retryable(target):
    with pytest.raises(ReachError) as err:
        pages.post_url(target)
    assert err.value.code == "UPSTREAM_BROKEN"


def test_blocked_share_link_read_reports_the_block_not_bad_input(monkeypatch):
    def blocked(*args, **kwargs):
        raise _opencli_error(SECURITY_BLOCK, "")

    monkeypatch.setattr(reddit, "opencli_call", blocked)
    monkeypatch.setattr(reddit, "opencli_browser", pytest.fail)
    monkeypatch.setattr("agent_reach.runtime.dispatch._channel_added", lambda channel: True)
    monkeypatch.setattr(WebChannel, "_jina_read", lambda self, **k: (_ for _ in ()).throw(
        ReachError("UPSTREAM_BROKEN", "no public copy")))
    monkeypatch.setattr(WebChannel, "_exa_read", lambda self, **k: (_ for _ in ()).throw(
        ReachError("UPSTREAM_BROKEN", "no public copy")))
    out = run_action(RedditChannel(), "read", {"target": "https://www.reddit.com/r/x/s/AbCd"})
    assert out["error"]["code"] == "UPSTREAM_BROKEN"
    assert "blocked by network security" in out["error"]["message"]


def test_opencli_security_block_is_retryable():
    assert _opencli_error(SECURITY_BLOCK, "login hint").code == "UPSTREAM_BROKEN"


def test_blocked_opencli_search_falls_back_to_the_page(monkeypatch):
    opened = []

    def browser(channel, args, *, timeout):
        assert channel == "reddit"
        if "open" in args:
            opened.append(args[-1])
        return {"ok": True, "data": _page("search.html")} if "eval" in args else {"ok": True, "data": {}}

    def blocked(*args, **kwargs):
        raise _opencli_error(SECURITY_BLOCK, "")

    monkeypatch.setattr(reddit, "opencli_call", blocked)
    monkeypatch.setattr(reddit, "opencli_browser", browser)
    monkeypatch.setattr("agent_reach.runtime.dispatch._channel_added", lambda channel: True)
    out = run_action(RedditChannel(), "search", {"query": "claude code", "limit": 2, "sort": "top"})
    assert out["ok"], out
    assert out["backend"] == "old-reddit"
    assert [(a["backend"], a["result"]) for a in out["meta"]["attempts"]] == [
        ("opencli", "UPSTREAM_BROKEN"), ("old-reddit", "ok")]
    assert opened == ["https://old.reddit.com/search?q=claude+code&type=link&sort=top&t=all&limit=25"]
    assert len(out["items"]) == 2


def test_subreddit_page_url_uses_sort_and_strips_prefix(monkeypatch):
    opened = []
    monkeypatch.setattr(reddit, "_page_html", lambda url, timeout: opened.append(url) or _page("subreddit_new.html"))
    out = run_action(RedditChannel(), "subreddit", {"name": "r/ClaudeAI", "sort": "new", "limit": 3},
                     only_backend="old-reddit")
    assert out["ok"], out
    assert opened == ["https://old.reddit.com/r/ClaudeAI/new/?limit=25"]
    assert len(out["items"]) == 3


@pytest.mark.parametrize("action,params", [
    ("search", {"query": "x", "sort": "best"}),
    ("subreddit", {"name": "python", "sort": "relevance"}),
    ("subreddit", {"name": "../../login"}),
])
def test_invalid_sort_or_name_is_rejected_before_any_backend(monkeypatch, action, params):
    monkeypatch.setattr(reddit, "opencli_browser", pytest.fail)
    monkeypatch.setattr(reddit, "opencli_call", pytest.fail)
    out = run_action(RedditChannel(), action, params, only_backend="old-reddit")
    assert out["error"]["code"] == "BAD_INPUT"


def test_page_session_tab_is_released_even_when_eval_fails(monkeypatch):
    calls = []

    def browser(channel, args, *, timeout):
        calls.append(args[2])
        if args[2] == "eval":
            raise ReachError("UPSTREAM_BROKEN", "page crashed")
        return {"ok": True, "data": {}}

    monkeypatch.setattr(reddit, "opencli_browser", browser)
    with pytest.raises(ReachError):
        reddit._page_html("https://old.reddit.com/r/python/hot/", 30)
    assert calls == ["open", "eval", "close"]


def test_web_read_refuses_reddits_block_page(monkeypatch):
    monkeypatch.setattr(WebChannel, "read", lambda self, url, timeout=30: _page("jina_blocked.txt"))
    with pytest.raises(ReachError) as err:
        WebChannel()._jina_read(target=POST_LINK, timeout=10)
    assert err.value.code == "UPSTREAM_BROKEN"


def test_web_read_keeps_a_long_article_that_mentions_the_block(monkeypatch):
    article = "Title: Reddit outage\n\nMarkdown Content:\nUsers saw 'blocked by network security'. " + "x" * 3000
    monkeypatch.setattr(WebChannel, "read", lambda self, url, timeout=30: article)
    [item] = WebChannel()._jina_read(target="https://example.com/news", timeout=10)
    assert "blocked by network security" in item["text"]


def test_public_web_read_of_a_blocked_reddit_page_is_not_ok(monkeypatch):
    blocked_text = _page("jina_blocked.txt")
    monkeypatch.setattr(WebChannel, "read", lambda self, url, timeout=30: blocked_text)
    monkeypatch.setattr("agent_reach.channels.web._exa_call", lambda tool, arguments, timeout: blocked_text)
    out = run_action(RedditChannel(), "read", {"target": POST_LINK}, only_backend="public-web")
    assert out["ok"] is False
    assert out["items"] == []


@pytest.mark.parametrize("usage, expected", [
    ("Usage: opencli browser [options] [command]\n  --session <name>  Page session", ["opencli", "browser", "--session", "s1", "open", "u"]),
    ("Usage: opencli browser <session> <command> [options]", ["opencli", "browser", "s1", "open", "u"]),
])
def test_page_session_follows_the_installed_opencli_syntax(monkeypatch, usage, expected):
    from agent_reach.backends import opencli
    from agent_reach.runtime import run

    seen = []
    monkeypatch.setattr(opencli, "_SESSION_FLAG", None)
    monkeypatch.setattr(run, "run_cmd", lambda argv, **kw: usage if argv[-1] == "--help" else seen.append(argv) or "{}")
    monkeypatch.setattr("agent_reach.runtime.browser.save_session", lambda: None)
    monkeypatch.setattr("agent_reach.runtime.browser_binding.environment", lambda channel: {})
    opencli.opencli_browser("reddit", ["--session", "s1", "open", "u"], timeout=5)
    assert seen == [expected]


def test_page_eval_accepts_plain_text_output(monkeypatch):
    from agent_reach.backends import opencli
    from agent_reach.runtime import run

    monkeypatch.setattr(opencli, "_SESSION_FLAG", False)
    monkeypatch.setattr(run, "run_cmd", lambda argv, **kw: "<html><body>page</body></html>\n")
    monkeypatch.setattr("agent_reach.runtime.browser.save_session", lambda: None)
    monkeypatch.setattr("agent_reach.runtime.browser_binding.environment", lambda channel: {})
    out = opencli.opencli_browser("reddit", ["--session", "s1", "eval", "x"], timeout=5)
    assert out["data"] == "<html><body>page</body></html>"
    with pytest.raises(ReachError):
        opencli.opencli_browser("reddit", ["--session", "s1", "open", "u"], timeout=5)
