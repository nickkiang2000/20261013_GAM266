"""Export an official public PTT article as an offline color reference."""

import argparse
from collections import Counter
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.request import HTTPRedirectHandler, Request, build_opener

from archive import write_page
from ansi_style import ANSI_STYLE, COLOR_CLASS, render_run


SAMPLE_URL = 'https://www.ptt.cc/bbs/NctuNthuTYSH/M.1251990698.A.3CB.html'
ARTICLE_URL = re.compile(r'https://www\.ptt\.cc/bbs/[a-zA-Z0-9_.-]+/M\.\d+\.A(?:\.[0-9A-F]+)?\.html')

REFERENCE_STYLE = '''
html { color-scheme: dark; background: #000; }
body { margin: 0; color: #999; }
main { max-width: 960px; margin: auto; }
.masthead { background: #0c0a66; color: #fff; box-sizing: border-box;
            min-height: 38px; display: flex; align-items: baseline; gap: 1em; }
.masthead a { color: inherit; text-decoration: none; }
.masthead .brand { color: #ff6; }
.masthead .source { margin-left: auto; padding-right: .5em; color: #aaa; }
.masthead, .article-meta, '''


class PublicArticle(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = 'PTT 彩色文章'
        self.runs = []
        self.depth = 0
        self.complete = False
        self.spans = []
        self.ignored = None
        self.metadata = {}
        self.meta_depth = 0
        self.meta_field = None
        self.meta_parts = {'article-meta-tag': '', 'article-meta-value': ''}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.ignored:
            return
        if tag == 'meta' and attrs.get('property') == 'og:title':
            self.title = attrs.get('content') or self.title
        if not self.depth:
            if tag == 'div' and attrs.get('id') == 'main-content' and not self.complete:
                self.depth = 1
            return
        if tag in {'script', 'style', 'iframe', 'template'}:
            self.ignored = tag
        elif tag == 'div':
            self.depth += 1
            if self.depth == 2 and set((attrs.get('class') or '').split()) & {'article-metaline', 'article-metaline-right'}:
                self.meta_depth = self.depth
                self.meta_parts = dict.fromkeys(self.meta_parts, '')
        elif tag == 'span':
            if self.meta_depth:
                self.meta_field = next((token for token in (attrs.get('class') or '').split()
                                        if token in self.meta_parts), None)
                return
            self.spans.append(tuple(token for token in (attrs.get('class') or '').split()
                                    if COLOR_CLASS.fullmatch(token)))
        elif tag == 'br':
            self.handle_data('\n')

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored:
                self.ignored = None
            return
        if not self.depth:
            return
        if tag == 'span':
            if self.meta_depth:
                self.meta_field = None
            elif self.spans:
                self.spans.pop()
        elif tag == 'div':
            if self.depth == self.meta_depth:
                self.metadata[self.meta_parts['article-meta-tag'].strip()] = self.meta_parts['article-meta-value']
                self.meta_depth = 0
                self.meta_field = None
                self.depth -= 1
                return
            self.depth -= 1
            if self.depth == 0:
                self.complete = True
            elif self.runs and not self.runs[-1][0].endswith('\n'):
                self.handle_data('\n')

    def handle_data(self, text):
        if not self.depth or self.ignored:
            return
        if self.meta_depth:
            if self.meta_field:
                self.meta_parts[self.meta_field] += text
            return
        styles = {}
        for span in self.spans:
            styles.update((token[0], token) for token in span)
        self.runs.append((text, tuple(styles.values())))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_public_article(url):
    if not ARTICLE_URL.fullmatch(url):
        raise ValueError('請提供 www.ptt.cc 的公開文章網址')
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    limit = 2_000_000
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('文章頁面超過大小限制')
    article = PublicArticle()
    article.feed(data.decode('utf-8'))
    article.close()
    if not article.complete or not article.runs:
        raise ValueError('頁面沒有可讀取的文章正文')
    return article


def export_reference(article, url, output):
    runs = ''.join(render_run(text, classes) for text, classes in article.runs)
    board = article.metadata.get('看板', '')
    board_label = f'<span>› 看板 {escape(board)}</span>' if board else ''
    rows = []
    for field in ('作者', '標題', '時間'):
        value = article.metadata.get(field, article.title if field == '標題' else '')
        if value:
            if field == '作者' and board:
                rows.append(f'<div><dt>{field}</dt><dd>{escape(value)}</dd>'
                            f'<dt class="board">看板</dt><dd>{escape(board)}</dd></div>')
            else:
                rows.append(f'<div><dt>{field}</dt><dd>{escape(value)}</dd></div>')
    if board and '作者' not in article.metadata:
        rows.append(f'<div><dt>看板</dt><dd>{escape(board)}</dd></div>')
    html = f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(article.title)}</title><style>{REFERENCE_STYLE}{ANSI_STYLE}</style></head>
<body><main><header class="masthead"><span class="brand">批踢踢實業坊</span>{board_label}
<a class="source" href="{escape(url, quote=True)}">來源文章</a></header>
<dl class="article-meta">{"".join(rows)}</dl><pre class="color-reference">
{runs}</pre>
</main></body></html>'''
    write_page(output, html)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--url', default=SAMPLE_URL)
    args = parser.parse_args()
    article = download_public_article(args.url)
    export_reference(article, args.url, args.output)
    styles = Counter(token for _, classes in article.runs for token in classes)
    print(f'Saved {args.output}. Color class counts: {dict(sorted(styles.items()))}')


if __name__ == '__main__':
    main()
