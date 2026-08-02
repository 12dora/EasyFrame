"""共享页脚 HTML 白名单清洗。"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

ALLOWED_FOOTER_TAGS = {"a", "br", "span", "strong", "em", "b", "i", "small"}
FOOTER_VOID_TAGS = {"br"}
FOOTER_SKIP_CONTENT_TAGS = {"script", "style", "iframe"}
ALLOWED_LINK_SCHEMES = {"http", "https", "mailto"}
ALLOWED_TARGETS = {"_blank", "_self", "_parent", "_top"}
REL_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class FooterHtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag in FOOTER_SKIP_CONTENT_TAGS:
                self.skip_depth += 1
            return
        if tag in FOOTER_SKIP_CONTENT_TAGS:
            self.skip_depth = 1
            return
        if tag not in ALLOWED_FOOTER_TAGS:
            return
        if tag in FOOTER_VOID_TAGS:
            self.parts.append(f"<{tag}>")
            return
        self.parts.append(f"<{tag}{self._attribute_text(tag, attrs)}>")
        self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in FOOTER_VOID_TAGS:
            self.handle_starttag(tag, attrs)
        elif tag in ALLOWED_FOOTER_TAGS and not self.skip_depth:
            self.parts.append(f"<{tag}{self._attribute_text(tag, attrs)}></{tag}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag in FOOTER_SKIP_CONTENT_TAGS:
                self.skip_depth -= 1
            return
        if tag not in self.open_tags or tag in FOOTER_VOID_TAGS:
            return
        while self.open_tags:
            current = self.open_tags.pop()
            self.parts.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(html.escape(data, quote=False))

    def sanitized(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)

    def _attribute_text(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        if tag != "a":
            return ""
        values: dict[str, str] = {}
        rel_tokens: list[str] = []
        for raw_name, raw_value in attrs:
            name, value = raw_name.lower(), raw_value or ""
            if name == "href" and (href := clean_href(value)) is not None:
                values["href"] = href
            elif name == "title":
                values["title"] = value
            elif name == "target" and value in ALLOWED_TARGETS:
                values["target"] = value
            elif name == "rel":
                rel_tokens.extend(clean_rel_tokens(value))
        for token in ("noopener", "noreferrer"):
            if token not in rel_tokens:
                rel_tokens.append(token)
        values["rel"] = " ".join(rel_tokens)
        return "".join(
            f' {name}="{html.escape(values[name], quote=True)}"'
            for name in ("href", "title", "target", "rel")
            if name in values
        )


def clean_href(value: str) -> str | None:
    href = value.strip()
    if not href or any(ord(char) < 32 or ord(char) == 127 for char in href):
        return None
    parsed = urlsplit(href)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_LINK_SCHEMES:
        return None
    if scheme in {"http", "https"} and not parsed.netloc:
        return None
    if scheme == "mailto" and not parsed.path:
        return None
    return href


def clean_rel_tokens(value: str) -> list[str]:
    result: list[str] = []
    for raw_token in value.split():
        token = raw_token.lower()
        if token != "opener" and token not in result and REL_TOKEN_PATTERN.fullmatch(token):
            result.append(token)
    return result


def sanitize_footer_html(value: str) -> str:
    parser = FooterHtmlSanitizer()
    parser.feed(value)
    parser.close()
    return parser.sanitized()
