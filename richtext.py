"""商品详情富文本渲染(仅标准库)。

商品详情有两种写法,自动识别:
- 纯文本:转义后换行变 <br>(与原来完全一致,旧数据不受影响)。
- 含 HTML 标签:按白名单清洗后原样渲染,实现彩色标题、描边框等美化,
  危险标签/属性一律过滤,杜绝存储型 XSS。

只有后台管理员能写商品详情,买家只能看清洗后的结果。
"""

import re
from html import escape
from html.parser import HTMLParser

from markupsafe import Markup

_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")

# 允许的标签。未列出的标签去掉标签留文字;DROP_CONTENT 里的标签连内容一起去掉。
ALLOWED_TAGS = {
    "p", "br", "div", "span", "b", "strong", "i", "em", "u", "s", "del",
    "font", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "blockquote", "pre", "code", "hr", "a", "img", "table", "thead",
    "tbody", "tr", "th", "td", "sub", "sup", "small", "mark", "wbr",
}
DROP_CONTENT_TAGS = {
    "script", "style", "iframe", "object", "embed", "svg", "video", "audio",
    "form", "input", "select", "textarea", "button", "link", "meta", "base",
    "title", "template", "noscript",
}
VOID_TAGS = {"br", "hr", "img", "wbr"}
# 无结束标签的危险标签:只去掉标签本身,不进入跳过模式(否则深度永远回不来)。
VOID_DROP_TAGS = {"input", "link", "meta", "base"}

# 允许的属性(除 style/class 外,按标签限定)。on* 事件属性一律不允许。
GLOBAL_ATTRS = {"style", "class"}
TAG_ATTRS = {
    "a": {"href", "target"},
    "img": {"src", "alt", "width", "height"},
    "font": {"color", "size", "face"},
    "td": {"colspan", "rowspan", "align", "valign"},
    "th": {"colspan", "rowspan", "align", "valign"},
    "ol": {"type", "start"},
    "ul": {"type"},
    "li": {"type", "value"},
}

# style 里允许的 CSS 属性。position/float/z-index 等一律不放行,防遮罩钓鱼。
STYLE_PROPS = {
    "color", "background", "background-color", "font-size", "font-weight",
    "font-style", "text-decoration", "text-align", "line-height", "border",
    "border-top", "border-bottom", "border-left", "border-right", "border-color",
    "border-width", "border-style", "border-radius", "padding", "padding-top",
    "padding-bottom", "padding-left", "padding-right", "margin", "margin-top",
    "margin-bottom", "margin-left", "margin-right", "display", "width",
    "height", "max-width", "max-height", "vertical-align", "letter-spacing",
}
_STYLE_BAD = re.compile(r"expression\s*\(|url\s*\(|javascript\s*:|vbscript\s*:|"
                        r"behavior\s*:|binding\s*:|@import|<!--|<|>|\\", re.I)
_CTRL = re.compile(r"[\x00-\x20]+")


def _clean_url(value, allow_data_img=False):
    """URL 是否安全。去掉控制字符后判断协议:相对地址与 http/https/mailto 放行,
    img 另放行 data:image/png|jpeg|gif|webp。其余一律拒绝。"""
    v = _CTRL.sub("", value).strip().lower()
    if not v:
        return None
    if v.startswith(("http://", "https://", "mailto:")):
        return value.strip()
    if allow_data_img and re.match(r"data:image/(png|jpeg|gif|webp);", v):
        return value.strip()
    if re.match(r"^[a-z][a-z0-9+.-]*:", v):
        return None
    if v.startswith("//"):
        return None
    return value.strip()


def _clean_style(value):
    """过滤 style:只保留白名单属性的声明,值里出现危险构造的整条声明丢掉。"""
    out = []
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop = prop.strip().lower()
        val = val.strip()
        if prop not in STYLE_PROPS or not val or _STYLE_BAD.search(val):
            continue
        out.append("%s:%s" % (prop, val))
    return ";".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open_stack = []
        self.skip_depth = 0

    def _emit_attrs(self, tag, attrs):
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, set())
        seen = set()
        parts = []
        for name, value in attrs:
            name = name.lower()
            if name in seen or name not in allowed or name.startswith("on"):
                continue
            seen.add(name)
            value = value if value is not None else ""
            if name == "style":
                value = _clean_style(value)
                if not value:
                    continue
            elif name == "href":
                value = _clean_url(value)
                if value is None:
                    continue
            elif name == "src":
                value = _clean_url(value, allow_data_img=True)
                if value is None:
                    continue
            elif name == "target":
                if value.strip().lower() != "_blank":
                    continue
            parts.append(' %s="%s"' % (name, escape(value, quote=True)))
        if tag == "a":
            parts.append(' rel="noopener noreferrer"')
        return "".join(parts)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_depth:
            if tag in DROP_CONTENT_TAGS and tag not in VOID_DROP_TAGS:
                self.skip_depth += 1
            return
        if tag in VOID_DROP_TAGS:
            return
        if tag in DROP_CONTENT_TAGS:
            self.skip_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        self.out.append("<%s%s>" % (tag, self._emit_attrs(tag, attrs)))
        if tag not in VOID_TAGS:
            self.open_stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_depth or tag in DROP_CONTENT_TAGS or tag not in ALLOWED_TAGS:
            return
        self.out.append("<%s%s>" % (tag, self._emit_attrs(tag, attrs)))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_depth:
            if tag in DROP_CONTENT_TAGS:
                self.skip_depth -= 1
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self.open_stack:
            while self.open_stack:
                top = self.open_stack.pop()
                self.out.append("</%s>" % top)
                if top == tag:
                    break

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.out.append(escape(data))

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def unknown_decl(self, data):
        pass

    def result(self):
        while self.open_stack:
            self.out.append("</%s>" % self.open_stack.pop())
        return "".join(self.out)


def sanitize_html(value):
    """按白名单清洗 HTML,返回安全的可直接渲染的字符串。"""
    p = _Sanitizer()
    p.feed(value or "")
    p.close()
    return p.result()


def render_description(value):
    """商品详情渲染:含 HTML 标签则清洗后渲染,否则按纯文本转义 + 换行变 <br>。"""
    if not value:
        return Markup("")
    if _TAG_RE.search(value):
        return Markup(sanitize_html(value))
    lines = [escape(line) for line in str(value).split("\n")]
    return Markup("<br>".join(lines))
