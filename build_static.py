#!/usr/bin/env python3
"""Build a static copy of the visualizer that runs the parser in the browser with Pyodide (no server needed).

    python3 build_static.py [--out dist] [--grammar PATH]

Upload the output directory to any static host (GitHub Pages, Netlify, an S3 bucket, ...). The page downloads
Pyodide (~10 MB, cached by the browser) from the jsDelivr CDN on first load.
"""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from server import DEFAULT_GRAMMAR, GrammarCache  # noqa: E402

PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.28.2/full/"
MODULES = ["peg_grammar.py", "peg_tokenizer.py", "peg_matcher.py", "peg_static.py"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    ap.add_argument("--grammar", default=DEFAULT_GRAMMAR)
    ap.add_argument("--pyodide", default=PYODIDE)
    ap.add_argument("--label", help="grammar description shown in the page header (default: the grammar file name)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    grammar_text = GrammarCache(args.grammar)._read()
    with open(os.path.join(args.out, "grammar.gram"), "w", encoding="utf-8") as f:
        f.write(grammar_text)
    for m in MODULES:
        shutil.copy(os.path.join(HERE, m), os.path.join(args.out, m))
    with open(os.path.join(HERE, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    config = ('<script>window.PEG_STATIC = {grammar: "grammar.gram", modules: %r, pyodide: %r, '
              'grammar_name: %r};</script>\n<script>' % (MODULES, args.pyodide,
                                                        args.label or os.path.basename(args.grammar)))
    marker = "<script>\n(function () {"
    if marker not in html:
        sys.exit("index.html: cannot find the main script to inject the static configuration")
    html = html.replace(marker, config + "\n(function () {", 1)
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(args.out, ".nojekyll"), "w") as f:
        f.write("")
    print("static site written to %s (%d files); serve it with any static file server, e.g." % (
        args.out, len(os.listdir(args.out))))
    print("    python3 -m http.server --directory %s 8000" % args.out)


if __name__ == "__main__":
    main()
