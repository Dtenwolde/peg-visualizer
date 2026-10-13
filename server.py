#!/usr/bin/env python3
"""PEG grammar visualizer for DuckDB's SQL parser.

Web mode (default):   python3 server.py [--port 8765] [--grammar PATH]
CLI mode:             python3 server.py --sql "SELECT 1" [--start-rule Program] [--json] [--tree-only] [--ok-only]

Loads the grammar straight from the .gram file (default: the inlined grammar in src/include/duckdb/parser/peg) and
reloads it whenever the file changes, so grammar edits are reflected on the next parse.
"""

import argparse
import json
import os
import sys
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from peg_grammar import Grammar, GrammarError, rule_source  # noqa: E402
from peg_matcher import MatchOptions, parse_sql, format_trace_text  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
import subprocess  # noqa: E402


def find_duckdb(explicit=None):
    """The duckdb CLI used for cross-checking: --duckdb, $DUCKDB_BINARY, or a build in this repo."""
    candidates = [explicit, os.environ.get("DUCKDB_BINARY")]
    for build in ("reldebug", "release", "relassert", "debug"):
        candidates.append(os.path.join(REPO_ROOT, "build", build, "duckdb"))
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)
    return None


def run_duckdb(binary, sql, timeout=10):
    """Run sql through the real parser (in-memory database, file system access disabled) and classify the result."""
    env = {k: v for k, v in os.environ.items() if k != "AI_AGENT"}
    env["DUCKDB_AGENT_MODE"] = "0"
    base = [binary, "-no-agent", "-json", "-cmd", "SET enable_external_access=false"]
    out = {"available": True, "binary": binary}
    try:
        proc = subprocess.run(base + ["-c", sql], capture_output=True, text=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        out.update({"parsed": None, "error": "duckdb timed out after %ds" % timeout})
        return out
    err = proc.stderr.strip()
    first = err.split("\n", 1)[0] if err else ""
    if proc.returncode == 0:
        out.update({"parsed": True, "error": None})
    elif first.startswith("Parser Error:"):
        out.update({"parsed": False, "error": first[len("Parser Error:"):].strip(), "detail": err})
    else:
        # the statement parsed; it failed later (binder, catalog, execution, ...)
        out.update({"parsed": True, "error": None, "later_error": first})
    # the AST, which json_serialize_sql only produces for SELECT statements
    escaped = sql.replace("'", "''")
    try:
        proc = subprocess.run(base + ["-cmd", "SET VARIABLE q = '%s'" % escaped,
                                      "-c", "SELECT json_serialize_sql(getvariable('q')) AS j"],
                              capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        rows = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else []
        j = rows[0]["j"] if rows else None
        if isinstance(j, str):
            j = json.loads(j)
        if j and not j.get("error"):
            out["ast"] = j.get("statements")
        elif j:
            out["ast_error"] = j.get("error_message")
    except (subprocess.TimeoutExpired, ValueError, KeyError, IndexError):
        pass
    return out

DEFAULT_GRAMMAR = os.path.join(REPO_ROOT, "src", "include", "duckdb", "parser", "peg", "inlined_grammar.gram")


class GrammarCache:
    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.grammar = None
        self.error = None

    def _files(self):
        """Grammar inputs: a single .gram file, or a directory layout like src/parser/peg/grammar (statements/*.gram
        plus keywords/*.list, which inline_grammar.py turns into the *Keyword rules)."""
        if not os.path.isdir(self.path):
            return [self.path]
        dirs = [self.path]
        for sub in ("statements", "keywords"):
            if os.path.isdir(os.path.join(self.path, sub)):
                dirs.append(os.path.join(self.path, sub))
        sibling = os.path.join(os.path.dirname(self.path.rstrip(os.sep)), "keywords")
        if os.path.basename(self.path.rstrip(os.sep)) == "statements" and os.path.isdir(sibling):
            dirs.append(sibling)
        files = []
        for d in dirs:
            for name in sorted(os.listdir(d)):
                if name.endswith(".gram") or name.endswith(".list"):
                    files.append(os.path.join(d, name))
        return files

    def _read(self):
        parts = []
        for path in self._files():
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if path.endswith(".list"):
                # reserved_keyword.list -> ReservedKeyword <- 'ALL' / 'AND' / ...
                stem = os.path.basename(path)[:-len(".list")]
                rule_name = "".join(p.capitalize() for p in stem.split("_"))
                words = ["'%s'" % line.strip() for line in content.splitlines() if line.strip()]
                content = "%s <- %s\n" % (rule_name, " /\n".join(words))
            parts.append(content)
        return "\n".join(parts)

    def _mtime(self):
        files = self._files()
        return max(os.path.getmtime(f) for f in files) if files else 0

    def get(self):
        mtime = self._mtime()
        if self.grammar is None or mtime != self.mtime:
            started = time.time()
            text = self._read()
            try:
                self.grammar = Grammar(text, source=self.path)
                self.error = None
                print("loaded grammar %s: %d rules, %d keywords (%.2fs)" % (
                    self.path, len(self.grammar.rules), len(self.grammar.keyword_flags), time.time() - started),
                      file=sys.stderr)
            except GrammarError as e:
                self.error = str(e)
                if self.grammar is None:
                    raise
                print("grammar reload failed, keeping previous grammar: %s" % e, file=sys.stderr)
            self.mtime = mtime
        return self.grammar


def make_handler(cache, duckdb_binary):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, code, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html")
            if path == "/api/grammar":
                g = cache.get()
                rules = g.rule_names()
                return self._send(200, json.dumps({
                    "source": g.source, "rules": rules, "rule_count": len(g.rules),
                    "keyword_count": len(g.keyword_flags), "reload_error": cache.error,
                    "duckdb": duckdb_binary,
                }))
            if path.startswith("/api/rule/"):
                g = cache.get()
                name = path[len("/api/rule/"):]
                rule = g.get_rule(name)
                if rule is None:
                    return self._send(404, json.dumps({"error": "unknown rule"}))
                return self._send(200, json.dumps({"name": rule.name, "line": rule.line,
                                                   "source": rule_source(g, rule.name)}))
            return self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/api/parse", "/api/duckdb"):
                return self._send(404, json.dumps({"error": "not found"}))
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, json.dumps({"error": "invalid JSON"}))
            if path == "/api/duckdb":
                if not duckdb_binary:
                    return self._send(200, json.dumps({"available": False}))
                return self._send(200, json.dumps(run_duckdb(duckdb_binary, body.get("sql", ""))))
            try:
                g = cache.get()
            except GrammarError as e:
                return self._send(500, json.dumps({"error": "grammar error: %s" % e}))
            options = MatchOptions(
                packrat=bool(body.get("packrat", True)),
                first_set_skip=bool(body.get("first_set_skip", True)),
                start_rule=body.get("start_rule") or "Program",
                max_trace=int(body.get("max_trace", 400000)),
            )
            started = time.time()
            result = parse_sql(g, body.get("sql", ""), options)
            result["elapsed_ms"] = round((time.time() - started) * 1000, 1)
            result["grammar_reload_error"] = cache.error
            return self._send(200, json.dumps(result))

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grammar", default=DEFAULT_GRAMMAR, help="path to a .gram file or a directory of .gram files")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--duckdb", help="duckdb CLI binary for cross-checking (default: $DUCKDB_BINARY or build/*/duckdb)")
    ap.add_argument("--no-duckdb", action="store_true", help="disable the cross-check against the duckdb binary")
    ap.add_argument("--sql", help="parse this SQL on the command line instead of starting the server")
    ap.add_argument("--start-rule", default="Program")
    ap.add_argument("--no-packrat", action="store_true", help="disable packrat memoization")
    ap.add_argument("--no-first-set", action="store_true", help="disable FIRST-set skipping")
    ap.add_argument("--json", action="store_true", help="CLI mode: dump the full result as JSON")
    ap.add_argument("--tree-only", action="store_true", help="CLI mode: only print the parse tree")
    ap.add_argument("--ok-only", action="store_true", help="CLI mode: hide failed attempts in the trace")
    args = ap.parse_args()

    cache = GrammarCache(args.grammar)
    if args.sql is not None:
        g = cache.get()
        options = MatchOptions(packrat=not args.no_packrat, first_set_skip=not args.no_first_set,
                               start_rule=args.start_rule)
        result = parse_sql(g, args.sql, options)
        if args.json:
            print(json.dumps(result, indent=1))
            return
        if "error" in result:
            print(result["error"]["message"])
            exp = result.get("expected")
            if exp:
                print("expected at token %d (%r): %s" % (exp["position"], exp["token"],
                                                       ", ".join(exp["keywords"] + exp["classes"])))
        if not args.tree_only:
            print("=== trace (%d attempts, %d ok, %d failed, %d skipped, %d cache hits) ===" % (
                result["stats"]["attempts"], result["stats"]["ok"], result["stats"]["fail"],
                result["stats"]["skip"], result["stats"]["cache_hits"]))
            print(format_trace_text(result, g, show_failures=not args.ok_only))
        print("=== parse tree ===")
        print(result.get("tree_text") or "(no result)")
        return

    cache.get()
    binary = None if args.no_duckdb else find_duckdb(args.duckdb)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(cache, binary))
    print("PEG visualizer: http://%s:%d/  (grammar: %s)" % (args.host, args.port, args.grammar), file=sys.stderr)
    print("cross-check binary: %s" % (binary or "none found (build duckdb or pass --duckdb)"), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
