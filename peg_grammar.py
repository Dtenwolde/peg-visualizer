"""Load a DuckDB PEG grammar (.gram) and build a matcher tree.

This mirrors src/parser/peg/peg_parser.cpp (grammar syntax), matcher_factory.cpp (matcher construction),
compiled_grammar.cpp (terminal rule overrides), parsed_grammar_keyword_helper.cpp (keyword categories) and
matcher_first_set.cpp (FIRST sets).
"""

import re

# ---------------------------------------------------------------------------
# Grammar expression AST
# ---------------------------------------------------------------------------

LITERAL = "LITERAL"
REFERENCE = "REFERENCE"
FUNCTION_CALL = "FUNCTION_CALL"
SEQUENCE = "SEQUENCE"
CHOICE = "CHOICE"
OPTIONAL = "OPTIONAL"
REPEAT = "REPEAT"
OPTIONAL_REPEAT = "OPTIONAL_REPEAT"
REGEX = "REGEX"


class Expr:
    __slots__ = ("type", "text", "children")

    def __init__(self, type_, text="", children=None):
        self.type = type_
        self.text = text
        self.children = children or []

    def __repr__(self):
        return "Expr(%s, %r, %r)" % (self.type, self.text, self.children)


class Rule:
    __slots__ = ("name", "params", "expr", "line")

    def __init__(self, name, params, expr, line):
        self.name = name
        self.params = params
        self.expr = expr
        self.line = line


class GrammarError(Exception):
    pass


def _is_alnum(c):
    return c.isalnum() or c == "_"


def _unescape_literal(text):
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


class _ExprParser:
    def __init__(self, tokens, rule_name):
        self.tokens = tokens
        self.pos = 0
        self.rule_name = rule_name

    def parse(self):
        root = self.parse_choice()
        if self.pos != len(self.tokens):
            raise GrammarError("Unexpected trailing tokens in rule %s" % self.rule_name)
        return root

    def has_next(self):
        return self.pos < len(self.tokens)

    def is_op(self, op):
        return self.has_next() and self.tokens[self.pos][0] == "OP" and self.tokens[self.pos][1] == op

    def parse_choice(self):
        first = self.parse_sequence()
        if not self.is_op("/"):
            return first
        choice = Expr(CHOICE, children=[first])
        while self.is_op("/"):
            self.pos += 1
            choice.children.append(self.parse_sequence())
        return choice

    def parse_sequence(self):
        seq = Expr(SEQUENCE)
        while self.has_next() and not self.is_op("/") and not self.is_op(")"):
            seq.children.append(self.parse_postfix())
        if len(seq.children) == 1:
            return seq.children[0]
        return seq

    def parse_postfix(self):
        node = self.parse_primary()
        if self.is_op("?"):
            self.pos += 1
            return Expr(OPTIONAL, children=[node])
        if self.is_op("*"):
            self.pos += 1
            return Expr(OPTIONAL_REPEAT, children=[node])
        if self.is_op("+"):
            self.pos += 1
            return Expr(REPEAT, children=[node])
        return node

    def parse_primary(self):
        if self.is_op("("):
            self.pos += 1
            inner = self.parse_choice()
            if not self.is_op(")"):
                raise GrammarError("Expected closing ')' in rule %s" % self.rule_name)
            self.pos += 1
            if inner.type == SEQUENCE:
                return inner
            # a bracketed group always becomes a (possibly single-element) sequence, like the C++ parser
            return Expr(SEQUENCE, children=[inner])
        if self.is_op("!"):
            # negative lookahead is parsed but ignored by the DuckDB matcher
            self.pos += 1
            return self.parse_primary()
        if not self.has_next():
            raise GrammarError("Unexpected end of rule %s" % self.rule_name)
        kind, text = self.tokens[self.pos]
        self.pos += 1
        if kind == "LITERAL":
            return Expr(LITERAL, _unescape_literal(text))
        if kind == "REFERENCE":
            return Expr(REFERENCE, text)
        if kind == "FUNCTION_CALL":
            body = self.parse_choice()
            if not self.is_op(")"):
                raise GrammarError("Expected closing ')' after %s( in rule %s" % (text, self.rule_name))
            self.pos += 1
            return Expr(FUNCTION_CALL, text, [body])
        if kind == "REGEX":
            return Expr(REGEX, text)
        raise GrammarError("Unexpected token %r in rule %s" % (kind, self.rule_name))


def parse_grammar(text):
    """Parse grammar text into an ordered dict of rule name -> Rule (case-insensitive keys preserved as written)."""
    rules = {}
    lower_names = {}
    n = len(text)
    c = 0
    line = 1
    state = "RULE_NAME"
    rule_name = ""
    rule_line = 1
    params = []
    tokens = []
    bracket_count = 0
    in_or_clause = False

    def finish_rule():
        expr = _ExprParser(tokens, rule_name).parse()
        key = rule_name.lower()
        if key in lower_names:
            raise GrammarError("duplicate rule name %s" % rule_name)
        lower_names[key] = rule_name
        rules[rule_name] = Rule(rule_name, list(params), expr, rule_line)

    while c < n:
        ch = text[c]
        if ch == "#":
            while c < n and text[c] not in "\r\n":
                c += 1
            continue
        if ch == "\n":
            line += 1
        if state == "RULE_DEFINITION" and ch in "\r\n" and bracket_count == 0 and not in_or_clause and tokens:
            finish_rule()
            tokens = []
            params = []
            rule_name = ""
            state = "RULE_NAME"
            c += 1
            continue
        if ch.isspace():
            c += 1
            continue
        if state == "RULE_NAME":
            start = c
            if text[c] == "%":
                c += 1
            while c < n and _is_alnum(text[c]):
                c += 1
            if c == start:
                raise GrammarError("expected a rule name at line %d" % line)
            rule_name = text[start:c]
            rule_line = line
            tokens = []
            params = []
            state = "RULE_SEPARATOR"
        elif state == "RULE_SEPARATOR":
            if text[c] == "(":
                c += 1
                pstart = c
                while c < n and _is_alnum(text[c]):
                    c += 1
                if pstart == c or c >= n or text[c] != ")":
                    raise GrammarError("bad parameter list for rule %s (line %d)" % (rule_name, line))
                params.append(text[pstart:c])
                c += 1
            else:
                if text[c : c + 2] != "<-":
                    raise GrammarError("expected '<-' after rule %s (line %d)" % (rule_name, line))
                c += 2
                state = "RULE_DEFINITION"
        else:
            in_or_clause = False
            if ch == "'":
                c += 1
                start = c
                while c < n and text[c] != "'":
                    if text[c] == "\\":
                        c += 1
                    c += 1
                if c >= n:
                    raise GrammarError("unterminated literal in rule %s" % rule_name)
                tokens.append(("LITERAL", text[start:c]))
                c += 1
            elif _is_alnum(ch):
                start = c
                while c < n and _is_alnum(text[c]):
                    c += 1
                name = text[start:c]
                if c < n and text[c] == "(":
                    c += 1
                    bracket_count += 1
                    tokens.append(("FUNCTION_CALL", name))
                else:
                    tokens.append(("REFERENCE", name))
            elif ch in "[<":
                start = c
                final = "]" if ch == "[" else ">"
                while c < n and text[c] != final:
                    if text[c] == "\\":
                        c += 1
                    if c < n:
                        c += 1
                c += 1
                tokens.append(("REGEX", text[start:c]))
            elif ch in "/?()*+!":
                if ch == "(":
                    bracket_count += 1
                elif ch == ")":
                    if bracket_count == 0:
                        raise GrammarError("unbalanced ')' in rule %s (line %d)" % (rule_name, line))
                    bracket_count -= 1
                elif ch == "/":
                    in_or_clause = True
                tokens.append(("OP", ch))
                c += 1
            else:
                raise GrammarError("unrecognized character %r in rule %s (line %d)" % (ch, rule_name, line))
    if state == "RULE_SEPARATOR":
        raise GrammarError("rule %s has no definition" % rule_name)
    if state == "RULE_DEFINITION":
        if not tokens:
            raise GrammarError("rule %s is empty" % rule_name)
        finish_rule()
    return rules


# ---------------------------------------------------------------------------
# Keyword categories (see parsed_grammar_keyword_helper.cpp / default_keyword_maps.cpp)
# ---------------------------------------------------------------------------

KEYWORD_RESERVED = 1
KEYWORD_UNRESERVED = 2
KEYWORD_TYPE_FUNC = 4
KEYWORD_COL_NAME = 8
KEYWORD_TYPE_NAME = 16

KEYWORD_RULES = {
    "ReservedKeyword": KEYWORD_RESERVED,
    "UnreservedKeyword": KEYWORD_UNRESERVED,
    "ColumnNameKeyword": KEYWORD_COL_NAME,
    "TypeFuncKeyword": KEYWORD_TYPE_FUNC,
    "TypeNameKeyword": KEYWORD_TYPE_NAME,
}

CATEGORY_NAMES = {
    KEYWORD_RESERVED: "reserved",
    KEYWORD_UNRESERVED: "unreserved",
    KEYWORD_TYPE_FUNC: "type_func",
    KEYWORD_COL_NAME: "col_name",
    KEYWORD_TYPE_NAME: "type_name",
}


def _collect_keywords(rules, lookup, rule_name, out, active):
    if rule_name in active:
        raise GrammarError("recursive keyword rule %s" % rule_name)
    active.add(rule_name)
    rule = lookup(rule_name)
    if rule is None:
        raise GrammarError("missing keyword rule %s" % rule_name)

    def visit(expr):
        if expr.type == LITERAL:
            out.add(expr.text.lower())
        elif expr.type == REFERENCE:
            _collect_keywords(rules, lookup, expr.text, out, active)
        elif expr.type == CHOICE:
            for ch in expr.children:
                visit(ch)
        else:
            raise GrammarError("keyword rule %s contains unsupported expression" % rule_name)

    visit(rule.expr)
    active.discard(rule_name)


# ---------------------------------------------------------------------------
# Token classes (see matcher_token.hpp)
# ---------------------------------------------------------------------------

CLASS_WORD = 1
CLASS_OPERATOR = 2
CLASS_GENERIC_OPERATOR = 4
CLASS_STRING = 8
CLASS_NUMBER = 16


# ---------------------------------------------------------------------------
# Matchers
# ---------------------------------------------------------------------------

# Suggestion states that select the identifier mask / behaviour (see identifier_matcher.hpp)
SUGGEST_VARIABLE = "VARIABLE"
SUGGEST_CATALOG_NAME = "CATALOG_NAME"
SUGGEST_SCHEMA_NAME = "SCHEMA_NAME"
SUGGEST_TABLE_NAME = "TABLE_NAME"
SUGGEST_TYPE_NAME = "TYPE_NAME"
SUGGEST_COLUMN_NAME = "COLUMN_NAME"
SUGGEST_FILE_NAME = "FILE_NAME"
SUGGEST_SCALAR_FUNCTION_NAME = "SCALAR_FUNCTION_NAME"
SUGGEST_TABLE_FUNCTION_NAME = "TABLE_FUNCTION_NAME"
SUGGEST_PRAGMA_NAME = "PRAGMA_NAME"
SUGGEST_SETTING_NAME = "SETTING_NAME"


def identifier_mask(suggest):
    if suggest == SUGGEST_TYPE_NAME:
        return KEYWORD_UNRESERVED | KEYWORD_TYPE_NAME
    if suggest in (SUGGEST_SCALAR_FUNCTION_NAME, SUGGEST_TABLE_FUNCTION_NAME):
        return KEYWORD_UNRESERVED | KEYWORD_TYPE_FUNC
    return KEYWORD_UNRESERVED | KEYWORD_COL_NAME


class FirstSet:
    __slots__ = ("computed", "nullable", "any", "class_mask", "literals")

    def __init__(self):
        self.computed = False
        self.nullable = False
        self.any = False
        self.class_mask = 0
        self.literals = set()

    def merge(self, other):
        changed = False
        if other.any and not self.any:
            self.any = True
            changed = True
        if (self.class_mask | other.class_mask) != self.class_mask:
            self.class_mask |= other.class_mask
            changed = True
        if not other.literals.issubset(self.literals):
            self.literals |= other.literals
            changed = True
        return changed

    def might_match(self, token):
        """False only if the matcher certainly cannot match at this token."""
        if not self.computed or self.any or self.nullable:
            return True
        if token is None:
            return False
        if token.token_class & self.class_mask:
            return True
        return token.text.lower() in self.literals


class Matcher:
    kind = "?"
    atomic = False

    def __init__(self):
        self.id = -1
        self.name = None  # rule name when this matcher *is* a named rule
        self.memoized = False
        self.collapsible = False
        self.structural = False
        self.first_set = FirstSet()
        self.rule_line = None

    def children_list(self):
        return []

    def describe(self, depth=1):
        """Short grammar-like rendering used in the UI."""
        raise NotImplementedError

    def label(self):
        return self.name if self.name else self.describe()


class KeywordMatcher(Matcher):
    kind = "KEYWORD"
    atomic = True

    def __init__(self, keyword):
        super().__init__()
        self.keyword = keyword
        self.lower = keyword.lower()

    def describe(self, depth=1):
        return "'%s'" % self.keyword


class IdentifierMatcher(Matcher):
    kind = "IDENTIFIER"
    atomic = True

    def __init__(self, suggest, reserved=False):
        super().__init__()
        self.suggest = suggest
        self.reserved = reserved  # ReservedIdentifierMatcher: keywords of any category are accepted
        self.mask = identifier_mask(suggest)

    def supports_string_literal(self):
        return self.suggest in (SUGGEST_TABLE_NAME, SUGGEST_FILE_NAME)

    def describe(self, depth=1):
        return ("Reserved" if self.reserved else "") + self.suggest


class NumberLiteralMatcher(Matcher):
    kind = "NUMBER_LITERAL"
    atomic = True

    def describe(self, depth=1):
        return "NUMBER_LITERAL"


class StringLiteralMatcher(Matcher):
    kind = "STRING_LITERAL"
    atomic = True

    def describe(self, depth=1):
        return "STRING_LITERAL"


class OperatorMatcher(Matcher):
    kind = "OPERATOR"
    atomic = True

    def __init__(self, all_operators=False):
        super().__init__()
        self.all_operators = all_operators

    def describe(self, depth=1):
        return "ANY_OPERATOR" if self.all_operators else "OPERATOR"


class EndOfInputMatcher(Matcher):
    kind = "END_OF_INPUT"
    atomic = True

    def describe(self, depth=1):
        return "EndOfInput"


def _child_desc(child, depth):
    if child.name:
        return child.name
    if depth <= 0:
        return "…"
    return child.describe(depth - 1)


class ListMatcher(Matcher):
    kind = "LIST"

    def __init__(self, children=None):
        super().__init__()
        self.children = children or []
        self.macro_label = None  # e.g. "List(Expression)" for macro instantiations

    def children_list(self):
        return self.children

    def describe(self, depth=1):
        if self.macro_label:
            return self.macro_label
        parts = []
        for ch in self.children:
            d = _child_desc(ch, depth)
            if ch.kind == "CHOICE" and not ch.name and len(self.children) > 1:
                d = "(" + d + ")"
            parts.append(d)
        return " ".join(parts) if parts else "()"


class ChoiceMatcher(Matcher):
    kind = "CHOICE"

    def __init__(self, children, literal_dispatch=None):
        super().__init__()
        self.children = children
        # lowercase literal text -> child index; set when every alternative is a keyword (LiteralChoiceMatcher)
        self.literal_dispatch = literal_dispatch

    def children_list(self):
        return self.children

    def describe(self, depth=1):
        if len(self.children) > 6:
            shown = [_child_desc(c, depth) for c in self.children[:5]]
            return " / ".join(shown) + " / … (%d alternatives)" % len(self.children)
        return " / ".join(_child_desc(c, depth) for c in self.children)


class OptionalMatcher(Matcher):
    kind = "OPTIONAL"

    def __init__(self, child):
        super().__init__()
        self.child = child

    def children_list(self):
        return [self.child]

    def describe(self, depth=1):
        inner = _child_desc(self.child, depth)
        if self.child.kind == "REPEAT" and not self.child.name:
            return inner[:-1] + "*"
        if " " in inner or "/" in inner:
            inner = "(" + inner + ")"
        return inner + "?"


class RepeatMatcher(Matcher):
    kind = "REPEAT"

    def __init__(self, child):
        super().__init__()
        self.child = child

    def children_list(self):
        return [self.child]

    def describe(self, depth=1):
        inner = _child_desc(self.child, depth)
        if " " in inner or "/" in inner:
            inner = "(" + inner + ")"
        return inner + "+"


# Terminal rules that DuckDB implements in C++ instead of expanding the grammar (compiled_grammar.cpp)
def _terminal_overrides():
    return {
        "Identifier": lambda: IdentifierMatcher(SUGGEST_VARIABLE),
        "ReservedIdentifier": lambda: IdentifierMatcher(SUGGEST_VARIABLE, True),
        "CatalogName": lambda: IdentifierMatcher(SUGGEST_CATALOG_NAME),
        "SchemaName": lambda: IdentifierMatcher(SUGGEST_SCHEMA_NAME),
        "ReservedSchemaName": lambda: IdentifierMatcher(SUGGEST_SCHEMA_NAME, True),
        "TableName": lambda: IdentifierMatcher(SUGGEST_TABLE_NAME),
        "ReservedTableName": lambda: IdentifierMatcher(SUGGEST_TABLE_NAME, True),
        "ColumnName": lambda: IdentifierMatcher(SUGGEST_COLUMN_NAME),
        "ReservedColumnName": lambda: IdentifierMatcher(SUGGEST_COLUMN_NAME, True),
        "IndexName": lambda: IdentifierMatcher(SUGGEST_VARIABLE),
        "ReservedIndexName": lambda: IdentifierMatcher(SUGGEST_VARIABLE, True),
        "SequenceName": lambda: IdentifierMatcher(SUGGEST_VARIABLE),
        "FunctionName": lambda: IdentifierMatcher(SUGGEST_SCALAR_FUNCTION_NAME),
        "ReservedFunctionName": lambda: IdentifierMatcher(SUGGEST_SCALAR_FUNCTION_NAME, True),
        "ReservedKeyword": lambda: IdentifierMatcher(SUGGEST_VARIABLE, True),
        "TableFunctionName": lambda: IdentifierMatcher(SUGGEST_TABLE_FUNCTION_NAME),
        "TypeName": lambda: IdentifierMatcher(SUGGEST_TYPE_NAME),
        "ReservedTypeName": lambda: IdentifierMatcher(SUGGEST_TYPE_NAME, True),
        "PragmaName": lambda: IdentifierMatcher(SUGGEST_PRAGMA_NAME),
        "SettingName": lambda: IdentifierMatcher(SUGGEST_SETTING_NAME),
        "CopyOptionName": lambda: IdentifierMatcher(SUGGEST_VARIABLE, True),
        "NumberLiteral": NumberLiteralMatcher,
        "StringLiteral": StringLiteralMatcher,
        "OperatorLiteral": lambda: OperatorMatcher(False),
        "AnyOperatorLiteral": lambda: OperatorMatcher(True),
        "EndOfInput": EndOfInputMatcher,
    }


PACKRAT_MEMOIZED_RULES = {
    "Expression",
    "Identifier",
    "ColId",
    "CatalogQualification",
    "SchemaQualification",
    "ReservedSchemaQualification",
}

# see PEGTransformerFactory (collapsible rules forward a single child's result instead of wrapping it)
COLLAPSIBLE_RULES = {
    "Expression",
    "ColumnDefaultExpr",
    "LambdaArrowExpression",
    "LogicalOrExpression",
    "ColDefOrExpr",
    "LogicalAndExpression",
    "ColDefAndExpr",
    "LogicalNotExpression",
    "IsExpression",
    "IsDistinctFromExpression",
    "ComparisonExpression",
    "BetweenInLikeExpression",
    "OtherOperatorExpression",
    "InfixOtherOperatorExpression",
    "BitwiseExpression",
    "AdditiveExpression",
    "MultiplicativeExpression",
    "ExponentiationExpression",
    "CollateExpression",
    "AtTimeZoneExpression",
    "PrefixExpression",
    "BaseExpression",
    "SelectSetOpChain",
    "IntersectChain",
    "TableRef",
}


class Grammar:
    def __init__(self, text, source=None):
        self.source = source
        self.text = text
        self.rules = parse_grammar(text)
        self._lower = {name.lower(): name for name in self.rules}
        self.matchers = []  # all matchers, indexed by id
        self.rule_matchers = {}  # rule name -> matcher (named rules and terminal overrides)
        self._keywords = {}  # keyword text (lower) -> KeywordMatcher
        self._macro_cache = {}
        self.keyword_flags = {}  # lower keyword -> category flags
        self.literals = set()  # every literal text (lower) in the grammar
        self._overrides = _terminal_overrides()
        self._pending = []
        self._draining = False
        self._first_sets_dirty = True
        self._build_keyword_maps()

    # -- lookup ---------------------------------------------------------------
    def get_rule(self, name):
        real = self._lower.get(name.lower())
        return self.rules.get(real) if real else None

    def get_matcher(self, name):
        """Matcher for a rule; rules are compiled on first use, like MatcherFactory builds them from the root."""
        real = self._lower.get(name.lower())
        if real is None:
            if name.lower() == "endofinput":
                return self._construct_eoi()
            return None
        m = self.rule_matchers.get(real)
        if m is None:
            m = self._construct_rule(real)
        return m

    def ensure_first_sets(self):
        if self._first_sets_dirty:
            self._compute_first_sets()
            self._first_sets_dirty = False

    def rule_names(self):
        return [name for name, rule in self.rules.items() if not rule.params]

    def is_keyword(self, text):
        return text.lower() in self.keyword_flags

    def keyword_categories(self, text):
        flags = self.keyword_flags.get(text.lower(), 0)
        return [n for f, n in CATEGORY_NAMES.items() if flags & f]

    # -- construction ---------------------------------------------------------
    def _build_keyword_maps(self):
        for rule_name, flag in KEYWORD_RULES.items():
            words = set()
            _collect_keywords(self.rules, self.get_rule, rule_name, words, set())
            for w in words:
                self.keyword_flags[w] = self.keyword_flags.get(w, 0) | flag

    def _register(self, matcher):
        matcher.id = len(self.matchers)
        self.matchers.append(matcher)
        return matcher

    def _structural(self, matcher):
        matcher.structural = True
        return self._register(matcher)

    def _keyword(self, text):
        key = text.lower()
        m = self._keywords.get(key)
        if m is None:
            m = self._register(KeywordMatcher(text))
            self._keywords[key] = m
            self.literals.add(key)
        return m

    def _choice(self, children):
        dispatch = None
        if len(children) > 1 and all(c.kind == "KEYWORD" for c in children):
            dispatch = {}
            for i, c in enumerate(children):
                dispatch.setdefault(c.lower, i)
        return self._structural(ChoiceMatcher(children, dispatch))

    def _construct_eoi(self):
        m = self.rule_matchers.get("EndOfInput")
        if m is None:
            m = self._register(EndOfInputMatcher())
            m.name = "EndOfInput"
            self.rule_matchers["EndOfInput"] = m
            self._lower["endofinput"] = "EndOfInput"
            self._first_sets_dirty = True
        return m

    def _construct_rule(self, name):
        rule = self.rules[name]
        if rule.params:
            raise GrammarError("rule %s is a macro and needs a parameter" % name)
        if name in self._overrides:
            m = self._register(self._overrides[name]())
        else:
            m = self._structural(ListMatcher())
            self._pending.append((m, rule))
        m.name = name
        m.rule_line = rule.line
        m.memoized = name in PACKRAT_MEMOIZED_RULES
        m.collapsible = name in COLLAPSIBLE_RULES
        self.rule_matchers[name] = m
        self._first_sets_dirty = True
        self._drain_pending()
        return m

    def _drain_pending(self):
        # rule bodies are filled breadth-first; references register shells first so recursion resolves immediately
        if self._draining:
            return
        self._draining = True
        try:
            while self._pending:
                m, rule = self._pending.pop()
                self._fill_rule_body(m, rule, {}, [])
        finally:
            self._draining = False

    def _fill_rule_body(self, list_matcher, rule, param_map, params):
        expr_matcher = self._create(rule.expr, param_map, params)
        if rule.expr.type == SEQUENCE:
            list_matcher.children = list(expr_matcher.children)
            # the anonymous sequence matcher is replaced by the named rule; drop it from the registry
            self.matchers[expr_matcher.id] = None
        else:
            list_matcher.children = [expr_matcher]

    def _create(self, expr, param_map, params):
        t = expr.type
        if t == LITERAL:
            return self._keyword(expr.text)
        if t == REFERENCE:
            if expr.text in param_map:
                return params[param_map[expr.text]]
            m = self.get_matcher(expr.text)
            if m is None:
                raise GrammarError("reference to unknown rule %s" % expr.text)
            return m
        if t == FUNCTION_CALL:
            arg = self._create(expr.children[0], param_map, params)
            return self._instantiate_macro(expr.text, [arg])
        if t == SEQUENCE:
            return self._structural(ListMatcher([self._create(c, param_map, params) for c in expr.children]))
        if t == CHOICE:
            return self._choice([self._create(c, param_map, params) for c in expr.children])
        if t in (OPTIONAL, REPEAT, OPTIONAL_REPEAT):
            child = self._create(expr.children[0], param_map, params)
            if t == OPTIONAL:
                return self._structural(OptionalMatcher(child))
            rep = self._structural(RepeatMatcher(child))
            if t == OPTIONAL_REPEAT:
                return self._structural(OptionalMatcher(rep))
            return rep
        if t == REGEX:
            raise GrammarError("regex %s is only supported in rules that DuckDB overrides with a built-in matcher" % expr.text)
        raise GrammarError("unknown expression type %s" % t)

    def _instantiate_macro(self, name, args):
        rule = self.get_rule(name)
        if rule is None:
            raise GrammarError("call of unknown macro %s" % name)
        if len(rule.params) != len(args):
            raise GrammarError("macro %s expects %d parameter(s)" % (name, len(rule.params)))
        key = (rule.name, tuple(a.id for a in args))
        cached = self._macro_cache.get(key)
        if cached is not None:
            return cached
        m = self._structural(ListMatcher())
        m.macro_label = "%s(%s)" % (rule.name, ", ".join(a.label() for a in args))
        m.rule_line = rule.line
        self._macro_cache[key] = m
        param_map = {p: i for i, p in enumerate(rule.params)}
        self._fill_rule_body(m, rule, param_map, args)
        return m

    # -- FIRST sets (matcher_first_set.cpp) -----------------------------------
    def _compute_first_sets(self):
        composites = []
        for m in self.matchers:
            if m is None:
                continue
            fs = m.first_set = FirstSet()
            if m.atomic:
                if m.kind == "KEYWORD":
                    fs.literals.add(m.lower)
                elif m.kind == "IDENTIFIER":
                    fs.class_mask = CLASS_WORD
                elif m.kind == "NUMBER_LITERAL":
                    fs.class_mask = CLASS_NUMBER
                elif m.kind == "STRING_LITERAL":
                    fs.class_mask = CLASS_STRING
                elif m.kind == "OPERATOR":
                    fs.class_mask = CLASS_OPERATOR if m.all_operators else CLASS_GENERIC_OPERATOR
                else:
                    fs.any = True
            elif m.structural:
                composites.append(m)
            else:
                fs.any = True
        changed = True
        while changed:
            changed = False
            for m in composites:
                changed |= self._update_first_set(m)
        for m in self.matchers:
            if m is not None:
                m.first_set.computed = True

    @staticmethod
    def _update_first_set(m):
        fs = m.first_set
        changed = False
        if m.kind == "LIST":
            nullable = True
            for ch in m.children:
                changed |= fs.merge(ch.first_set)
                if not ch.first_set.nullable:
                    nullable = False
                    break
        elif m.kind == "CHOICE":
            nullable = False
            for ch in m.children:
                changed |= fs.merge(ch.first_set)
                nullable = nullable or ch.first_set.nullable
        elif m.kind == "OPTIONAL":
            changed |= fs.merge(m.child.first_set)
            nullable = True
        elif m.kind == "REPEAT":
            changed |= fs.merge(m.child.first_set)
            nullable = m.child.first_set.nullable
        else:
            return False
        if nullable and not fs.nullable:
            fs.nullable = True
            changed = True
        return changed


def rule_source(grammar, name):
    """Return the grammar text of a rule (for display)."""
    rule = grammar.get_rule(name)
    if rule is None:
        return ""
    lines = grammar.text.split("\n")
    start = rule.line - 1
    out = [lines[start]]
    i = start + 1
    pattern = re.compile(r"^[%A-Za-z_][A-Za-z0-9_]*(\([A-Za-z0-9_]+\))?\s*<-")
    while i < len(lines) and lines[i].strip() and not pattern.match(lines[i]):
        out.append(lines[i])
        i += 1
    return "\n".join(out)
