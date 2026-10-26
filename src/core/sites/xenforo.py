"""XenForo 2 thread pages -> one compact post per paragraph.

``is_thread`` is a cheap string check on the ``<html>`` tag. ``parse_thread`` turns one
rendered thread page into a :class:`Doc` whose text is::

    #21 · someuser · 2026-09-06
    body markdown (quotes collapsed to one line, code fenced, chrome removed)

Pure and synchronous: no network, no shared state.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html
from markdownify import MarkdownConverter

from ..pages import Doc
from ..text import SEP, oneline, tidy, trunc

SOURCE = "xenforo"
QUOTE_CHARS = 200

_HTML_TAG = re.compile(r"<html\b[^>]*>", re.I)
_XF_ID = re.compile(r"""\sid\s*=\s*["']?XF["'\s>]""")
_THREAD_TEMPLATE = re.compile(r"""\bdata-template\s*=\s*["']?thread_view""")
_URL_PAGE = re.compile(r"/page-(\d+)(?=[/?#&]|$)")
_BASE_TAIL = re.compile(r"(?:page-\d+|post-\d+|unread|latest)/?$")
_POST_NO = re.compile(r"^#\s*([\d,.]+)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_OF_PAGES = re.compile(r"(\d+)\s+of\s+(\d+)")
_TOGGLE_LINE = re.compile(r"^\s*Click to (?:expand|shrink)\.\.\.\s*$", re.M)
_FILE_ALT = re.compile(r"^[^/\\]{1,120}\.(?:png|jpe?g|gif|webp|bmp|svg|heic|avif|tiff?)$", re.I)
_GENERIC_ALT = {"image", "img", "[img]", "photo", "picture", "pic", "attachment", "screenshot", "thumbnail"}
_BLOCK_TAGS = {"br", "p", "div", "li", "tr", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"}
_BLANKS = re.compile(r"\n{3,}")

_MD_OPTS = dict(heading_style="ATX", bullets="-", escape_asterisks=False, escape_underscores=False)
_MD_PLAIN = MarkdownConverter(strip=["a", "img"], **_MD_OPTS)
_MD_LINKS = MarkdownConverter(strip=["img"], **_MD_OPTS)


def is_thread(html: str) -> bool:
    """Cheap check: is this a rendered XenForo 2 thread page (``<html id="XF" data-template="thread_view…">``)?"""
    match = _HTML_TAG.search(html[:20000])
    if not match:
        return False
    tag = match.group(0)
    return bool(_XF_ID.search(tag) and _THREAD_TEMPLATE.search(tag))


def parse_thread(html: str, url: str, links: bool = False) -> Doc | None:
    """Parse one XenForo 2 thread page into a compact Doc.

    Args:
        html: Rendered page HTML.
        url: The page URL (used for page number, next page and absolute links).
        links: Keep body links as ``[text](absolute url)``; otherwise links become plain text.

    Returns:
        Doc with source "xenforo", or None when the page has no posts.
    """
    try:
        root = lxml_html.document_fromstring(html)
    except (etree.ParserError, ValueError):
        return None

    posts: list[tuple[int, str]] = []
    seen: set[str] = set()
    prev = 0
    for article in root.xpath(_POSTS):
        key = article.get("data-content") or article.get("id") or ""
        if key and key in seen:
            continue
        seen.add(key)
        rendered = _post(article, url, links)
        if rendered is None:
            continue
        number, author, date, body = rendered
        number = number or prev + 1
        prev = number
        header = SEP.join(p for p in (f"#{number}", author, date) if p)
        posts.append((number, header + ("\n" + body if body else "")))
    if not posts:
        return None

    current, last = _pages(root, url)
    note = f"thread page {current}/{last}{SEP}posts #{posts[0][0]}–{posts[-1][0]}"
    return Doc(
        url=url,
        source=SOURCE,
        text="\n\n".join(text for _, text in posts),
        title=_title(root),
        note=note,
        next_url=_next_url(root, url, current) if current < last else "",
    )


# --- xpath helpers ---------------------------------------------------------------------------


def _has(cls: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {cls} ')"


_QUOTE = _has("bbCodeBlock--quote")
_NESTED_BOLD = re.compile(r"\*{4}(\S(?:.*?\S)?)\*{4}")
_ATTRIBUTED = "(normalize-space(@data-quote) != '' or normalize-space(@data-source) != '')"
_POSTS = (
    f"//article[{_has('message')} and ({_has('js-post')} or starts-with(@data-content, 'post-')"
    " or starts-with(@id, 'js-post-'))]"
)


def _first(el: etree._Element, xpath: str) -> etree._Element | None:
    found = el.xpath(xpath)
    return found[0] if found else None


def _text(el: etree._Element) -> str:
    """Visible text of ``el`` on one line, with block boundaries kept as spaces (mutates tails)."""
    for node in el.iter():
        if isinstance(node.tag, str) and node.tag.lower() in _BLOCK_TAGS and node is not el:
            node.tail = " " + (node.tail or "")
    return oneline("".join(el.itertext()))


def _drop(el: etree._Element) -> None:
    el.drop_tree()  # HtmlElement: keeps the tail text


def _swap(old: etree._Element, text: str = "", tag: str = "p") -> etree._Element:
    new = lxml_html.Element(tag)
    new.text = text
    new.tail = old.tail
    old.getparent().replace(old, new)
    return new


# --- page-level fields -----------------------------------------------------------------------


def _title(root: etree._Element) -> str:
    h1 = _first(root, f"//h1[{_has('p-title-value')}]")
    heading = _text(h1) if h1 is not None else ""
    if heading:
        return heading
    og = root.xpath("//meta[@property='og:title']/@content")
    if og and oneline(og[0]):
        return oneline(og[0])
    title = oneline(root.findtext(".//title") or "")
    title = re.sub(r"\s+\|\s+Page\s+\d+(?=\s+\||$)", "", title)
    site = root.xpath("//meta[@property='og:site_name']/@content")
    if site and title.endswith(" | " + oneline(site[0])):
        return title[: -len(oneline(site[0])) - 3].strip()
    return title.rsplit(" | ", 1)[0].strip() if " | " in title else title


def _pages(root: etree._Element, url: str) -> tuple[int, int]:
    """(current, last) from the pageNav; the URL's ``/page-N`` is the fallback for current."""
    numbers: list[int] = []
    current = 0
    for li in root.xpath(f"//li[{_has('pageNav-page')}]"):
        label = oneline("".join(a.text_content() for a in li.xpath("./a")))
        if label.isdigit():
            numbers.append(int(label))
            if "pageNav-page--current" in (li.get("class") or ""):
                current = current or int(label)
    for label in root.xpath(f"//*[{_has('pageNavSimple-el--current')}]//text()"):
        match = _OF_PAGES.search(label)
        if match:
            current = current or int(match.group(1))
            numbers.append(int(match.group(2)))
    if not current:
        match = _URL_PAGE.search(url)
        current = int(match.group(1)) if match else 1
    return current, max(numbers + [current])


def _next_url(root: etree._Element, url: str, current: int) -> str:
    href = root.xpath(f"//a[{_has('pageNav-jump--next')}]/@href")
    if href and href[0].strip():
        return urljoin(url, href[0].strip())
    parts = urlsplit(url)
    path = _BASE_TAIL.sub("", parts.path)
    path = path if path.endswith("/") else path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path + f"page-{current + 1}", "", ""))


# --- one post --------------------------------------------------------------------------------


def _post(article: etree._Element, url: str, links: bool) -> tuple[int, str, str, str] | None:
    body_el = _first(article, f".//*[{_has('message-body')}]//*[{_has('bbWrapper')}]")
    if body_el is None:
        return None  # deleted / ignored placeholder
    attribution = _first(article, f".//*[{_has('message-attribution')}]")
    scope = attribution if attribution is not None else article

    number = 0
    for link in scope.xpath(".//a"):
        match = _POST_NO.match(oneline(link.text_content()))
        if match:
            number = int(re.sub(r"\D", "", match.group(1)) or 0)
            break

    author = oneline(article.get("data-author"))
    if not author:
        name = _first(article, f".//*[{_has('message-name')}]")
        author = _text(name) if name is not None else ""

    date = ""
    stamp = _first(scope, ".//time[@datetime]")
    if stamp is None:
        stamp = _first(article, f".//time[@datetime and not(ancestor::*[{_has('message-body')}])]")
    if stamp is not None:
        match = _DATE.match(stamp.get("datetime", "").strip())
        if match:
            date = match.group(0)
        elif (stamp.get("data-time") or "").isdigit():
            date = datetime.fromtimestamp(int(stamp.get("data-time")), timezone.utc).strftime("%Y-%m-%d")

    body = _body(body_el, url, links)
    files = []
    for name in article.xpath(f".//*[{_has('message-attachments')}]//*[{_has('file-name')}]"):
        label = oneline(name.get("title") or name.text_content())
        if label and label not in files:
            files.append(label)
    if files:
        line = " ".join(f"[attachment: {name}]" for name in files)
        body = f"{body}\n\n{line}" if body else line
    return number, author, date, body


def _body(el: etree._Element, url: str, links: bool) -> str:
    """bbWrapper element -> tidy markdown. Mutates ``el`` (the caller owns the parsed tree)."""
    blocks: dict[str, str] = {}

    def hold(old: etree._Element, rendered: str) -> None:
        token = f"XFBLOCK{len(blocks)}Z"
        blocks[token] = rendered
        _swap(old, token)

    for junk in el.xpath(
        ".//script | .//style | .//noscript | .//template"
        f" | .//img[{_has('smilie')}] | .//*[{_has('bbCodeBlock-expandLink')}]"
        f" | .//*[{_has('bbCodeBlock-shrinkLink')}] | .//*[{_has('message-signature')}]"
        f" | .//*[{_has('reactionsBar')}] | .//*[{_has('js-selectToQuoteEnd')}]"
    ):
        if junk.getparent() is not None:
            _drop(junk)

    # An unattributed [QUOTE] is the author's own boxed section (XDA OPs put whole
    # instructions in one): unwrap it. Only quotes of other posts collapse to a snippet.
    for box in el.xpath(f".//blockquote[{_QUOTE} and not({_ATTRIBUTED})]"):
        content = _first(box, f".//*[{_has('bbCodeBlock-expandContent')}]")
        box.tag = "div"
        box.attrib.clear()
        if content is not None:
            for child in list(box):
                if child is not content and content not in child.iterdescendants():
                    _drop(child)
    for quote in el.xpath(f".//blockquote[{_QUOTE} and not(ancestor::blockquote[{_QUOTE}])]"):
        hold(quote, _quote(quote))

    for code in el.xpath(f".//*[{_has('bbCodeBlock--code')}]"):
        if code.getparent() is not None:
            hold(code, _code(code))

    for spoiler in el.xpath(f".//*[{_has('bbCodeSpoiler')}]"):
        button = _first(spoiler, f".//*[{_has('bbCodeSpoiler-button')}]")
        if button is None:
            continue
        title_el = _first(button, f".//*[{_has('bbCodeSpoiler-button-title')}]")
        title = _text(title_el) if title_el is not None else ""
        if title:
            _swap(button, f"Spoiler: {title}")
        else:
            _drop(button)

    for card in el.xpath(f".//*[{_has('bbCodeBlock--unfurl')}]"):
        href = card.get("data-url") or next(iter(card.xpath(".//a/@href")), "")
        title_el = _first(card, f".//*[{_has('js-unfurl-title')} or {_has('contentRow-header')}]")
        title = (_text(title_el) if title_el is not None else "") or href
        host = card.get("data-host") or urlsplit(href).netloc
        p = _swap(card, "")
        if links and href:
            a = etree.SubElement(p, "a", href=href)
            a.text = title
        else:
            p.text = f"{title} ({host})" if host and host not in title else title

    for img in el.xpath(".//img"):
        alt = _alt(img.get("alt"))
        if alt:
            _swap(img, f"[image: {alt}]", tag="span")
        else:
            _drop(img)

    for a in el.xpath(".//a"):
        for attr in ("title", "target", "rel"):
            a.attrib.pop(attr, None)
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            a.attrib.pop("href", None)
        elif links:
            a.set("href", urljoin(url, href))

    markup = lxml_html.tostring(el, encoding="unicode", method="html", with_tail=False)
    text = (_MD_LINKS if links else _MD_PLAIN).convert(markup)
    text = _tidy(_TOGGLE_LINE.sub("", text))
    for token, rendered in blocks.items():
        text = text.replace(token, rendered)
    return _BLANKS.sub("\n\n", text).strip()


def _tidy(text: str) -> str:
    """``tidy`` per line, keeping the 2+ space indents markdownify uses for nested lists."""
    text = _NESTED_BOLD.sub(r"**\1**", text)
    lines = []
    for line in text.splitlines():
        body = line.lstrip(" ")
        indent = len(line) - len(body)
        body = tidy(body)
        lines.append((" " * indent if indent >= 2 and body else "") + body)
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _quote(quote: etree._Element) -> str:
    author = oneline(quote.get("data-quote"))
    content = _first(quote, f".//*[{_has('bbCodeBlock-expandContent')}]")
    if content is None:
        content = _first(quote, f".//*[{_has('bbCodeBlock-content')}]")
    if content is None:
        content = quote
        for title in quote.xpath(f".//*[{_has('bbCodeBlock-title')}]"):
            _drop(title)
    for nested in content.xpath(
        f".//blockquote[{_QUOTE}] | .//*[{_has('bbCodeBlock-expandLink')}] | .//img"
    ):
        if nested.getparent() is not None:
            _drop(nested)
    snippet = trunc(_text(content), QUOTE_CHARS + 1)
    if author and snippet:
        return f"> {author}: {snippet}"
    return f"> {author or snippet}" if (author or snippet) else ">"


def _code(block: etree._Element) -> str:
    pre = _first(block, ".//pre")
    source = pre if pre is not None else _first(block, f".//*[{_has('bbCodeBlock-content')}]")
    source = source if source is not None else block
    for br in source.iter("br"):
        br.tail = "\n" + (br.tail or "")
    lines = "".join(source.itertext()).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    code = _BLANKS.sub("\n\n", "\n".join(line.rstrip() for line in lines)).strip("\n")
    lang = re.sub(r"[^\w+#.-]", "", (pre.get("data-lang") if pre is not None else "") or "")
    longest = max((len(run) for run in re.findall(r"`{3,}", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{code}\n{fence}"


def _alt(alt: str | None) -> str:
    """Image alt text worth keeping: not empty, a filename, a URL, or a generic word."""
    alt = oneline(alt)
    if (
        len(alt) < 3
        or alt.lower() in _GENERIC_ALT
        or "://" in alt
        or alt.startswith(("/", "data:"))
        or _FILE_ALT.match(alt)
    ):
        return ""
    return trunc(alt, 120)


__all__ = ["is_thread", "parse_thread"]
