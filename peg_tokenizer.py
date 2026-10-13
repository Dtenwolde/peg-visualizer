"""Port of DuckDB's SQL tokenizer (src/parser/peg/tokenizer/base_tokenizer.cpp + parser_tokenizer.cpp)."""

from peg_grammar import CLASS_WORD, CLASS_OPERATOR, CLASS_GENERIC_OPERATOR, CLASS_STRING, CLASS_NUMBER

KEYWORD = "KEYWORD"
IDENTIFIER = "IDENTIFIER"
STRING_LITERAL = "STRING_LITERAL"
NUMBER_LITERAL = "NUMBER_LITERAL"
OPERATOR = "OPERATOR"
TERMINATOR = "TERMINATOR"
END_OF_INPUT = "END_OF_INPUT"


class TokenizeError(Exception):
    def __init__(self, message, position):
        super().__init__(message)
        self.position = position


class Token:
    __slots__ = ("text", "type", "offset", "length", "unterminated", "preceded_by_newline", "preceded_by_block_comment",
                 "token_class", "index")

    def __init__(self, text, offset, type_, unterminated=False):
        self.text = text
        self.type = type_
        self.offset = offset
        self.length = len(text)
        self.unterminated = unterminated
        self.preceded_by_newline = False
        self.preceded_by_block_comment = False
        self.token_class = compute_token_class(type_, text)
        self.index = -1

    def to_json(self):
        return {"text": self.text, "type": self.type, "offset": self.offset, "length": self.length,
                "class": self.token_class}


def has_special_precedence(op):
    n = len(op)
    if n == 1:
        return op in "=<>~"
    if n == 2:
        return op in ("->", "<=", ">=", "!=", "==", "<>", "~~", "~*", "!~")
    if n == 3:
        return op in ("~~*", "~~~", "!~~", "!~*")
    if n == 4:
        return op == "!~~*"
    return False


def compute_token_class(type_, text):
    if type_ in (KEYWORD, IDENTIFIER):
        return CLASS_WORD
    if type_ == STRING_LITERAL:
        return CLASS_STRING | CLASS_WORD
    if type_ == NUMBER_LITERAL:
        return CLASS_NUMBER
    if type_ == OPERATOR:
        if has_special_precedence(text):
            return CLASS_OPERATOR
        return CLASS_OPERATOR | CLASS_GENERIC_OPERATOR
    return 0


def is_single_byte_operator(c):
    return c in "(){}[],:?$#"


def is_initial_number(c):
    return c.isdigit() and c.isascii() or c == "."


def is_special_string_character(c):
    return c in "NnXxEeBb"


def is_number_char(c):
    return is_initial_number(c) or c in "eE_"


def is_scientific(c):
    return c in "eE"


def is_control_flow(c):
    return c in "';\"."


def string_util_is_operator(c):
    # StringUtil::CharacterIsOperator
    return c in "+-*/%^<>=~!@&|#?`"


def is_space(c):
    return c in " \t\n\r\f\v"


def is_keyword_char(c):
    if is_single_byte_operator(c) or string_util_is_operator(c) or is_space(c) or is_control_flow(c):
        return False
    return True


def is_operator_char(c):
    return c in "+-*/%^<>=~!@&|"


def is_valid_dollar_tag_char(c):
    return c.isalnum() and c.isascii() or c == "_" or ord(c) >= 0x80


def _state_to_type(state):
    return {
        "STANDARD": IDENTIFIER,
        "QUOTED_IDENTIFIER": IDENTIFIER,
        "STRING_LITERAL": STRING_LITERAL,
        "KEYWORD": KEYWORD,
        "NUMERIC": NUMBER_LITERAL,
        "OPERATOR": OPERATOR,
        "DOLLAR_QUOTED_STRING": STRING_LITERAL,
    }[state]


class Tokenizer:
    def __init__(self, grammar):
        self.grammar = grammar

    def tokenize(self, sql):
        tokens = []
        self._has_block_comment = False
        self._last_block_comment = 0
        self._sql = sql
        self._tokens = tokens
        clean = self._tokenize_internal(sql)
        tokens.append(Token("", len(sql), END_OF_INPUT))
        for i, t in enumerate(tokens):
            t.index = i
        return tokens, clean

    # -- TokenizerBehavior / ParserTokenizerBehavior ---------------------------
    def _push(self, start, end, type_, unterminated=False):
        sql = self._sql
        if type_ == "COMMENT":
            if unterminated:
                raise TokenizeError('unterminated /* comment at or near "%s"' % sql[start:end], start)
            if end >= start + 2 and sql[start:start + 2] == "/*":
                self._has_block_comment = True
                self._last_block_comment = start
            return
        if start >= end:
            return
        text = sql[start:end]
        if type_ == IDENTIFIER and end == start + 2 and text in ('""', "``"):
            raise TokenizeError("zero-length delimited identifier", start)
        self._emit(Token(text, start, type_, unterminated))

    def _emit(self, token):
        tokens = self._tokens
        tokens.append(token)
        if len(tokens) < 2:
            return
        prev = tokens[-2]
        prev_end = prev.offset + prev.length
        start = token.offset
        if self._has_block_comment and prev_end <= self._last_block_comment < start:
            token.preceded_by_block_comment = True
        for pos in range(prev_end, start):
            if self._sql[pos] in "\r\n":
                token.preceded_by_newline = True
                break

    def _push_operator(self, start, end):
        sql = self._sql
        end_pos = end
        has_special = any(ch in "~!@#%^&|`?" for ch in sql[start:end])
        if not has_special:
            while end_pos > start + 1 and sql[end_pos - 1] in "+-":
                end_pos -= 1
        self._push(start, end_pos, OPERATOR)
        for pos in range(end_pos, end):
            self._emit(Token(sql[pos], pos, OPERATOR))

    def _on_last_token(self, state, last_word, last_pos):
        if state == "STRING_LITERAL":
            raise TokenizeError("unterminated string literal", last_pos)
        if state == "QUOTED_IDENTIFIER":
            raise TokenizeError("unterminated quoted identifier", last_pos)
        if not last_word:
            return
        if state == "KEYWORD" and not self.grammar.is_keyword(last_word):
            state = "STANDARD"
        unterminated = state in ("STRING_LITERAL", "QUOTED_IDENTIFIER", "DOLLAR_QUOTED_STRING")
        self._emit(Token(last_word, last_pos, _state_to_type(state), unterminated))

    # -- Tokenizer::TokenizeInputInternal ---------------------------------------
    def _tokenize_internal(self, sql):
        n = len(sql)
        state = "STANDARD"
        last_pos = 0
        escape_string = False
        quote_delim = '"'
        dollar_marker = ""
        comment_depth = 0
        i = 0
        while i < n:
            c = sql[i]
            if state == "STANDARD":
                if c == "'":
                    state = "STRING_LITERAL"
                    last_pos = i
                    escape_string = False
                elif c in '"`':
                    state = "QUOTED_IDENTIFIER"
                    quote_delim = c
                    last_pos = i
                elif c == ";":
                    self._emit(Token(";", i, TERMINATOR))
                    last_pos = i + 1
                elif c == "$":
                    if i + 1 >= n:
                        pass
                    elif sql[i + 1].isdigit():
                        self._emit(Token("$", i, OPERATOR))
                    else:
                        last_pos = i
                        next_dollar = 0
                        for idx in range(i + 1, n):
                            if sql[idx] == "$":
                                next_dollar = idx
                                break
                            if not is_valid_dollar_tag_char(sql[idx]):
                                break
                        if next_dollar == 0:
                            self._emit(Token("$", i, OPERATOR))
                        else:
                            state = "DOLLAR_QUOTED_STRING"
                            last_pos = i
                            i = next_dollar
                            dollar_marker = sql[last_pos + 1:i]
                elif c == "-" and i + 1 < n and sql[i + 1] == "-":
                    i += 1
                    state = "SINGLE_LINE_COMMENT"
                elif c == "/" and i + 1 < n and sql[i + 1] == "*":
                    i += 1
                    state = "MULTI_LINE_COMMENT"
                    comment_depth = 1
                elif is_space(c):
                    last_pos = i + 1
                elif c == ":" and i + 1 < n and sql[i + 1] in ":=":
                    if i + 2 < n and is_operator_char(sql[i + 2]):
                        state = "OPERATOR"
                        last_pos = i
                    else:
                        self._emit(Token(sql[i:i + 2], i, OPERATOR))
                        i += 1
                        last_pos = i + 1
                elif is_single_byte_operator(c):
                    self._emit(Token(c, i, OPERATOR))
                    last_pos = i + 1
                elif is_initial_number(c):
                    state = "NUMERIC"
                    last_pos = i
                elif is_special_string_character(c) and i + 1 < n and sql[i + 1] == "'":
                    state = "STRING_LITERAL"
                    last_pos = i
                    escape_string = c in "Ee"
                    i += 1
                elif string_util_is_operator(c):
                    state = "OPERATOR"
                    last_pos = i
                else:
                    state = "KEYWORD"
                    last_pos = i
            elif state == "NUMERIC":
                cont = False
                if is_initial_number(c):
                    cont = True
                elif c == "_" and i + 1 < n and is_initial_number(sql[i + 1]):
                    cont = True
                elif is_scientific(c) and not is_scientific(sql[i - 1]):
                    if sql[last_pos].isdigit() or sql[i - 1].isdigit():
                        cont = True
                elif c in "+-" and is_scientific(sql[i - 1]):
                    cont = True
                if not cont:
                    while not is_initial_number(sql[i - 1]):
                        i -= 1
                    self._push(last_pos, i, NUMBER_LITERAL)
                    state = "STANDARD"
                    last_pos = i
                    i -= 1
            elif state == "OPERATOR":
                if (c == "-" and i + 1 < n and sql[i + 1] == "-") or (c == "/" and i + 1 < n and sql[i + 1] == "*"):
                    self._push_operator(last_pos, i)
                    state = "STANDARD"
                    last_pos = i
                    i -= 1
                elif not is_operator_char(c):
                    self._push_operator(last_pos, i)
                    state = "STANDARD"
                    last_pos = i
                    i -= 1
            elif state == "KEYWORD":
                if c != "$" and not is_keyword_char(c):
                    word = sql[last_pos:i]
                    type_ = KEYWORD if self.grammar.is_keyword(word) else IDENTIFIER
                    self._push(last_pos, i, type_)
                    state = "STANDARD"
                    last_pos = i
                    i -= 1
            elif state == "STRING_LITERAL":
                if escape_string and c == "\\" and i + 1 < n:
                    i += 1
                elif c == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 1
                    else:
                        self._push(last_pos, i + 1, STRING_LITERAL)
                        last_pos = i + 1
                        escape_string = False
                        state = "STANDARD"
            elif state == "QUOTED_IDENTIFIER":
                if c == quote_delim:
                    if i + 1 < n and sql[i + 1] == quote_delim:
                        i += 1
                    else:
                        self._push(last_pos, i + 1, IDENTIFIER)
                        last_pos = i + 1
                        state = "STANDARD"
            elif state == "SINGLE_LINE_COMMENT":
                if c in "\r\n":
                    self._push(last_pos, i + 1, "COMMENT")
                    last_pos = i + 1
                    state = "STANDARD"
            elif state == "MULTI_LINE_COMMENT":
                if c == "/" and i + 1 < n and sql[i + 1] == "*":
                    i += 1
                    comment_depth += 1
                elif c == "*" and i + 1 < n and sql[i + 1] == "/":
                    i += 1
                    comment_depth -= 1
                    if comment_depth == 0:
                        self._push(last_pos, i + 1, "COMMENT")
                        last_pos = i + 1
                        state = "STANDARD"
            elif state == "DOLLAR_QUOTED_STRING":
                if c == "$" and i + 1 < n:
                    start = i + 1
                    end = start
                    while end < n and sql[end] != "$":
                        end += 1
                    if end < n and sql[start:end] == dollar_marker:
                        self._push(last_pos, end + 1, STRING_LITERAL)
                        dollar_marker = ""
                        state = "STANDARD"
                        i = end
                        last_pos = i + 1
            i += 1

        if state == "SINGLE_LINE_COMMENT":
            self._push(last_pos, n, "COMMENT")
            return False
        if state == "MULTI_LINE_COMMENT":
            self._push(last_pos, n, "COMMENT", True)
            return False
        if state == "OPERATOR":
            self._push_operator(last_pos, n)
            return True
        if state == "DOLLAR_QUOTED_STRING":
            self._push(last_pos, n, STRING_LITERAL, True)
            return False
        self._on_last_token(state, sql[last_pos:], last_pos)
        return True
