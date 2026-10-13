"""Tracing PEG matcher that mirrors DuckDB's MatchStack / MatchProcess semantics (matcher_stack.cpp,
matcher_process.cpp) and records every matcher attempt."""

import re
import sys
import threading

from peg_grammar import Grammar, GrammarError
from peg_tokenizer import Tokenizer, TokenizeError, END_OF_INPUT, STRING_LITERAL, is_initial_number, \
    is_number_char, is_scientific, is_keyword_char, is_operator_char, has_special_precedence

# trace statuses
OK = "ok"
FAIL = "fail"
SKIP = "skip"  # FIRST-set check: certainly cannot match at this token, not attempted
SKIP_OPT = "skip_opt"  # optional whose element cannot match: resolved to "empty" without attempting
CACHE_OK = "cache_ok"
CACHE_FAIL = "cache_fail"
NO_DISPATCH = "no_dispatch"  # literal choice: no alternative starts with the current token

_DOLLAR_QUOTE = re.compile(r"^\$([A-Za-z_\x80-￿][A-Za-z0-9_\x80-￿]*)?\$")


class ParseNode:
    __slots__ = ("type", "name", "offset", "length", "value", "selected", "children", "has_rule", "collapsed", "extra")

    def __init__(self, type_, name=None, offset=None, length=None, value=None, selected=None, children=None,
                 has_rule=False):
        self.type = type_
        self.name = name
        self.offset = offset
        self.length = length
        self.value = value
        self.selected = selected
        self.children = children if children is not None else []
        self.has_rule = has_rule
        self.collapsed = False
        self.extra = None

    def enclose(self, child):
        if self.offset is None or child.offset is None:
            return
        child_end = child.offset + (child.length or 0)
        own_end = self.offset + (self.length or 0)
        start = min(self.offset, child.offset)
        self.length = max(child_end, own_end) - start
        self.offset = start

    def to_json(self):
        d = {"t": self.type}
        if self.name:
            d["n"] = self.name
        if self.offset is not None:
            d["o"] = self.offset
            d["l"] = self.length if self.length is not None else 0
        if self.value is not None:
            d["v"] = self.value
        if self.selected is not None:
            d["i"] = self.selected
        if self.collapsed:
            d["col"] = True
        if self.extra:
            d["x"] = self.extra
        if self.type in ("LIST", "REPEAT"):
            d["c"] = [c.to_json() for c in self.children]
        elif self.type in ("CHOICE", "OPTIONAL") and self.children:
            d["c"] = [self.children[0].to_json()]
        return d

    def to_text(self, indent="", is_last=True, visited=None):
        """DuckDB ParseResult::ToString() look-alike."""
        visited = visited if visited is not None else set()
        branch = "└─" if is_last else "├─"
        child_indent = indent + ("   " if is_last else "│  ")
        t = self.type
        if t == "OPTIONAL":
            if self.children:
                return self.children[0].to_text(indent, is_last, visited)
            return "%s%s OPTIONAL [empty]\n" % (indent, branch)
        if t == "CHOICE":
            out = "%s%s [CHOICE (idx: %d)] ->\n" % (indent, branch, self.selected)
            return out + self.children[0].to_text(child_indent, True, visited)
        if t in ("LIST", "REPEAT"):
            name = " (%s)" % self.name if self.name else ""
            out = "%s%s %s%s [%d children]\n" % (indent, branch, t, name, len(self.children))
            for i, c in enumerate(self.children):
                out += c.to_text(child_indent, i == len(self.children) - 1, visited)
            return out
        name = " (%s)" % self.name if self.name else ""
        val = ""
        if self.value is not None:
            val = ': "%s"' % self.value if t == "KEYWORD" else ": %s" % self.value
        return "%s%s %s%s%s\n" % (indent, branch, t, name, val)


class TraceNode:
    __slots__ = ("id", "parent", "matcher", "alt", "start", "end", "status", "depth", "note")

    def __init__(self, id_, parent, matcher, alt, start, depth):
        self.id = id_
        self.parent = parent
        self.matcher = matcher
        self.alt = alt
        self.start = start
        self.end = None
        self.status = None
        self.depth = depth
        self.note = None

    def to_json(self):
        row = [self.id, self.parent, self.matcher.id, self.alt, self.start, self.end, self.status, self.depth]
        if self.note:
            row.append(self.note)
        return row


class MatchOptions:
    def __init__(self, packrat=True, first_set_skip=True, start_rule="Program", max_trace=400000):
        self.packrat = packrat
        self.first_set_skip = first_set_skip
        self.start_rule = start_rule
        self.max_trace = max_trace


class Tracer:
    def __init__(self, grammar, tokens, options):
        self.grammar = grammar
        self.tokens = tokens
        self.opt = options
        self.trace = []
        self.cache = {}
        self.max_token = 0
        self.truncated = False
        self.stats = {"attempts": 0, "ok": 0, "fail": 0, "skip": 0, "cache_hits": 0, "no_dispatch": 0}
        self.used_matchers = {}

    # -- helpers ----------------------------------------------------------------
    def token(self, pos):
        return self.tokens[pos] if pos < len(self.tokens) else None

    def _node(self, matcher, pos, parent, alt, depth):
        self.stats["attempts"] += 1
        self.used_matchers[matcher.id] = matcher
        if len(self.trace) >= self.opt.max_trace:
            self.truncated = True
            return TraceNode(-1, parent, matcher, alt, pos, depth)
        node = TraceNode(len(self.trace), parent, matcher, alt, pos, depth)
        self.trace.append(node)
        return node

    def _finish(self, node, success, end, status=None):
        node.status = status or (OK if success else FAIL)
        node.end = end if success else None
        if node.status in (OK, CACHE_OK, SKIP_OPT):
            self.stats["ok"] += 1
        elif node.status in (FAIL, CACHE_FAIL, NO_DISPATCH, SKIP):
            self.stats["fail"] += 1
        if node.status == SKIP or node.status == SKIP_OPT:
            self.stats["skip"] += 1
        if node.status in (CACHE_OK, CACHE_FAIL):
            self.stats["cache_hits"] += 1
        if node.status == NO_DISPATCH:
            self.stats["no_dispatch"] += 1

    def advance(self, pos):
        pos += 1
        if pos > self.max_token:
            self.max_token = pos
        return pos

    # -- entry ------------------------------------------------------------------
    def run(self, matcher, pos):
        return self.match(matcher, pos, -1, None, 0)

    def child(self, matcher, pos, parent_node, alt):
        """A composite matcher asks for a child: mirrors MatchStack::ExecuteFrame's FIRST-set shortcuts."""
        depth = parent_node.depth + 1
        if not matcher.atomic and self.opt.first_set_skip:
            tok = self.token(pos)
            if matcher.kind == "OPTIONAL" and matcher.structural and not matcher.child.first_set.might_match(tok):
                node = self._node(matcher, pos, parent_node.id, alt, depth)
                node.note = "element cannot start with this token"
                self._finish(node, True, pos, SKIP_OPT)
                return True, pos, ParseNode("OPTIONAL")
            if not matcher.first_set.might_match(tok):
                node = self._node(matcher, pos, parent_node.id, alt, depth)
                node.note = "cannot start with this token (FIRST set)"
                self._finish(node, False, pos, SKIP)
                return False, pos, None
        return self.match(matcher, pos, parent_node.id, alt, depth)

    def match(self, m, pos, parent, alt, depth):
        node = self._node(m, pos, parent, alt, depth)
        use_cache = self.opt.packrat and m.memoized
        if use_cache:
            key = (m.id, pos)
            hit = self.cache.get(key)
            if hit is not None:
                success, end, result, max_seen = hit
                self.max_token = max(self.max_token, max_seen)
                self._finish(node, success, end, CACHE_OK if success else CACHE_FAIL)
                return success, end, result
            max_before = self.max_token
        success, end, result, status = self._match_internal(m, pos, node)
        if use_cache:
            self.cache[key] = (success, end if success else pos, result, max(max_before, self.max_token))
        self._finish(node, success, end, status)
        return success, end, result

    # -- matcher semantics --------------------------------------------------------
    def _match_internal(self, m, pos, node):
        kind = m.kind
        if kind == "LIST":
            return self._match_list(m, pos, node)
        if kind == "CHOICE":
            return self._match_choice(m, pos, node)
        if kind == "OPTIONAL":
            ok, end, res = self.child(m.child, pos, node, 0)
            if not ok:
                return True, pos, ParseNode("OPTIONAL"), None
            wrapper = ParseNode("OPTIONAL", name=res.name if res else None, offset=self._offset(pos))
            if res is not None:
                wrapper.children = [res]
                wrapper.enclose(res)
            return True, end, wrapper, None
        if kind == "REPEAT":
            results = []
            cur = pos
            matched_once = False
            while True:
                ok, end, res = self.child(m.child, cur, node, len(results))
                if not ok:
                    break
                matched_once = True
                if res is not None:
                    results.append(res)
                if end == cur:
                    node.note = "element matched without consuming input; repeat stopped"
                    break
                cur = end
            if not matched_once:
                return False, pos, None, None
            rep = ParseNode("REPEAT", offset=self._offset(pos), children=results)
            for r in results:
                rep.enclose(r)
            return True, cur, rep, None
        return self._match_atomic(m, pos, node)

    def _offset(self, pos):
        tok = self.token(pos)
        return tok.offset if tok is not None else None

    def _match_list(self, m, pos, node):
        cur = pos
        results = []
        for i, ch in enumerate(m.children):
            ok, end, res = self.child(ch, cur, node, i)
            if not ok:
                return False, pos, None, None
            if res is not None:
                results.append(res)
            cur = end
        if m.collapsible:
            collapsible = None
            for r in results:
                if r.type == "OPTIONAL" and not r.children:
                    continue
                if collapsible is not None:
                    collapsible = None
                    break
                collapsible = r
            else:
                if collapsible is not None and collapsible.has_rule:
                    collapsible.collapsed = True
                    return True, cur, collapsible, None
        lst = ParseNode("LIST", name=m.name, offset=self._offset(pos), children=results, has_rule=m.name is not None)
        for r in results:
            lst.enclose(r)
        return True, cur, lst, None

    def _match_choice(self, m, pos, node):
        if m.literal_dispatch is not None:
            tok = self.token(pos)
            idx = m.literal_dispatch.get(tok.text.lower()) if tok is not None else None
            if idx is None:
                node.note = "no alternative starts with %s" % (
                    ("'%s'" % tok.text) if tok is not None and tok.text else "end of input")
                return False, pos, None, NO_DISPATCH
            indices = [idx]
        else:
            indices = range(len(m.children))
        for i in indices:
            ok, end, res = self.child(m.children[i], pos, node, i)
            if ok:
                choice = ParseNode("CHOICE", name=res.name if res else None, offset=self._offset(pos), selected=i)
                if res is not None:
                    choice.children = [res]
                    choice.enclose(res)
                return True, end, choice, None
        return False, pos, None, None

    def _match_atomic(self, m, pos, node):
        tok = self.token(pos)
        if tok is None:
            node.note = "no more tokens"
            return False, pos, None, None
        kind = m.kind
        named = m.name is not None
        if kind == "KEYWORD":
            if tok.text.lower() != m.lower:
                return False, pos, None, None
            end = self.advance(pos)
            return True, end, ParseNode("KEYWORD", name=m.name, offset=tok.offset, length=tok.length, value=tok.text,
                                        has_rule=named), None
        if kind == "IDENTIFIER":
            if not self._is_identifier(m, tok):
                return False, pos, None, None
            if not m.reserved and self.grammar.is_keyword(tok.text) and \
                    not (self.grammar.keyword_flags.get(tok.text.lower(), 0) & m.mask):
                cats = ", ".join(self.grammar.keyword_categories(tok.text))
                node.note = "'%s' is a %s keyword, not allowed here" % (tok.text, cats)
                return False, pos, None, None
            end = self.advance(pos)
            value = tok.text
            if len(value) >= 2 and value[0] in '"`' and value[-1] == value[0]:
                value = value[1:-1].replace(value[0] * 2, value[0])
            elif len(value) >= 2 and value[0] == "'" and value[-1] == "'" and m.supports_string_literal():
                value = value[1:-1].replace("''", "'")
            return True, end, ParseNode("IDENTIFIER", name=m.name, offset=tok.offset, length=tok.length, value=value,
                                        has_rule=named), None
        if kind == "NUMBER_LITERAL":
            if not self._is_number(tok.text):
                return False, pos, None, None
            end = self.advance(pos)
            return True, end, ParseNode("NUMBER", name=m.name, offset=tok.offset, length=tok.length, value=tok.text,
                                        has_rule=named), None
        if kind == "STRING_LITERAL":
            return self._match_string(m, pos, tok, node)
        if kind == "OPERATOR":
            if not m.all_operators and has_special_precedence(tok.text):
                node.note = "'%s' has its own precedence level in the grammar" % tok.text
                return False, pos, None, None
            if any(not is_operator_char(c) for c in tok.text):
                return False, pos, None, None
            end = self.advance(pos)
            return True, end, ParseNode("OPERATOR", name=m.name, offset=tok.offset, length=tok.length, value=tok.text,
                                        has_rule=named), None
        if kind == "END_OF_INPUT":
            if tok.type != END_OF_INPUT:
                return False, pos, None, None
            end = self.advance(pos)
            return True, end, ParseNode("END_OF_INPUT", name=m.name, has_rule=named), None
        raise RuntimeError("unknown atomic matcher %s" % kind)

    # -- terminal helpers (identifier_matcher.hpp, number_literal_matcher.hpp, string_literal_matcher.hpp) -------
    @staticmethod
    def _is_identifier(m, tok):
        text = tok.text
        if not text:
            return False
        if text[0] == "'" and text[-1] == "'" and m.supports_string_literal():
            return True
        if len(text) >= 2 and text[0] in '"`' and text[0] == text[-1]:
            return True
        if is_initial_number(text[0]):
            return False
        return is_keyword_char(text[0])

    @staticmethod
    def _is_number(text):
        if not text or not is_initial_number(text[0]):
            return False
        if text == ".":
            return False
        scientific = False
        for c in text[1:]:
            if is_scientific(c):
                if scientific:
                    return False
                scientific = True
            if scientific and c in "+-":
                continue
            if not is_number_char(c):
                return False
        return True

    @staticmethod
    def _string_prefix(text):
        if not text:
            return 0
        if text[0] == "'":
            return 1
        if len(text) > 1 and text[1] == "'" and text[0] in "NnXxBbEe":
            return 2
        return 0

    @classmethod
    def _is_single_quoted(cls, text):
        prefix = cls._string_prefix(text)
        if prefix == 0 or len(text) < prefix + 1:
            return False
        return text[prefix - 1] == "'" and text[-1] == "'"

    @staticmethod
    def _dollar_delimiter(text):
        mt = _DOLLAR_QUOTE.match(text)
        if not mt:
            return None
        delim = mt.group(0)
        if len(text) >= 2 * len(delim) and text.endswith(delim):
            return delim
        return None

    def _match_string(self, m, pos, tok, node):
        text = tok.text
        single = self._is_single_quoted(text)
        delim = None if single else self._dollar_delimiter(text)
        if not single and delim is None:
            return False, pos, None, None
        if single:
            prefix = self._string_prefix(text)
            value = text[prefix:-1].replace("''", "'")
        else:
            value = text[len(delim):-len(delim)]
        end = self.advance(pos)
        length = tok.length
        count = 1
        if single:
            while True:
                nxt = self.token(end)
                if nxt is None or not nxt.preceded_by_newline or nxt.preceded_by_block_comment:
                    break
                if nxt.type != STRING_LITERAL or not self._is_single_quoted(nxt.text):
                    break
                value += nxt.text[self._string_prefix(nxt.text):-1].replace("''", "'")
                length = nxt.offset + nxt.length - tok.offset
                end = self.advance(end)
                count += 1
        if count > 1:
            node.note = "%d adjacent string literals concatenated" % count
        return True, end, ParseNode("STRING", name=m.name, offset=tok.offset, length=length, value=value,
                                    has_rule=m.name is not None), None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _run_with_big_stack(fn):
    result = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            result["error"] = e

    try:
        old = threading.stack_size(1024 * 1024 * 1024)
    except (RuntimeError, ValueError):
        return fn()  # no thread support (e.g. Pyodide): run on the current stack
    try:
        t = threading.Thread(target=target)
        t.start()
        t.join()
    except RuntimeError:
        return fn()
    finally:
        threading.stack_size(old)
    if "error" in result:
        raise result["error"]
    return result["value"]


def parse_sql(grammar, sql, options=None):
    """Tokenize and match sql against the grammar; returns a JSON-serializable dict."""
    options = options or MatchOptions()
    out = {"sql": sql, "start_rule": options.start_rule, "options": {"packrat": options.packrat,
                                                                      "first_set_skip": options.first_set_skip}}
    tokenizer = Tokenizer(grammar)
    try:
        tokens, _clean = tokenizer.tokenize(sql)
    except TokenizeError as e:
        out["error"] = {"message": "Parser Error: %s" % e, "offset": e.position, "stage": "tokenizer"}
        out["tokens"] = []
        return out
    out["tokens"] = [t.to_json() for t in tokens]
    try:
        start = grammar.get_matcher(options.start_rule)
        grammar.ensure_first_sets()
    except GrammarError as e:
        out["error"] = {"message": "grammar error: %s" % e, "stage": "setup"}
        return out
    if start is None:
        out["error"] = {"message": "unknown start rule %s" % options.start_rule, "stage": "setup"}
        return out

    tracer = Tracer(grammar, tokens, options)
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 1000000))
    try:
        success, end, result = _run_with_big_stack(lambda: tracer.run(start, 0))
    finally:
        sys.setrecursionlimit(old_limit)

    # the last token is always END_OF_INPUT; start rules without EndOfInput cannot consume it
    consumed_all = success and end >= len(tokens) - 1
    out["success"] = bool(success)
    out["consumed_all"] = bool(consumed_all)
    out["end"] = end if success else None
    out["max_token"] = tracer.max_token
    if not success or not consumed_all:
        err_idx = min(tracer.max_token, len(tokens) - 1)
        if err_idx > 0 and tokens[err_idx].type == END_OF_INPUT:
            err_idx -= 1
        tok = tokens[err_idx]
        if success:
            msg = 'matched, but input remains: syntax error at or near "%s"' % tok.text
        else:
            msg = 'syntax error at or near "%s"' % tok.text
        out["error"] = {"message": "Parser Error: " + msg, "offset": tok.offset, "token": err_idx, "stage": "parser"}
        out["expected"] = _expected_at(tracer, grammar, tracer.max_token)
    out["tree"] = result.to_json() if result is not None else None
    out["tree_text"] = result.to_text() if result is not None else ""
    out["trace"] = [n.to_json() for n in tracer.trace]
    out["truncated"] = tracer.truncated
    out["stats"] = tracer.stats
    # every matcher that was tried, plus the alternatives of every choice that was tried (so untried ones show up)
    matchers = dict(tracer.used_matchers)
    for m in list(matchers.values()):
        if m.kind == "CHOICE":
            for ch in m.children:
                matchers[ch.id] = ch
    out["matchers"] = {str(m.id): _matcher_json(m) for m in matchers.values()}
    return out


def _expected_at(tracer, grammar, pos):
    """What the grammar would have accepted at token `pos`: literals and token classes of the atomic matchers that
    were tried there, plus the FIRST sets of the rules skipped there; and the named rules attempted at that position."""
    keywords = set()
    classes = set()
    rules = {}
    class_names = {1: "identifier", 2: "operator", 4: "operator", 8: "string literal", 16: "number literal"}

    def add_class_mask(mask):
        for bit, name in class_names.items():
            if mask & bit:
                classes.add(name)

    for node in tracer.trace:
        if node.start != pos:
            continue
        m = node.matcher
        if m.atomic:
            if m.kind == "KEYWORD":
                keywords.add(m.keyword)
            elif m.kind == "END_OF_INPUT":
                classes.add("end of input")
            else:
                add_class_mask(m.first_set.class_mask)
        elif node.status == SKIP:
            fs = m.first_set
            if fs.any:
                continue
            add_class_mask(fs.class_mask)
            for lit in fs.literals:
                keywords.add(lit.upper() if lit.isalpha() else lit)
        if m.name and node.status not in (OK, CACHE_OK, SKIP_OPT):
            rules[m.name] = rules.get(m.name, 0) + 1
    if "identifier" in classes:
        # keywords that may be used as identifiers are covered by "identifier"
        from peg_grammar import KEYWORD_RESERVED
        keywords = {k for k in keywords if not k.isalpha() or
                    not grammar.keyword_flags.get(k.lower()) or grammar.keyword_flags.get(k.lower()) & KEYWORD_RESERVED}
    tok = tracer.token(pos)
    return {
        "position": pos,
        "token": tok.text if tok is not None else None,
        "keywords": sorted(keywords, key=lambda k: (not k.isalpha(), k)),
        "classes": sorted(classes),
        "rules": sorted(rules),
    }


def _matcher_json(m):
    d = {"id": m.id, "kind": m.kind, "label": m.label(), "desc": m.describe(2)}
    if m.name:
        d["name"] = m.name
    if m.rule_line:
        d["line"] = m.rule_line
    if m.memoized:
        d["memo"] = True
    if m.collapsible:
        d["collapsible"] = True
    if m.kind == "CHOICE":
        d["alts"] = [c.id for c in m.children]
        if m.literal_dispatch is not None:
            d["literal"] = True
    elif m.kind in ("LIST",):
        d["seq"] = [c.id for c in m.children]
    return d


def format_trace_text(result, grammar=None, show_failures=True):
    """Plain-text rendering of the trace for the CLI."""
    lines = []
    matchers = result["matchers"]
    tokens = result["tokens"]
    icons = {OK: "✓", FAIL: "✗", SKIP: "⊘", SKIP_OPT: "∅", CACHE_OK: "✓ (cached)", CACHE_FAIL: "✗ (cached)",
             NO_DISPATCH: "✗ (no alternative)"}
    for row in result["trace"]:
        _id, _parent, mid, _alt, start, end, status, depth = row[:8]
        note = row[8] if len(row) > 8 else None
        if not show_failures and status not in (OK, CACHE_OK, SKIP_OPT):
            continue
        m = matchers[str(mid)]
        span = ""
        if end is not None and end > start:
            span = " ⟶ %s" % " ".join(t["text"] for t in tokens[start:end] if t["text"])
        label = m["label"]
        if m.get("name") is None:
            label = "%s: %s" % (m["kind"].lower(), label)
        lines.append("%s%s %s @%d%s%s" % ("  " * depth, icons.get(status, status), label, start, span,
                                          ("  [%s]" % note) if note else ""))
    return "\n".join(lines)
