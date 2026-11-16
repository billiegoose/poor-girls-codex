"""Convert ChatGPT's rendered assistant message HTML to portable Markdown.

Only the message's Markdown content containers should be passed here, never
an entire chat turn with its action buttons and other UI chrome.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Element | str] = field(default_factory=list)


_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'wbr'}
_SKIP = {'script', 'style', 'button', 'svg'}
_BLOCK = {'p', 'pre', 'blockquote', 'ul', 'ol', 'table', 'hr', 'div', 'section', 'article', 'details', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element('root')
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Element(tag, {key: value or '' for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _text(node: _Element | str) -> str:
    if isinstance(node, str):
        return node
    if node.tag in _SKIP:
        return ''
    if node.tag == 'br':
        return '\n'
    return ''.join(_text(child) for child in node.children)


def _inline(node: _Element | str) -> str:
    if isinstance(node, str):
        return node.replace('\u00a0', ' ')
    tag = node.tag
    if tag in _SKIP:
        return ''
    if tag == 'br':
        return '  \n'
    if tag == 'img':
        alt = node.attrs.get('alt', '')
        src = node.attrs.get('src', '')
        return f'![{alt}]({src})' if src else alt
    content = ''.join(_inline(child) for child in node.children)
    if tag in {'strong', 'b'}:
        return f'**{content.strip()}**' if content.strip() else ''
    if tag in {'em', 'i'}:
        return f'*{content.strip()}*' if content.strip() else ''
    if tag in {'s', 'strike', 'del'}:
        return f'~~{content.strip()}~~' if content.strip() else ''
    if tag == 'code':
        value = _text(node)
        fence = '`' * (max((len(match.group()) for match in re.finditer(r'`+', value)), default=0) + 1)
        spacer = ' ' if value.startswith(('`', ' ')) or value.endswith(('`', ' ')) else ''
        return f'{fence}{spacer}{value}{spacer}{fence}'
    if tag == 'a':
        href = node.attrs.get('href', '')
        return f'[{content}]({href.replace(")", "%29")})' if href else content
    return content


def _code(node: _Element) -> str:
    code = next((child for child in node.children if isinstance(child, _Element) and child.tag == 'code'), None)
    source = code or node
    content = _text(source).rstrip('\n')
    classes = source.attrs.get('class', '').split()
    language = next((value.removeprefix('language-') for value in classes if value.startswith('language-')), '')
    language = language or source.attrs.get('data-language', '') or node.attrs.get('data-language', '')
    language = re.sub(r'[^\w+.-]', '', language)
    fence = '`' * max(3, max((len(match.group()) for match in re.finditer(r'`+', content)), default=0) + 1)
    return f'{fence}{language}\n{content}\n{fence}'


def _list(node: _Element, depth: int = 0) -> str:
    lines: list[str] = []
    start = node.attrs.get('start', '1')
    try:
        index = int(start)
    except ValueError:
        index = 1
    for item in node.children:
        if not isinstance(item, _Element) or item.tag != 'li':
            continue
        marker = f'{index}. ' if node.tag == 'ol' else '- '
        index += 1
        nested = [child for child in item.children if isinstance(child, _Element) and child.tag in {'ol', 'ul'}]
        direct = [_inline(child) for child in item.children if child not in nested]
        first = ' '.join(''.join(direct).split())
        lines.append('  ' * depth + marker + first)
        for child in nested:
            lines.append(_list(child, depth + 1))
    return '\n'.join(lines)


def _table(node: _Element) -> str:
    rows: list[list[str]] = []
    def collect(element: _Element) -> None:
        if element.tag == 'tr':
            cells = [child for child in element.children if isinstance(child, _Element) and child.tag in {'td', 'th'}]
            rows.append([_inline(cell).strip().replace('|', r'\|').replace('\n', ' ') for cell in cells])
        else:
            for child in element.children:
                if isinstance(child, _Element):
                    collect(child)
    collect(node)
    if not rows:
        return ''
    width = max(map(len, rows))
    row = lambda cells: '| ' + ' | '.join(cells + [''] * (width - len(cells))) + ' |'
    return '\n'.join([row(rows[0]), row(['---'] * width), *(row(cells) for cells in rows[1:])])


def _blocks(node: _Element) -> list[str]:
    results: list[str] = []
    inline: list[str] = []
    def flush() -> None:
        value = ''.join(inline).strip()
        if value:
            results.append(value)
        inline.clear()
    for child in node.children:
        if isinstance(child, str):
            inline.append(_inline(child))
            continue
        if child.tag in _SKIP:
            continue
        tag = child.tag
        if tag not in _BLOCK:
            inline.append(_inline(child))
            continue
        flush()
        if tag in {'div', 'section', 'article', 'details'}:
            results.extend(_blocks(child))
        elif tag in {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            value = _inline(child).strip()
            if value:
                results.append(('#' * int(tag[1]) + ' ' if tag.startswith('h') else '') + value)
        elif tag == 'pre':
            results.append(_code(child))
        elif tag in {'ol', 'ul'}:
            value = _list(child)
            if value:
                results.append(value)
        elif tag == 'table':
            value = _table(child)
            if value:
                results.append(value)
        elif tag == 'hr':
            results.append('---')
        elif tag == 'blockquote':
            value = '\n\n'.join(_blocks(child)) or _inline(child).strip()
            results.append('\n'.join('> ' + line if line else '>' for line in value.splitlines()))
    flush()
    return results


def html_to_markdown(source: str) -> str:
    """Best-effort Markdown for a rendered assistant body; no external packages."""
    parser = _Tree()
    parser.feed(source)
    parser.close()
    return '\n\n'.join(_blocks(parser.root)).strip()
