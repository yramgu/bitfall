#!/usr/bin/env python3
"""Render README.md to a single self-contained README.html.

    "D:/12_ScriptsPython/4_tests/_ENV_/venv-tests/python.exe" docs/render_html.py

Images are inlined as data URIs, so the result is one file you can mail or drop
on a share without dragging docs/ along with it.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import sys
from pathlib import Path

import markdown

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1c1e21;
  --muted: #5c6370;
  --rule: #d8dde3;
  --code-bg: #f4f6f8;
  --accent: #0b6bcb;
  --note-bg: #f7f9fb;
  --warn-bg: #fff6e0;
  --warn-edge: #b4690e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a;
    --fg: #e4e6eb;
    --muted: #9aa4b2;
    --rule: #2c3238;
    --code-bg: #1d2126;
    --accent: #6cb6ff;
    --note-bg: #1a1e23;
    --warn-bg: #2a2114;
    --warn-edge: #e0a33a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  padding: 3rem 1.5rem 6rem;
  max-width: 62rem;
  background: var(--bg);
  color: var(--fg);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
h1, h2, h3 { line-height: 1.25; font-weight: 650; }
h1 { font-size: 2.1rem; margin: 0 0 .4rem; letter-spacing: -.02em; }
h2 {
  font-size: 1.45rem; margin: 3rem 0 1rem;
  padding-bottom: .4rem; border-bottom: 1px solid var(--rule);
}
h3 { font-size: 1.12rem; margin: 2rem 0 .6rem; color: var(--fg); }
p { margin: 0 0 1rem; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2.5rem 0; }
code {
  font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas,
               "Liberation Mono", monospace;
  font-size: .89em;
  background: var(--code-bg);
  padding: .15em .38em;
  border-radius: 4px;
}
pre {
  background: var(--code-bg);
  border: 1px solid var(--rule);
  border-radius: 7px;
  padding: .9rem 1.1rem;
  overflow-x: auto;
  line-height: 1.5;
}
pre code { background: none; padding: 0; font-size: .85rem; }
table {
  border-collapse: collapse;
  width: 100%;
  margin: 0 0 1.4rem;
  font-size: .93rem;
  display: block;
  overflow-x: auto;
}
th, td {
  border: 1px solid var(--rule);
  padding: .48rem .7rem;
  text-align: left;
  vertical-align: top;
}
th { background: var(--note-bg); font-weight: 620; white-space: nowrap; }
tbody tr:nth-child(even) { background: color-mix(in srgb, var(--note-bg) 55%, transparent); }
td code, th code { white-space: nowrap; }
img {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 1.4rem auto 1.8rem;
  border: 1px solid var(--rule);
  border-radius: 7px;
  background: #fff;
}
blockquote {
  margin: 0 0 1rem;
  padding: .1rem 1rem;
  border-left: 3px solid var(--rule);
  color: var(--muted);
}
blockquote.disclaimer {
  margin: 1.5rem 0 2.5rem;
  padding: 1.4rem 1.6rem;
  border: 2px solid var(--warn-edge);
  border-left: 8px solid var(--warn-edge);
  border-radius: 8px;
  background: var(--warn-bg);
  color: var(--fg);
}
blockquote.disclaimer h1 {
  margin: 0 0 .7rem;
  font-size: 1.85rem;
  letter-spacing: .01em;
  color: var(--warn-edge);
}
blockquote.disclaimer p { margin: 0; font-size: .97rem; }
ul, ol { margin: 0 0 1rem; padding-left: 1.4rem; }
li { margin: .25rem 0; }
strong { font-weight: 640; }
.footer {
  margin-top: 4rem; padding-top: 1rem;
  border-top: 1px solid var(--rule);
  color: var(--muted); font-size: .85rem;
}
@media print {
  body { max-width: none; padding: 0; }
  pre, img, table { break-inside: avoid; }
}
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{body}
<p class="footer">Rendered from README.md by docs/render_html.py. Figures are
embedded; regenerate them with <code>python docs/make_figures.py</code>.</p>
</body>
</html>
"""


def inline_images(html: str, base: Path) -> str:
    """Replace every local <img src> with a data URI."""

    def repl(m):
        src = m.group(1)
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        path = (base / src).resolve()
        if not path.is_file():
            print(f"  warning: missing image {src}", file=sys.stderr)
            return m.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        print(f"  embedded {src} ({path.stat().st_size / 1e3:,.0f} kB)")
        return f'src="data:{mime};base64,{data}"'

    return re.sub(r'src="([^"]+)"', repl, html)


def main() -> int:
    src = ROOT / "README.md"
    dst = ROOT / "README.html"
    text = src.read_text(encoding="utf-8")

    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "toc"],
        output_format="html5",
    )
    body = inline_images(body, ROOT)
    # The README opens with a blockquote disclaimer; promote it to a banner.
    body = body.replace("<blockquote>", '<blockquote class="disclaimer">', 1)

    title = next((ln.lstrip("# ").strip() for ln in text.splitlines()
                  if ln.startswith("# ")), "README")
    dst.write_text(PAGE.format(title=title, css=CSS, body=body), encoding="utf-8")
    print(f"wrote {dst}  ({dst.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
