"""Entry point used by the static (Pyodide, in-browser) build: one JSON-in / JSON-out function."""

import json
import time

from peg_grammar import Grammar, GrammarError, rule_source
from peg_matcher import MatchOptions, parse_sql

_grammar = None


def load_grammar(text, source="grammar.gram"):
    global _grammar
    _grammar = Grammar(text, source=source)
    return json.dumps({"source": source, "rules": _grammar.rule_names(), "rule_count": len(_grammar.rules),
                       "keyword_count": len(_grammar.keyword_flags), "reload_error": None, "duckdb": None})


def api(kind, payload):
    body = json.loads(payload) if payload else {}
    if kind == "rule":
        rule = _grammar.get_rule(body["name"])
        if rule is None:
            return json.dumps({"error": "unknown rule"})
        return json.dumps({"name": rule.name, "line": rule.line, "source": rule_source(_grammar, rule.name)})
    if kind == "parse":
        options = MatchOptions(packrat=bool(body.get("packrat", True)),
                               first_set_skip=bool(body.get("first_set_skip", True)),
                               start_rule=body.get("start_rule") or "Program",
                               max_trace=int(body.get("max_trace", 400000)))
        started = time.time()
        try:
            result = parse_sql(_grammar, body.get("sql", ""), options)
            result["elapsed_ms"] = round((time.time() - started) * 1000, 1)
        except RecursionError:
            result = {"error": {"message": "query too deeply nested for the in-browser parser", "stage": "setup"},
                      "tokens": []}
        result["grammar_reload_error"] = None
        return json.dumps(result)
    if kind == "duckdb":
        return json.dumps({"available": False})
    return json.dumps({"error": "unknown api %s" % kind})
