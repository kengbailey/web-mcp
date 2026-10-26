"""XenForo 2 thread parser (src/core/sites/xenforo.py): detection, post rendering, paging, compactness.

Fixtures: ``xenforo_thread_p1.html`` / ``xenforo_thread_p12.html`` reproduce XenForo 2.2 thread_view
markup with XDA-style chrome (fictional content); ``xenforo_features.html`` is a small hand-made page
covering quotes, code, spoilers, images, links, signatures and attachments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from lxml import html as lxml_html
from markdownify import markdownify

from src.core.pages import Doc
from src.core.sites.xenforo import is_thread, parse_thread

FIXTURES = Path(__file__).parent / "fixtures"
THREAD = "https://xdaforums.com/t/unlock-magisk-twrp-firetv-3nd-gen-cube-gazelle-ps7717.4800530/"
FEATURES_URL = "https://forum.example.com/threads/feature-test.42/"
HEADER = re.compile(r"^#\d+ · [^·\n]+ · \d{4}-\d{2}-\d{2}$")


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def p1() -> Doc:
    return parse_thread(load("xenforo_thread_p1.html"), THREAD)


@pytest.fixture(scope="module")
def p12() -> Doc:
    return parse_thread(load("xenforo_thread_p12.html"), THREAD + "page-12")


@pytest.fixture(scope="module")
def features() -> Doc:
    return parse_thread(load("xenforo_features.html"), FEATURES_URL)


@pytest.fixture(scope="module")
def features_links() -> Doc:
    return parse_thread(load("xenforo_features.html"), FEATURES_URL, links=True)


def headers(doc: Doc) -> list[str]:
    return [line for line in doc.text.splitlines() if re.match(r"^#\d+ · ", line)]


def visible_text(raw: str) -> str:
    root = lxml_html.document_fromstring(raw)
    for el in root.xpath("//script | //style | //noscript | //template"):
        el.drop_tree()
    return " ".join(root.body.text_content().split())


# --- detection -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["xenforo_thread_p1.html", "xenforo_thread_p12.html", "xenforo_features.html"])
def test_is_thread_true_on_fixtures(name):
    assert is_thread(load(name))


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<!DOCTYPE html><html lang='en'><head><title>Blog</title></head><body><article>Hi</article></body></html>",
        '<html id="XF" data-xf="2.2" data-template="forum_view"><body></body></html>',
        '<html lang="en"><body><pre>&lt;html id="XF" data-template="thread_view"&gt;</pre></body></html>',
        '<html data-id="XF" data-template="thread_view"><body></body></html>',
    ],
)
def test_is_thread_false(html):
    assert not is_thread(html)


def test_is_thread_accepts_typed_thread_templates():
    assert is_thread('<!DOCTYPE html>\n<html id="XF" lang="en" data-template="thread_view_type_question">')


def test_parse_returns_none_without_posts():
    empty = '<html id="XF" data-template="thread_view"><body><div class="block--messages"></div></body></html>'
    assert parse_thread(empty, THREAD) is None
    assert parse_thread("", THREAD) is None
    assert parse_thread("<html><body><p>not a forum</p></body></html>", THREAD) is None


# --- page-level fields -----------------------------------------------------------------------


def test_page1_doc_fields(p1):
    assert p1.source == "xenforo"
    assert p1.url == THREAD
    assert p1.title == "[UNLOCK][MAGISK][TWRP] FireTV 3nd gen Cube (gazelle/ps7717)"
    assert p1.note == "thread page 1/12 · posts #1–8"
    assert p1.next_url == THREAD + "page-2"


def test_last_page_doc_fields(p12):
    assert p12.title == "[UNLOCK][MAGISK][TWRP] FireTV 3nd gen Cube (gazelle/ps7717)"
    assert p12.note == "thread page 12/12 · posts #221–226"
    assert p12.next_url == ""


def test_title_falls_back_to_title_tag_minus_site(features):
    assert features.title == "Feature test | thread"
    page = '<html id="XF" data-template="thread_view"><head><title>Some thread | Page 3 | Board</title></head>'
    page += '<body><article class="message js-post" data-author="a"><div class="message-body">'
    page += '<div class="bbWrapper">hi</div></div></article></body></html>'
    assert parse_thread(page, THREAD).title == "Some thread"


def test_single_page_without_pagenav(features):
    assert features.note == "thread page 1/1 · posts #1–4"
    assert features.next_url == ""


def test_current_page_from_url_when_no_pagenav():
    doc = parse_thread(load("xenforo_features.html"), FEATURES_URL + "page-3")
    assert doc.note.startswith("thread page 3/3 · ")
    assert doc.next_url == ""


def test_pagenav_current_wins_over_url():
    doc = parse_thread(load("xenforo_thread_p12.html"), THREAD + "page-99")
    assert doc.note.startswith("thread page 12/12 · ")


def test_next_url_built_from_thread_base_without_next_link():
    raw = load("xenforo_thread_p1.html").replace("pageNav-jump--next", "pageNav-jump--gone")
    assert parse_thread(raw, THREAD + "post-88931201").next_url == THREAD + "page-2"
    assert parse_thread(raw, THREAD.rstrip("/") + "?utm=x#posts").next_url == THREAD + "page-2"


# --- posts -----------------------------------------------------------------------------------


def test_post_headers(p1, p12):
    lines = headers(p1)
    assert lines[0] == "#1 · cube_hacker · 2026-08-14"
    assert lines[3] == "#4 · sideload_sam · 2026-08-15"
    assert [int(line[1:].split(" ")[0]) for line in lines] == list(range(1, 9))
    assert all(HEADER.match(line) for line in lines + headers(p12))
    assert headers(p12)[0] == "#221 · gazelle_dev · 2026-10-19"
    # each post: header line, then the body on the next line; posts separated by a blank line
    assert p1.text.startswith("#1 · cube_hacker · 2026-08-14\n**Fire TV Cube 3rd gen")
    assert "\n\n#2 · cube_hacker · 2026-08-14\n**Reserved** for FAQ." in p1.text


def test_deleted_post_skipped_and_missing_number_continues(features):
    assert headers(features) == ["#1 · alice · 2026-09-06", "#3 · bob · 2026-09-08", "#4 · dave · 2026-09-09"]
    assert "spammer" not in features.text and "was deleted" not in features.text


def test_quotes_collapsed_to_one_line(p1, features):
    line = "> sideload_sam: Mine lights up blue every time I plug it in, even holding the button. Am I doing something"
    line += " wrong?"
    assert f"\n{line}\n" in p1.text
    assert "said:" not in p1.text
    quote = next(line for line in features.text.splitlines() if line.startswith("> alice: "))
    snippet = quote[len("> alice: "):]
    assert snippet.endswith("…") and len(snippet) <= 201
    assert snippet.startswith("This is a deliberately long quoted paragraph")
    assert "QUOTE-TAIL-MARKER" not in features.text
    assert "NESTED-QUOTE-TEXT" not in features.text and "carol" not in features.text
    # Unattributed [QUOTE] boxes are the author's own content: kept whole, unwrapped.
    assert "\nAn anonymous quote.\n" in features.text and "> An anonymous" not in features.text
    for doc in (p1, features):
        assert "Click to expand" not in doc.text and "Click to shrink" not in doc.text


def test_code_blocks_fenced_and_preserved(p1, features):
    assert (
        "```bash\nsudo ./bootrom-step.sh\n# wait for:  [+] Found device, sending payload\n"
        "sudo python3 amonet.py --unlock\n    [+] bootloader: unlocked\n    [+] rpmb: patched\n```"
    ) in p1.text
    assert "```\nadb shell\nsu\npm enable com.amazon.alexa.voice\n" in p1.text
    assert (
        '```php\nfunction greet($name) {\n    if ($name < 1 && true) {\n        return "a  b";\n    }\n}\n```'
    ) in features.text
    assert "````\necho ```not a fence```\n````" in features.text
    assert "Code:" not in p1.text and "PHP:" not in features.text
    assert "Inline `snake_case_name` stays." in features.text


def test_spoilers_keep_inner_text(p1, features):
    assert "Spoiler: Changelog\n\n**2026-10-20** – payload v4" in p1.text
    assert "Spoiler: Ending\n\nThe butler did it." in features.text
    assert "Untitled spoiler text." in features.text
    assert "And an inline secret too." in features.text
    assert "Click to reveal" not in features.text


def test_no_images_smilies_or_chrome(p1, p12, features):
    for doc in (p1, p12, features):
        text = doc.text
        assert "![" not in text and "data:image" not in text and "<img" not in text
        for chrome in (
            "Joined", "Messages", "Reaction score", "Report", "Bookmark", "Share", "Last edited",
            "Advertisement", "Similar threads", "Top Liked Posts", "Recognized Developer", "Senior Member",
            "and 211 others", "Log in", "Thread starter", "Contact us", "cookies",
        ):
            assert chrome not in text, chrome
    for emoji in ("😉", "🤦", "😕", "👍", "😀"):
        assert emoji not in p1.text + p12.text + features.text
    # filename / URL alts are noise; a descriptive alt survives
    assert "twrp-gazelle-screenshot" not in p1.text
    assert "[image: Magisk app showing installed modules on the Cube]" in p1.text
    assert "IMG_20260906_1234" not in features.text and "imgur" not in features.text
    assert "wiring.png" not in features.text
    assert "[image: Wiring diagram for the UART header]" in features.text


def test_signatures_and_reactions_removed(p1, features):
    assert "buy me a coffee" not in p1.text and "Please don't PM me" not in p1.text
    assert "SIGNATURE-TEXT" not in features.text and "sig link" not in features.text
    assert "REACTOR-NAME" not in features.text


def test_attachments_listed_by_name(p1, features):
    assert "[attachment: gazelle-unlock-v4.zip] [attachment: twrp-3.7.0_gazelle.img]" in p1.text
    assert "[attachment: photo.jpg] [attachment: firmware.zip]" in features.text
    assert "Views:" not in p1.text


def test_links_stripped_by_default(p1, features):
    assert "](" not in p1.text and "](" not in features.text
    assert "Links: the docs, another thread, @bob, https://bare.example.net/path and a js toggle." in features.text
    assert "Block OTA updates first: firetv-ota-block (no root needed)." in p1.text
    assert "Big Story Headline (news.example.com)" in features.text
    assert "UNFURL-DESCRIPTION" not in features.text


def test_links_kept_absolute_when_requested(features_links):
    text = features_links.text
    assert "[the docs](https://example.org/docs)" in text
    assert "[another thread](https://forum.example.com/threads/other-thread.77/)" in text
    assert "[@bob](https://forum.example.com/members/bob.6/)" in text
    assert "<https://bare.example.net/path>" in text
    assert "[Big Story Headline](https://news.example.com/story)" in text
    assert "javascript:" not in text and "proxy.php" not in text
    assert "and a js toggle." in text


def test_markdown_structure(features):
    assert "## Overview\n\nIntro line with **bold** and *italic* text and a smilie here." in features.text
    assert "- first item\n- second item\n  - nested item" in features.text


def test_text_is_tidy(p1, p12, features, features_links):
    for doc in (p1, p12, features, features_links):
        assert "\n\n\n" not in doc.text
        assert all(line == line.rstrip() for line in doc.text.splitlines())
        assert doc.text == doc.text.strip()


def test_parse_is_pure():
    raw = load("xenforo_features.html")
    first = parse_thread(raw, FEATURES_URL)
    assert parse_thread(raw, FEATURES_URL) == first
    assert raw == load("xenforo_features.html")


# --- compactness -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,url,max_ratio",
    [("xenforo_thread_p1.html", THREAD, 0.6), ("xenforo_thread_p12.html", THREAD + "page-12", 0.35)],
)
def test_compact_versus_page_text(name, url, max_ratio):
    raw = load(name)
    doc = parse_thread(raw, url)
    visible = visible_text(raw)
    page_md = markdownify(raw, strip=["script", "style"])
    page_md = "\n".join(line.rstrip() for line in page_md.splitlines() if line.strip())
    assert len(doc.text) < max_ratio * len(visible)
    assert len(doc.text) < 0.25 * len(page_md)
    assert len(doc.text) < 0.05 * len(raw)


def test_unattributed_quote_box_keeps_full_content():
    html = (
        '<html id="XF"><body data-template="thread_view"><h1 class="p-title-value">T</h1>'
        '<article class="message message--post js-post" data-author="op" data-content="post-1">'
        '<ul class="message-attribution-opposite"><li><a href="/t/x.1/post-1">#1</a></li></ul>'
        '<time datetime="2026-09-04T03:05:38+0000"></time><div class="message-body"><div class="bbWrapper">'
        '<blockquote data-quote="" data-source="" class="bbCodeBlock bbCodeBlock--expandable bbCodeBlock--quote">'
        '<div class="bbCodeBlock-content"><div class="bbCodeBlock-expandContent"><b><b>INSTRUCTIONS</b></b>'
        '<ol>' + "".join(f"<li>Step {i} " + "detail " * 30 + "</li>" for i in range(1, 9)) + '</ol>'
        '</div><div class="bbCodeBlock-expandLink"><a>Click to expand...</a></div></div></blockquote>'
        '</div></div></article></body></html>'
    )
    doc = parse_thread(html, "https://forum.example/t/x.1/")
    assert "8. Step 8" in doc.text and "Click to expand" not in doc.text
    assert "**INSTRUCTIONS**" in doc.text and "****" not in doc.text
