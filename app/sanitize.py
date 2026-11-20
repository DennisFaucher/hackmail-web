"""Sanitizing untrusted email HTML.

Policy mirrors the macOS app: no scripts, forms, styles sheets, or CSS url() loads; remote images only when the user
opts in for that message; cid: images resolve only from the message's own parts; links keep their true destination.
"""
import html
import re
from html.parser import HTMLParser

import nh3

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "code", "col", "colgroup", "dd", "div", "dl", "dt", "em",
    "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "s", "small", "span", "strike",
    "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
ALLOWED_ATTRIBUTES = {
    "*": {"style", "align", "dir", "lang", "title"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "width", "height", "valign", "bgcolor"},
    "th": {"colspan", "rowspan", "width", "height", "valign", "bgcolor"},
    "table": {"width", "border", "cellpadding", "cellspacing", "bgcolor"},
    "font": {"color", "face", "size"},
}
# No background / background-image: both can carry url() loads.
ALLOWED_STYLE_PROPERTIES = {
    "color", "background-color", "font", "font-family", "font-size", "font-style", "font-weight", "line-height",
    "text-align", "text-decoration", "text-indent", "text-transform", "letter-spacing", "white-space", "vertical-align",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left", "padding", "padding-top", "padding-right",
    "padding-bottom", "padding-left", "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-color", "border-style", "border-width", "border-collapse", "border-spacing", "width", "height", "max-width",
    "min-width", "max-height", "min-height", "display", "list-style-type",
}
# "hmcid" is an internal placeholder scheme for cid: images; it is swapped for a data: URI after cleaning.
# "cid" must pass the scheme check to reach attribute_filter, which only honors it on <img src>.
URL_SCHEMES = {"http", "https", "mailto", "tel", "cid", "hmcid"}
MAX_HTML_BYTES = 8 * 1024 * 1024
_DATA_IMAGE = re.compile(r'data:image/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=\s]+', re.I)


def escape_text(text: str) -> str:
    return html.escape(text, quote=True)


class SanitizeResult:
    def __init__(self, html_text: str, blocked_images: int, cids_used: list[str]):
        self.html = html_text
        self.blocked_images = blocked_images
        self.cids_used = cids_used


def sanitize_html(source: str, *, allow_remote_images: bool = False, cid_data: dict[str, str] | None = None,
                  cid_passthrough: set[str] | None = None) -> SanitizeResult:
    """Sanitize `source`. `cid_data` maps lowercase Content-IDs to ready-made data: URIs; Content-IDs in
    `cid_passthrough` (our own outgoing inline images) are kept as cid: references instead."""
    cid_data = cid_data or {}
    cid_passthrough = {c.lower() for c in (cid_passthrough or set())}
    if len(source.encode("utf-8", "ignore")) > MAX_HTML_BYTES:
        # Fail closed: show escaped text rather than parse an enormous document.
        return SanitizeResult(f"<pre>{escape_text(source[:200000])}</pre>", 0, [])
    blocked = 0
    cids: list[str] = []

    def attribute_filter(tag: str, attribute: str, value: str):
        nonlocal blocked
        if tag == "img" and attribute == "src":
            lowered = value.strip().lower()
            if lowered.startswith("cid:"):
                cid = lowered[4:].strip("<>")
                if cid in cid_data or cid in cid_passthrough:
                    cids.append(cid)
                    return f"hmcid:{len(cids) - 1}"
                return None
            if lowered.startswith("data:"):
                return value if _DATA_IMAGE.fullmatch(value.strip()) else None
            if lowered.startswith(("http:", "https:")):
                if allow_remote_images:
                    return value
                blocked += 1
                return None
            return None
        if attribute == "href":
            lowered = value.strip().lower()
            return value if lowered.startswith(("http:", "https:", "mailto:", "tel:")) else None
        return value

    # data: images must survive the URL-scheme check, so they ride through as placeholders too.
    data_images: list[str] = []

    def stash_data(match: re.Match) -> str:
        data_images.append(re.sub(r"\s+", "", match.group(0)))
        return f"hmcid:data{len(data_images) - 1}"

    prepared = re.sub(
        r'(?i)(<img\b[^>]*?\bsrc\s*=\s*["\'])(' + _DATA_IMAGE.pattern + r')',
        lambda m: m.group(1) + stash_data(re.match(_DATA_IMAGE, m.group(2))),
        source,
    )

    def attribute_filter_with_data(tag: str, attribute: str, value: str):
        if tag == "img" and attribute == "src" and value.startswith("hmcid:data"):
            return value
        return attribute_filter(tag, attribute, value)

    cleaned = nh3.clean(
        prepared,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
        clean_content_tags={"script", "style"},
        filter_style_properties=ALLOWED_STYLE_PROPERTIES,
        attribute_filter=attribute_filter_with_data,
    )

    def restore(match: re.Match) -> str:
        token = match.group(1)
        if token.startswith("data"):
            index = int(token[4:])
            return data_images[index] if index < len(data_images) else ""
        index = int(token)
        if index >= len(cids):
            return ""
        return f"cid:{cids[index]}" if cids[index] in cid_passthrough else cid_data.get(cids[index], "")

    cleaned = re.sub(r"hmcid:(data\d+|\d+)", restore, cleaned)
    return SanitizeResult(cleaned, blocked, cids)


class _TextExtractor(HTMLParser):
    BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table", "hr"}

    def __init__(self, limit: int):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.size = 0
        self.limit = limit
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCKS:
            self._add("\n")
        elif tag == "li":
            self._add("- ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCKS:
            self._add("\n")

    def handle_data(self, data):
        if not self.skip:
            self._add(data)

    def _add(self, text: str):
        room = self.limit - self.size
        if room > 0:
            self.parts.append(text[:room])
            self.size += min(len(text), room)


def html_to_text(source: str, limit: int = 1_000_000) -> str:
    """Bounded HTML to plain text for the plain-text alternative of outgoing mail."""
    parser = _TextExtractor(limit)
    try:
        parser.feed(source[: limit * 4])
        parser.close()
    except Exception:
        return source[:limit]
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
