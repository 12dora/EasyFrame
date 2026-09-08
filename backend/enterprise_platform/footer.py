"""共享页脚 HTML 白名单清洗、纯文本与标志 data URL 校验。"""

import base64
import binascii
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
MAX_LOGO_DECODED_BYTES = 128 * 1024
_LOGO_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]+={0,2})$")


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
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
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


def clean_plain_text(value: str, *, max_length: int) -> str:
    text = value.strip()
    if "<" in text or ">" in text:
        raise ValueError("不能包含尖括号")
    if len(text) > max_length:
        raise ValueError("文本过长")
    return text


def validate_logo_data_url(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return _decode_logo_data_url(stripped)


def _decode_logo_data_url(value: str) -> str:
    match = _LOGO_DATA_URL.fullmatch(value)
    if match is None:
        raise ValueError("图片格式无效")
    mime, encoded = match.group(1), match.group(2)
    if len(encoded) > MAX_LOGO_DECODED_BYTES * 4 // 3 + 4:
        raise ValueError("图片过大")
    payload = _decode_logo_base64(encoded)
    if len(payload) > MAX_LOGO_DECODED_BYTES:
        raise ValueError("图片过大")
    if _sniff_image_mime(payload) != mime:
        raise ValueError("图片内容与声明类型不符")
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _decode_logo_base64(encoded: str) -> bytes:
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except binascii.Error as exc:
        raise ValueError("图片格式无效") from exc


def _sniff_image_mime(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None
