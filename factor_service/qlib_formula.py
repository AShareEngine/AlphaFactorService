from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOKEN_RE = re.compile(
    r"\s+|\$asset\.[A-Za-z_][A-Za-z0-9_]*|\$[A-Za-z_][A-Za-z0-9_]*|"
    r"\d+(?:\.\d+)?|>=|<=|==|!=|&&|\|\||[A-Za-z_][A-Za-z0-9_]*|[+\-*/(),<>!]"
)


class FormulaError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledFormula:
    sql: str
    fields: list[str]
    max_window: int


@dataclass(frozen=True)
class SqlExpr:
    sql: str
    fields: frozenset[str]
    max_window: int = 1
    has_window: bool = False


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


@dataclass(frozen=True)
class NumberNode:
    value: str


@dataclass(frozen=True)
class VariableNode:
    name: str


@dataclass(frozen=True)
class UnaryNode:
    op: str
    expr: Any


@dataclass(frozen=True)
class BinaryNode:
    op: str
    left: Any
    right: Any


@dataclass(frozen=True)
class CallNode:
    name: str
    args: list[Any]


ROLLING_FUNCTIONS = {
    "Mean": "avg",
    "Sum": "sum",
    "Std": "stddevSamp",
    "Var": "varSamp",
    "Skew": "skewSamp",
    "Kurt": "kurtSamp",
    "Max": "max",
    "Min": "min",
    "Med": "median",
    "Count": "count",
}
PAIR_ELEMENT_FUNCTIONS = {
    "Add": "+",
    "Sub": "-",
    "Mul": "*",
    "Div": "/",
    "Greater": "greatest",
    "Less": "least",
}
PAIR_ROLLING_FUNCTIONS = {
    "Corr": "corr",
    "Cov": "covarSamp",
}
UNSUPPORTED_WINDOW_FUNCTIONS = {
    "Mask",
    "ChangeInstrument",
}
EXPANDED_WINDOW_FUNCTIONS = {
    "EMA",
    "WMA",
    "Rank",
    "IdxMax",
    "IdxMin",
    "Slope",
    "Rsquare",
    "Resi",
}
ELEMENT_FUNCTIONS = {
    "Abs": ("abs", 1, 1),
    "Sign": ("sign", 1, 1),
    "Log": ("log", 1, 1),
    "Power": ("pow", 2, 2),
    "NullIf": ("nullIf", 2, 2),
    "IsNull": ("isNull", 1, 1),
}
COMPARISON_FUNCTIONS = {
    "Gt": ">",
    "Ge": ">=",
    "Lt": "<",
    "Le": "<=",
    "Eq": "=",
    "Ne": "!=",
}
CANONICAL_FUNCTIONS = {
    name.lower(): name
    for name in [
        *ROLLING_FUNCTIONS,
        *PAIR_ELEMENT_FUNCTIONS,
        *PAIR_ROLLING_FUNCTIONS,
        *UNSUPPORTED_WINDOW_FUNCTIONS,
        *EXPANDED_WINDOW_FUNCTIONS,
        *ELEMENT_FUNCTIONS,
        *COMPARISON_FUNCTIONS,
        "Ref",
        "Delta",
        "Mad",
        "Quantile",
        "If",
        "Fillna",
        "And",
        "Or",
        "Not",
        "PeriodReturn",
        "FirstTrue",
    ]
}
CANONICAL_FUNCTIONS.update(
    {
        "period_return": "PeriodReturn",
        "first_true": "FirstTrue",
        "nullif": "NullIf",
        "isnull": "IsNull",
        "coalesce": "Fillna",
    }
)


def compile_qlib_formula(
    expression: str,
    *,
    params: Optional[dict[str, Any]] = None,
    code_column: str,
    date_column: str,
) -> CompiledFormula:
    parser = FormulaParser(tokenize_formula(expression))
    node = parser.parse()
    context = CompileContext(
        params=params or {},
        code_column=identifier(code_column, "code column"),
        date_column=identifier(date_column, "date column"),
    )
    compiled = compile_node(node, context)
    return CompiledFormula(
        sql=compiled.sql,
        fields=sorted(compiled.fields),
        max_window=max(1, compiled.max_window),
    )


def tokenize_formula(expression: str) -> list[Token]:
    raw = str(expression or "").strip()
    if not raw:
        raise FormulaError("因子表达式不能为空")
    tokens: list[Token] = []
    index = 0
    match = TOKEN_RE.match(raw, index)
    while match:
        value = match.group(0)
        index = match.end()
        if value.isspace():
            match = TOKEN_RE.match(raw, index)
            continue
        if value.startswith("$asset."):
            tokens.append(Token("variable", value.replace("$asset.", "$", 1)))
        elif value.startswith("$"):
            tokens.append(Token("variable", value))
        elif re.match(r"^\d", value):
            tokens.append(Token("number", value))
        elif re.match(r"^[A-Za-z_]", value):
            tokens.append(Token("identifier", value))
        else:
            tokens.append(Token("symbol", value))
        match = TOKEN_RE.match(raw, index)
    if index != len(raw):
        raise FormulaError(f"表达式存在不支持的字符: {raw[index:]}")
    return tokens


class FormulaParser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0

    def parse(self) -> Any:
        node = self.parse_or()
        if self.peek() is not None:
            raise FormulaError(f"表达式存在多余内容: {self.peek().value}")
        return node

    def parse_or(self) -> Any:
        node = self.parse_and()
        while self.match("||"):
            node = BinaryNode("||", node, self.parse_and())
        return node

    def parse_and(self) -> Any:
        node = self.parse_compare()
        while self.match("&&"):
            node = BinaryNode("&&", node, self.parse_compare())
        return node

    def parse_compare(self) -> Any:
        node = self.parse_add()
        while True:
            token = self.peek()
            if token and token.value in {">", "<", ">=", "<=", "==", "!="}:
                self.index += 1
                node = BinaryNode(token.value, node, self.parse_add())
                continue
            return node

    def parse_add(self) -> Any:
        node = self.parse_mul()
        while True:
            token = self.peek()
            if token and token.value in {"+", "-"}:
                self.index += 1
                node = BinaryNode(token.value, node, self.parse_mul())
                continue
            return node

    def parse_mul(self) -> Any:
        node = self.parse_unary()
        while True:
            token = self.peek()
            if token and token.value in {"*", "/"}:
                self.index += 1
                node = BinaryNode(token.value, node, self.parse_unary())
                continue
            return node

    def parse_unary(self) -> Any:
        token = self.peek()
        if token and token.value in {"-", "!"}:
            self.index += 1
            return UnaryNode(token.value, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> Any:
        token = self.peek()
        if token is None:
            raise FormulaError("表达式不完整")
        self.index += 1
        if token.kind == "number":
            return NumberNode(token.value)
        if token.kind == "variable":
            return VariableNode(token.value[1:])
        if token.value == "(":
            node = self.parse_or()
            self.expect(")")
            return node
        if token.kind == "identifier":
            name = canonical_function(token.value)
            if not name:
                raise FormulaError(f"不支持的函数: {token.value}")
            self.expect("(")
            args: list[Any] = []
            if not self.match(")"):
                while True:
                    args.append(self.parse_or())
                    if self.match(")"):
                        break
                    self.expect(",")
            return CallNode(name, args)
        raise FormulaError(f"表达式位置不正确: {token.value}")

    def expect(self, value: str) -> None:
        if not self.match(value):
            current = self.peek().value if self.peek() else "结尾"
            raise FormulaError(f"期望 {value}，实际为 {current}")

    def match(self, value: str) -> bool:
        token = self.peek()
        if token and token.value == value:
            self.index += 1
            return True
        return False

    def peek(self) -> Optional[Token]:
        return self.tokens[self.index] if self.index < len(self.tokens) else None


@dataclass(frozen=True)
class CompileContext:
    params: dict[str, Any]
    code_column: str
    date_column: str


def compile_node(node: Any, context: CompileContext) -> SqlExpr:
    if isinstance(node, NumberNode):
        return SqlExpr(sql=numeric_literal(node.value), fields=frozenset())
    if isinstance(node, VariableNode):
        if node.name in context.params or node.name == "window":
            value = context.params.get(node.name, 20 if node.name == "window" else 0)
            return SqlExpr(sql=numeric_literal(value), fields=frozenset())
        field = identifier(node.name, "factor field")
        return SqlExpr(sql=field, fields=frozenset({field}))
    if isinstance(node, UnaryNode):
        expr = compile_node(node.expr, context)
        if node.op == "!":
            return merge_sql(f"NOT ({expr.sql})", [expr])
        return merge_sql(f"(-{expr.sql})", [expr])
    if isinstance(node, BinaryNode):
        left = compile_node(node.left, context)
        right = compile_node(node.right, context)
        op = {"&&": "AND", "||": "OR", "==": "="}.get(node.op, node.op)
        return merge_sql(f"({left.sql} {op} {right.sql})", [left, right])
    if isinstance(node, CallNode):
        return compile_call(node, context)
    raise FormulaError("无法识别的表达式节点")


def compile_call(node: CallNode, context: CompileContext) -> SqlExpr:
    name = node.name
    if name in ROLLING_FUNCTIONS:
        require_arg_count(name, node.args, 2, 2)
        expr = compile_node(node.args[0], context)
        ensure_no_nested_window(name, expr)
        window = compile_window(node.args[1], context)
        return merge_sql(
            f"{ROLLING_FUNCTIONS[name]}({expr.sql}) {window_clause(context, window)}",
            [expr],
            max_window=window,
            has_window=True,
        )
    if name in PAIR_ELEMENT_FUNCTIONS:
        require_arg_count(name, node.args, 2, 2)
        left = compile_node(node.args[0], context)
        right = compile_node(node.args[1], context)
        op = PAIR_ELEMENT_FUNCTIONS[name]
        if op in {"+", "-", "*", "/"}:
            return merge_sql(f"({left.sql} {op} {right.sql})", [left, right])
        return merge_sql(f"{op}({left.sql}, {right.sql})", [left, right])
    if name in PAIR_ROLLING_FUNCTIONS:
        require_arg_count(name, node.args, 3, 3)
        left = compile_node(node.args[0], context)
        right = compile_node(node.args[1], context)
        ensure_no_nested_window(name, left, right)
        window = compile_window(node.args[2], context)
        return merge_sql(
            f"{PAIR_ROLLING_FUNCTIONS[name]}({left.sql}, {right.sql}) {window_clause(context, window)}",
            [left, right],
            max_window=window,
            has_window=True,
        )
    if name in UNSUPPORTED_WINDOW_FUNCTIONS:
        raise FormulaError(f"{name} 属于 Qlib 标准函数，但当前 ClickHouse 编译器暂未支持")
    if name in {"EMA", "WMA", "Rank", "IdxMax", "IdxMin", "Slope", "Rsquare", "Resi"}:
        require_arg_count(name, node.args, 2, 2)
        expr = compile_node(node.args[0], context)
        ensure_no_nested_window(name, expr)
        window = compile_window(node.args[1], context)
        if name == "EMA":
            return compile_ema(expr, context, window)
        if name == "WMA":
            return compile_wma(expr, context, window)
        if name in {"Rank", "Quantile"}:
            return compile_rank(expr, context, window)
        if name in {"IdxMax", "IdxMin"}:
            return compile_idx_extreme(expr, context, window, is_max=name == "IdxMax")
        if name in {"Slope", "Rsquare", "Resi"}:
            return compile_linear_regression(name, expr, context, window)
    if name == "Mad":
        require_arg_count(name, node.args, 2, 2)
        expr = compile_node(node.args[0], context)
        ensure_no_nested_window(name, expr)
        window = compile_window(node.args[1], context)
        return compile_mad(expr, context, window)
    if name == "Quantile":
        require_arg_count(name, node.args, 3, 3)
        expr = compile_node(node.args[0], context)
        ensure_no_nested_window(name, expr)
        window = compile_window(node.args[1], context)
        qscore = compile_scalar_number(node.args[2], context, "qscore")
        return merge_sql(
            f"quantile({qscore})({expr.sql}) {window_clause(context, window)}",
            [expr],
            max_window=window,
            has_window=True,
        )
    if name == "Ref":
        require_arg_count(name, node.args, 2, 2)
        expr = compile_node(node.args[0], context)
        ensure_no_nested_window(name, expr)
        window = compile_window(node.args[1], context)
        return merge_sql(
            f"lagInFrame({expr.sql}, {window}) {window_clause(context, window + 1, preceding=window)}",
            [expr],
            max_window=window + 1,
            has_window=True,
        )
    if name == "Delta":
        require_arg_count(name, node.args, 2, 2)
        expr = compile_node(node.args[0], context)
        ensure_no_nested_window(name, expr)
        window = compile_window(node.args[1], context)
        lag_expr = f"lagInFrame({expr.sql}, {window}) {window_clause(context, window + 1, preceding=window)}"
        return merge_sql(f"({expr.sql} - {lag_expr})", [expr], max_window=window + 1, has_window=True)
    if name == "PeriodReturn":
        require_arg_count(name, node.args, 2, 2)
        expr = compile_node(node.args[0], context)
        ensure_no_nested_window(name, expr)
        window = compile_window(node.args[1], context)
        lag_expr = f"lagInFrame({expr.sql}, {window}) {window_clause(context, window + 1, preceding=window)}"
        sql = f"if(isNull({lag_expr}) OR {lag_expr} = 0 OR isNull({expr.sql}), NULL, {expr.sql} / {lag_expr} - 1)"
        return merge_sql(sql, [expr], max_window=window + 1, has_window=True)
    if name == "FirstTrue":
        require_arg_count(name, node.args, 2, 2)
        expr = compile_node(node.args[0], context)
        ensure_no_nested_window(name, expr)
        window = compile_window(node.args[1], context)
        truth = truth_expr(expr.sql)
        if window <= 1:
            return merge_sql(f"toFloat64({truth})", [expr])
        previous_clause = (
            f"OVER (PARTITION BY {context.code_column} ORDER BY {context.date_column} "
            f"ROWS BETWEEN {window - 1} PRECEDING AND 1 PRECEDING)"
        )
        sql = f"if({truth} = 1 AND coalesce(sum({truth}) {previous_clause}, 0) = 0, 1.0, 0.0)"
        return merge_sql(sql, [expr], max_window=window, has_window=True)
    if name in ELEMENT_FUNCTIONS:
        sql_name, min_args, max_args = ELEMENT_FUNCTIONS[name]
        require_arg_count(name, node.args, min_args, max_args)
        args = [compile_node(arg, context) for arg in node.args]
        return merge_sql(f"{sql_name}({', '.join(arg.sql for arg in args)})", args)
    if name == "Fillna":
        require_arg_count(name, node.args, 2, 2)
        args = [compile_node(arg, context) for arg in node.args]
        return merge_sql(f"ifNull({args[0].sql}, {args[1].sql})", args)
    if name == "If":
        require_arg_count(name, node.args, 3, 3)
        args = [compile_node(arg, context) for arg in node.args]
        return merge_sql(f"if({args[0].sql}, {args[1].sql}, {args[2].sql})", args)
    if name in COMPARISON_FUNCTIONS:
        require_arg_count(name, node.args, 2, 2)
        left = compile_node(node.args[0], context)
        right = compile_node(node.args[1], context)
        return merge_sql(f"({left.sql} {COMPARISON_FUNCTIONS[name]} {right.sql})", [left, right])
    if name in {"And", "Or"}:
        require_arg_count(name, node.args, 2, 99)
        args = [compile_node(arg, context) for arg in node.args]
        op = " AND " if name == "And" else " OR "
        return merge_sql(f"({op.join(arg.sql for arg in args)})", args)
    if name == "Not":
        require_arg_count(name, node.args, 1, 1)
        expr = compile_node(node.args[0], context)
        return merge_sql(f"NOT ({expr.sql})", [expr])
    raise FormulaError(f"不支持的函数: {name}")


def canonical_function(name: str) -> str:
    return CANONICAL_FUNCTIONS.get(str(name or "").lower(), "")


def compile_window(node: Any, context: CompileContext) -> int:
    if isinstance(node, NumberNode):
        return positive_int(node.value, "window")
    if isinstance(node, VariableNode):
        value = context.params.get(node.name, 20 if node.name == "window" else None)
        return positive_int(value, node.name)
    raise FormulaError("窗口参数必须是数字或参数变量")


def compile_scalar_number(node: Any, context: CompileContext, label: str) -> str:
    if isinstance(node, NumberNode):
        return numeric_literal(node.value)
    if isinstance(node, VariableNode):
        value = context.params.get(node.name)
        if value is None:
            raise FormulaError(f"{label} 参数不存在: {node.name}")
        return numeric_literal(value)
    raise FormulaError(f"{label} 必须是数字或参数变量")


def window_clause(context: CompileContext, window: int, *, preceding: Optional[int] = None) -> str:
    preceding_rows = window - 1 if preceding is None else preceding
    return (
        f"OVER (PARTITION BY {context.code_column} ORDER BY {context.date_column} "
        f"ROWS BETWEEN {preceding_rows} PRECEDING AND CURRENT ROW)"
    )


def compile_ema(expr: SqlExpr, context: CompileContext, window: int) -> SqlExpr:
    alpha = 2 / (window + 1)
    terms = lag_terms(expr.sql, context, window)
    weighted = []
    weights = []
    for offset, term in enumerate(terms):
        weight = alpha * ((1 - alpha) ** offset)
        weighted.append(f"if(isNull({term}), 0, {term} * {weight:.12g})")
        weights.append(f"if(isNull({term}), 0, {weight:.12g})")
    sql = f"({join_sum(weighted)} / nullIf({join_sum(weights)}, 0))"
    return merge_sql(sql, [expr], max_window=window, has_window=True)


def compile_wma(expr: SqlExpr, context: CompileContext, window: int) -> SqlExpr:
    terms = lag_terms(expr.sql, context, window)
    weighted = []
    weights = []
    for offset, term in enumerate(terms):
        weight = window - offset
        weighted.append(f"if(isNull({term}), 0, {term} * {weight})")
        weights.append(f"if(isNull({term}), 0, {weight})")
    sql = f"({join_sum(weighted)} / nullIf({join_sum(weights)}, 0))"
    return merge_sql(sql, [expr], max_window=window, has_window=True)


def compile_mad(expr: SqlExpr, context: CompileContext, window: int) -> SqlExpr:
    terms = lag_terms(expr.sql, context, window)
    count = join_sum([f"if(isNull({term}), 0, 1)" for term in terms])
    sum_y = join_sum([f"if(isNull({term}), 0, {term})" for term in terms])
    mean = f"(({sum_y}) / nullIf({count}, 0))"
    abs_dev = [f"if(isNull({term}), 0, abs({term} - ({mean})))" for term in terms]
    sql = f"({join_sum(abs_dev)} / nullIf({count}, 0))"
    return merge_sql(sql, [expr], max_window=window, has_window=True)


def compile_rank(expr: SqlExpr, context: CompileContext, window: int) -> SqlExpr:
    terms = lag_terms(expr.sql, context, window)
    current = nullable_expr(expr.sql)
    numerator = join_sum([f"if(isNull({term}) OR isNull({current}), 0, if({term} <= {current}, 1, 0))" for term in terms])
    denominator = join_sum([f"if(isNull({term}), 0, 1)" for term in terms])
    sql = f"if(isNull({current}), NULL, {numerator} / nullIf({denominator}, 0))"
    return merge_sql(sql, [expr], max_window=window, has_window=True)


def compile_idx_extreme(expr: SqlExpr, context: CompileContext, window: int, *, is_max: bool) -> SqlExpr:
    terms = lag_terms(expr.sql, context, window)
    array_sql = f"[{', '.join(terms)}]"
    extreme = "arrayMax" if is_max else "arrayMin"
    found_index = f"arrayFirstIndex(x -> x = {extreme}({array_sql}), {array_sql})"
    count = join_sum([f"if(isNull({term}), 0, 1)" for term in terms])
    sql = f"if({found_index} = 0, NULL, ({count}) - ({found_index}) + 1)"
    return merge_sql(sql, [expr], max_window=window, has_window=True)


def compile_linear_regression(name: str, expr: SqlExpr, context: CompileContext, window: int) -> SqlExpr:
    terms = lag_terms(expr.sql, context, window)
    rows = [(window - offset, term) for offset, term in enumerate(terms)]
    count = join_sum([f"if(isNull({term}), 0, 1)" for _, term in rows])
    sum_x = join_sum([f"if(isNull({term}), 0, {x})" for x, term in rows])
    sum_y = join_sum([f"if(isNull({term}), 0, {term})" for _, term in rows])
    sum_xy = join_sum([f"if(isNull({term}), 0, {x} * {term})" for x, term in rows])
    sum_x2 = join_sum([f"if(isNull({term}), 0, {x * x})" for x, term in rows])
    sum_y2 = join_sum([f"if(isNull({term}), 0, {term} * {term})" for _, term in rows])
    covariance = f"(({count}) * ({sum_xy}) - ({sum_x}) * ({sum_y}))"
    x_variance = f"(({count}) * ({sum_x2}) - ({sum_x}) * ({sum_x}))"
    y_variance = f"(({count}) * ({sum_y2}) - ({sum_y}) * ({sum_y}))"
    slope = f"({covariance} / nullIf({x_variance}, 0))"
    if name == "Slope":
        sql = slope
    elif name == "Rsquare":
        sql = f"(({covariance}) * ({covariance}) / nullIf(({x_variance}) * ({y_variance}), 0))"
    else:
        intercept = f"((({sum_y}) - ({slope}) * ({sum_x})) / nullIf({count}, 0))"
        current_x = window
        current_y = nullable_expr(expr.sql)
        sql = f"({current_y} - (({slope}) * {current_x} + ({intercept})))"
    return merge_sql(sql, [expr], max_window=window, has_window=True)


def lag_terms(sql: str, context: CompileContext, window: int) -> list[str]:
    return [lag_sql(sql, offset, context) for offset in range(window)]


def lag_sql(sql: str, offset: int, context: CompileContext) -> str:
    if offset == 0:
        return nullable_expr(sql)
    return (
        f"lagInFrame({nullable_expr(sql)}, {offset}, NULL) "
        f"{window_clause(context, offset + 1, preceding=offset)}"
    )


def nullable_expr(sql: str) -> str:
    return f"toNullable({sql})"


def join_sum(items: list[str]) -> str:
    return " + ".join(f"({item})" for item in items) if items else "0"


def merge_sql(sql: str, exprs: list[SqlExpr], *, max_window: int = 1, has_window: bool = False) -> SqlExpr:
    fields = set()
    window = max_window
    contains_window = has_window
    for expr in exprs:
        fields.update(expr.fields)
        window = max(window, expr.max_window)
        contains_window = contains_window or expr.has_window
    return SqlExpr(sql=sql, fields=frozenset(fields), max_window=window, has_window=contains_window)


def ensure_no_nested_window(function_name: str, *exprs: SqlExpr) -> None:
    if any(expr.has_window for expr in exprs):
        raise FormulaError(f"{function_name} 暂不支持嵌套窗口函数")


def require_arg_count(name: str, args: list[Any], min_count: int, max_count: int) -> None:
    if len(args) < min_count or len(args) > max_count:
        if min_count == max_count:
            raise FormulaError(f"{name} 需要 {min_count} 个参数")
        raise FormulaError(f"{name} 需要 {min_count} 到 {max_count} 个参数")


def truth_expr(sql: str) -> str:
    return f"if(isNull({sql}), 0, if({sql} != 0, 1, 0))"


def numeric_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip()
    if not re.match(r"^-?\d+(?:\.\d+)?$", text):
        raise FormulaError(f"参数必须是数字: {value}")
    return text


def positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FormulaError(f"{label} 必须是正整数") from exc
    if parsed <= 0:
        raise FormulaError(f"{label} 必须是正整数")
    return parsed


def identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.match(value or ""):
        raise FormulaError(f"{label} 不是合法标识: {value}")
    return value
