"""A small Markdown → HTML renderer for notes imported from Notion (notion.py).

Notion's "Markdown & CSV" export uses a narrow, regular subset: headings, paragraphs, bullet /
numbered / to-do lists nested by 4 spaces, quotes, code fences, `---`, pipe tables, and a little
HTML for callouts (<aside>) and toggles (<details><summary>). This covers that subset and nothing
more. Every piece of text is escaped and only the tags written here come out, so a note can't put
script or arbitrary HTML into the page. Links and images go through `link`, which the caller uses
to point attachments and subpages at where the import put them.
"""

import html
import re
from collections.abc import Callable

Link = Callable[[str, bool], str | None]  # (target, is_image) -> URL to use, or None to drop the link

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^([-*+])\s+(.*)$")
_NUMBER = re.compile(r"^(\d+)[.)]\s+(.*)$")
_TODO = re.compile(r"^\[([ xX])\]\s+(.*)$")
_FENCE = re.compile(r"^(```|~~~)\s*([\w+-]*)")
_HR = re.compile(r"^(?:-\s*){3,}$|^(?:\*\s*){3,}$|^(?:_\s*){3,}$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HTML_BLOCK = re.compile(r"^<(/?)(aside|details|summary)\b[^>]*>(.*)$", re.IGNORECASE)
_SAFE_URL = re.compile(r"^(https?:|mailto:|/|#)", re.IGNORECASE)


def _indent(line: str) -> int:
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


def _dedent(lines: list[str], n: int) -> list[str]:
    out = []
    for ln in lines:
        cut, i = 0, 0
        while i < len(ln) and cut < n and ln[i] in " \t":
            cut += 4 if ln[i] == "\t" else 1
            i += 1
        out.append(ln[i:])
    return out


class Renderer:
    def __init__(self, link: Link | None = None):
        self.link = link or (lambda target, image: target)

    # -- inline ---------------------------------------------------------------

    _INLINE = re.compile(
        r"(?P<code>`+)(?P<codetext>.+?)(?P=code)"
        r"|!\[(?P<alt>[^\]]*)\]\((?P<isrc><[^>]*>|[^)\s]*(?:\([^)\s]*\)[^)\s]*)*)(?:\s+\"[^\"]*\")?\)"
        r"|\[(?P<text>(?:[^\[\]]|\[[^\]]*\])*)\]\((?P<href><[^>]*>|[^)\s]*(?:\([^)\s]*\)[^)\s]*)*)(?:\s+\"[^\"]*\")?\)"
        r"|(?P<url>https?://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?])"
        r"|\*\*(?P<b>.+?)\*\*|__(?P<b2>.+?)__"
        r"|~~(?P<s>.+?)~~"
        r"|(?<![\w*])\*(?P<i>[^*\s](?:[^*]*[^*\s])?)\*(?![\w*])"
        r"|(?<![\w_])_(?P<i2>[^_\s](?:[^_]*[^_\s])?)_(?![\w_])"
        r"|(?P<br><br\s*/?>)",
        re.IGNORECASE,
    )

    def _url(self, target: str, image: bool) -> str | None:
        target = target.strip().removeprefix("<").removesuffix(">")
        url = self.link(target, image)
        if not url or not _SAFE_URL.match(url):
            return None
        return url

    def inline(self, text: str) -> str:
        out, pos = [], 0
        for m in self._INLINE.finditer(text):
            out.append(html.escape(text[pos:m.start()]))
            pos = m.end()
            g = m.groupdict()
            if g["code"]:
                out.append(f"<code>{html.escape(g['codetext'].strip())}</code>")
            elif g["isrc"] is not None:
                url = self._url(g["isrc"], True)
                alt = html.escape(g["alt"] or "")
                out.append(f'<img src="{html.escape(url)}" alt="{alt}" loading="lazy">' if url else alt)
            elif g["href"] is not None:
                url = self._url(g["href"], False)
                inner = self.inline(g["text"]) if g["text"] else html.escape(g["href"])
                if not url:
                    out.append(inner)
                elif url.startswith("#"):
                    out.append(f'<a href="{html.escape(url)}" data-page-link>{inner}</a>')
                else:
                    out.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{inner}</a>')
            elif g["url"]:
                url = g["url"]
                out.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(url)}</a>')
            elif g["b"] is not None or g["b2"] is not None:
                out.append(f"<strong>{self.inline(g['b'] or g['b2'])}</strong>")
            elif g["s"] is not None:
                out.append(f"<del>{self.inline(g['s'])}</del>")
            elif g["i"] is not None or g["i2"] is not None:
                out.append(f"<em>{self.inline(g['i'] or g['i2'])}</em>")
            elif g["br"]:
                out.append("<br>")
        out.append(html.escape(text[pos:]))
        return "".join(out)

    # -- blocks ---------------------------------------------------------------

    def render(self, text: str) -> str:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return self._blocks(lines)

    def _blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            s = line.strip()
            if not s:
                i += 1
                continue
            if m := _FENCE.match(s):
                j = i + 1
                while j < len(lines) and not lines[j].strip().startswith(m.group(1)):
                    j += 1
                code = "\n".join(_dedent(lines[i + 1:j], _indent(line)))
                out.append(f"<pre><code>{html.escape(code)}</code></pre>")
                i = j + 1
                continue
            if m := _HTML_BLOCK.match(s):
                i = self._html_block(lines, i, m, out)
                continue
            if m := _HEADING.match(s):
                level = len(m.group(1))
                out.append(f"<h{level}>{self.inline(m.group(2))}</h{level}>")
                i += 1
                continue
            if _HR.match(s):
                out.append("<hr>")
                i += 1
                continue
            if s.startswith(">"):
                j = i
                quoted = []
                while j < len(lines) and lines[j].strip().startswith(">"):
                    quoted.append(re.sub(r"^\s*>\s?", "", lines[j]))
                    j += 1
                out.append(f"<blockquote>{self._blocks(quoted)}</blockquote>")
                i = j
                continue
            if s.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
                i = self._table(lines, i, out)
                continue
            if _BULLET.match(s) or _NUMBER.match(s):
                i = self._list(lines, i, out)
                continue
            # paragraph: up to a blank line or the start of another block
            j = i
            para = []
            while j < len(lines) and lines[j].strip():
                t = lines[j].strip()
                if j > i and (_HEADING.match(t) or _FENCE.match(t) or _HTML_BLOCK.match(t) or t.startswith(">")
                              or _BULLET.match(t) or _NUMBER.match(t) or (t.startswith("|") and j + 1 < len(lines)
                                                                          and _TABLE_SEP.match(lines[j + 1].strip()))):
                    break
                para.append(t)
                j += 1
            out.append("<p>" + "<br>".join(self.inline(p) for p in para) + "</p>")
            i = j
        return "\n".join(out)

    def _html_block(self, lines: list[str], i: int, m: re.Match, out: list[str]) -> int:
        closing, tag, rest = m.group(1), m.group(2).lower(), m.group(3)
        if closing or tag == "summary":  # stray closer, or a <summary> outside <details>
            text = re.sub(r"</?summary[^>]*>", "", lines[i].strip(), flags=re.IGNORECASE)
            text = re.sub(r"</?(aside|details)[^>]*>", "", text, flags=re.IGNORECASE).strip()
            if text:
                out.append(f"<p>{self.inline(text)}</p>")
            return i + 1
        # find the matching closer (these nest: a toggle inside a callout)
        depth, j = 0, i
        body: list[str] = []
        first = rest
        while j < len(lines):
            t = lines[j] if j > i else first
            opens = len(re.findall(rf"<{tag}\b", t, re.IGNORECASE)) if j > i else 0
            closes = len(re.findall(rf"</{tag}>", t, re.IGNORECASE))
            depth += opens
            if closes > depth:
                body.append(re.sub(rf"</{tag}>.*$", "", t, flags=re.IGNORECASE, count=1))
                break
            depth -= closes
            body.append(t)
            j += 1
        if tag == "aside":
            out.append(f'<div class="callout">{self._blocks(_dedent(body, min((_indent(b) for b in body if b.strip()), default=0)))}</div>')
        else:  # details
            joined = "\n".join(body)
            sm = re.search(r"<summary[^>]*>(.*?)</summary>", joined, re.IGNORECASE | re.DOTALL)
            summary = sm.group(1).strip() if sm else ""
            if sm:
                joined = joined[:sm.start()] + joined[sm.end():]
            inner = joined.split("\n")
            inner = _dedent(inner, min((_indent(b) for b in inner if b.strip()), default=0))
            out.append(f"<details><summary>{self.inline(summary)}</summary>{self._blocks(inner)}</details>")
        return j + 1

    def _table(self, lines: list[str], i: int, out: list[str]) -> int:
        def cells(row: str) -> list[str]:
            row = row.strip().removeprefix("|").removesuffix("|")
            return [c.strip() for c in re.split(r"(?<!\\)\|", row)]

        def pipes(text: str) -> int:
            return len(re.findall(r"(?<!\\)\|", text))

        head = cells(lines[i])
        want = len(head) + 1  # pipes in a whole row
        j = i + 2
        rows = []
        while j < len(lines) and lines[j].strip().startswith("|"):
            # Notion writes a cell's line breaks as they are: a row goes on over the lines that
            # don't start a new row, until it has all its cells
            row = [lines[j].strip()]
            j += 1
            while (pipes("\n".join(row)) < want or not row[-1].rstrip().endswith("|")) and j < len(lines) \
                    and lines[j].strip() and not lines[j].strip().startswith("|"):
                row.append(lines[j].strip())
                j += 1
            rows.append(cells("\n".join(row)))
        cell = lambda c: "<br>".join(self.inline(part.strip()) for part in c.split("\n"))
        th = "".join(f"<th>{cell(c)}</th>" for c in head)
        body = "".join("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in r) + "</tr>" for r in rows)
        out.append(f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>')
        return j

    def _list(self, lines: list[str], i: int, out: list[str]) -> int:
        base = _indent(lines[i])
        ordered = bool(_NUMBER.match(lines[i].strip()))
        start = int(_NUMBER.match(lines[i].strip()).group(1)) if ordered else 1
        items: list[tuple[str, list[str]]] = []
        j = i
        while j < len(lines):
            line = lines[j]
            s = line.strip()
            if not s:
                # a blank line ends the list unless more of it (an item or an indented child) follows
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and (_indent(lines[k]) > base or (_indent(lines[k]) == base and self._is_item(lines[k].strip(), ordered))):
                    if items:
                        items[-1][1].append("")
                    j = k if _indent(lines[k]) == base else j + 1
                    continue
                break
            ind = _indent(line)
            if ind == base and self._is_item(s, ordered):
                m = (_NUMBER if ordered else _BULLET).match(s)
                items.append((m.group(2), []))
            elif ind > base and items:
                items[-1][1].append(line)
            else:  # less indented, or a paragraph / another kind of list at this indent: the list is over
                break
            j += 1
        tag = "ol" if ordered else "ul"
        lis = []
        todo_list = False
        for text, children in items:
            child_html = ""
            if any(c.strip() for c in children):
                child_lines = _dedent(children, min((_indent(c) for c in children if c.strip()), default=0))
                child_html = self._blocks(child_lines)
            if not ordered and (t := _TODO.match(text)):
                todo_list = True
                checked = " checked" if t.group(1) in "xX" else ""
                lis.append(f'<li class="todo"><input type="checkbox" disabled{checked}> {self.inline(t.group(2))}{child_html}</li>')
            else:
                lis.append(f"<li>{self.inline(text)}{child_html}</li>")
        attrs = f' start="{start}"' if ordered and start != 1 else ""
        cls = ' class="todos"' if todo_list else ""
        out.append(f"<{tag}{attrs}{cls}>{''.join(lis)}</{tag}>")
        return j

    @staticmethod
    def _is_item(s: str, ordered: bool) -> bool:
        return bool((_NUMBER if ordered else _BULLET).match(s)) and not _HR.match(s)


def render(text: str, link: Link | None = None) -> str:
    return Renderer(link).render(text)
