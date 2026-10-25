"""受限三值谓词：解析、类型校验与求值。

语法只允许：

    expr := or_expr
    or_expr := and_expr ("or" and_expr)*
    and_expr := unary ("and" unary)*
    unary := "not" unary | comparison | "(" expr ")"
    comparison := path op literal | path "in" "[" literal ("," literal)* "]"
    path := "slots." IDENT | "constants." IDENT | "today"
    op := "==" | "!=" | ">" | ">=" | "<" | "<="

不使用 eval/exec；未注册槽位、类型不匹配在加载期报错。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from campusguide.domain.models import SlotDefinition

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<date>\d{4}-\d{2}-\d{2})(?!\d)
  | (?P<number>\d+)
  | (?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<op>==|!=|>=|<=|>|<)
  | (?P<punct>[()\[\],])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
    """,
    re.VERBOSE,
)


class PredicateError(ValueError):
    """谓词语法或类型错误。"""


class Truth(Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    def as_bool(self) -> bool | None:
        if self is Truth.TRUE:
            return True
        if self is Truth.FALSE:
            return False
        return None


@dataclass(frozen=True)
class Literal:
    value: str | int | bool | date


@dataclass(frozen=True)
class ListLiteral:
    items: tuple[Literal, ...]


@dataclass(frozen=True)
class Path:
    kind: str  # slots | constants | today
    name: str | None = None


@dataclass(frozen=True)
class Compare:
    left: Path
    op: str
    right: Literal | ListLiteral


@dataclass(frozen=True)
class Not:
    operand: Expr


@dataclass(frozen=True)
class And:
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Or:
    left: Expr
    right: Expr


Expr = Compare | Not | And | Or


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int


def _tokenize(expression: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(expression):
        match = _TOKEN_RE.match(expression, pos)
        if not match:
            raise PredicateError(f"无法识别的字符：{expression[pos : pos + 10]!r}")
        pos = match.end()
        kind = match.lastgroup or ""
        value = match.group(0)
        if kind == "ws":
            continue
        tokens.append(Token(kind=kind, value=value, pos=match.start()))
    tokens.append(Token(kind="eof", value="", pos=len(expression)))
    return tokens


class _Parser:
    def __init__(self, expression: str) -> None:
        self.expression = expression
        self.tokens = _tokenize(expression)
        self.index = 0

    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current()
        self.index += 1
        return token

    def accept(self, value: str) -> bool:
        token = self.current()
        if token.kind in {"op", "punct"} and token.value == value:
            self.advance()
            return True
        return False

    def expect(self, value: str) -> Token:
        token = self.current()
        if token.kind in {"op", "punct"} and token.value == value:
            return self.advance()
        raise PredicateError(f"期望 {value!r}，实际 {token.value!r}（位置 {token.pos}）")

    def parse(self) -> Expr:
        if self.current().kind == "eof":
            raise PredicateError("谓词不能为空")
        expr = self.parse_or()
        if self.current().kind != "eof":
            token = self.current()
            raise PredicateError(f"多余内容：{token.value!r}（位置 {token.pos}）")
        return expr

    def parse_or(self) -> Expr:
        expr = self.parse_and()
        while self.current().kind == "ident" and self.current().value.lower() == "or":
            self.advance()
            expr = Or(left=expr, right=self.parse_and())
        return expr

    def parse_and(self) -> Expr:
        expr = self.parse_unary()
        while self.current().kind == "ident" and self.current().value.lower() == "and":
            self.advance()
            expr = And(left=expr, right=self.parse_unary())
        return expr

    def parse_unary(self) -> Expr:
        if self.current().kind == "ident" and self.current().value.lower() == "not":
            self.advance()
            return Not(operand=self.parse_unary())
        if self.current().kind == "punct" and self.current().value == "(":
            self.advance()
            expr = self.parse_or()
            self.expect(")")
            return expr
        return self.parse_comparison()

    def parse_comparison(self) -> Expr:
        path = self.parse_path()
        token = self.current()
        if token.kind == "ident" and token.value.lower() == "in":
            self.advance()
            self.expect("[")
            items: list[Literal] = []
            if not (self.current().kind == "punct" and self.current().value == "]"):
                items.append(self.parse_literal())
                while self.accept(","):
                    items.append(self.parse_literal())
            self.expect("]")
            if not items:
                raise PredicateError("in 右侧列表不能为空")
            return Compare(left=path, op="in", right=ListLiteral(tuple(items)))
        if token.kind != "op":
            raise PredicateError(f"期望比较运算符，实际 {token.value!r}（位置 {token.pos}）")
        self.advance()
        return Compare(left=path, op=token.value, right=self.parse_literal())

    def parse_path(self) -> Path:
        token = self.current()
        if token.kind != "ident":
            raise PredicateError(f"期望 slots./constants./today，实际 {token.value!r}")
        self.advance()
        name = token.value
        if name == "today":
            return Path(kind="today")
        if "." not in name:
            raise PredicateError(f"路径必须带前缀：{name!r}")
        prefix, suffix = name.split(".", 1)
        if prefix not in {"slots", "constants"} or not suffix:
            raise PredicateError(f"非法路径：{name!r}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", suffix):
            raise PredicateError(f"非法字段名：{suffix!r}")
        return Path(kind=prefix, name=suffix)

    def parse_literal(self) -> Literal:
        token = self.current()
        if token.kind == "date":
            self.advance()
            try:
                return Literal(date.fromisoformat(token.value))
            except ValueError as exc:
                raise PredicateError(f"非法日期：{token.value!r}") from exc
        if token.kind == "number":
            self.advance()
            return Literal(int(token.value))
        if token.kind == "string":
            self.advance()
            return Literal(_unquote(token.value))
        if token.kind == "ident" and token.value.lower() in {"true", "false"}:
            self.advance()
            return Literal(token.value.lower() == "true")
        raise PredicateError(f"期望字面量，实际 {token.value!r}（位置 {token.pos}）")


def _unquote(raw: str) -> str:
    quote = raw[0]
    body = raw[1:-1]
    # 只支持常见转义；不引入 Python 字符串语义。
    return body.replace(f"\\{quote}", quote).replace("\\\\", "\\")


def parse_predicate(expression: str) -> Expr:
    return _Parser(expression.strip()).parse()


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, date):
        return "date"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _slot_type(slots: Mapping[str, SlotDefinition], name: str) -> str:
    definition = slots.get(name)
    if definition is None:
        raise PredicateError(f"未定义槽位：slots.{name}")
    return definition.type


def _check_literal_type(expected: str, literal: Literal, *, where: str) -> None:
    actual = _type_name(literal.value)
    if expected == "enum":
        expected = "string"
    if expected == "integer" and actual == "integer":
        return
    if expected == "string" and actual == "string":
        return
    if expected == "boolean" and actual == "boolean":
        return
    if expected == "date" and actual == "date":
        return
    raise PredicateError(f"{where} 类型不匹配：期望 {expected}，实际 {actual}")


def validate_predicate(
    expression: str,
    slots: Mapping[str, SlotDefinition],
    constants: Mapping[str, str] | None = None,
) -> Expr:
    """解析并做加载期类型校验。"""
    expr = parse_predicate(expression)
    _validate_expr(expr, slots, constants or {})
    return expr


def _validate_expr(
    expr: Expr,
    slots: Mapping[str, SlotDefinition],
    constants: Mapping[str, str],
) -> None:
    if isinstance(expr, (And, Or)):
        _validate_expr(expr.left, slots, constants)
        _validate_expr(expr.right, slots, constants)
        return
    if isinstance(expr, Not):
        _validate_expr(expr.operand, slots, constants)
        return

    left = expr.left
    if left.kind == "slots":
        expected = _slot_type(slots, left.name or "")
    elif left.kind == "constants":
        if left.name not in constants:
            raise PredicateError(f"未定义常量：constants.{left.name}")
        expected = constants[left.name]
    else:
        expected = "date"

    if isinstance(expr.right, ListLiteral):
        if expr.op != "in":
            raise PredicateError("列表只允许用于 in")
        for item in expr.right.items:
            _check_literal_type(expected, item, where=f"{left.kind}.{left.name or 'today'}")
        return

    _check_literal_type(expected, expr.right, where=f"{left.kind}.{left.name or 'today'}")
    if expr.op in {">", ">=", "<", "<="} and expected not in {"integer", "date"}:
        raise PredicateError(f"运算符 {expr.op} 只支持 integer/date，实际 {expected}")


def _resolve_path(
    path: Path,
    slots: Mapping[str, Any],
    today: date,
    constants: Mapping[str, Any],
) -> Any:
    if path.kind == "today":
        return today
    if path.kind == "constants":
        return constants.get(path.name or "")
    if path.kind == "slots":
        value = slots.get(path.name or "")
        # 兼容 SlotValue；None 和缺失都表示未知。
        return getattr(value, "value", value)
    raise PredicateError(f"非法路径：{path}")


def _compare(left: Any, op: str, right: Any) -> Truth:
    if left is None or right is None:
        return Truth.UNKNOWN
    left_type = _type_name(left)
    right_type = _type_name(right)
    if left_type != right_type:
        # bool 是 int 的子类，_type_name 已先判 bool。
        raise PredicateError(f"运行时类型不匹配：{left_type} {op} {right_type}")
    if op == "==":
        result = left == right
    elif op == "!=":
        result = left != right
    elif op == ">":
        result = left > right
    elif op == ">=":
        result = left >= right
    elif op == "<":
        result = left < right
    elif op == "<=":
        result = left <= right
    else:  # pragma: no cover - parser 已限制
        raise PredicateError(f"未知运算符：{op}")
    return Truth.TRUE if result else Truth.FALSE


def evaluate_predicate(
    expr: Expr,
    slots: Mapping[str, Any],
    *,
    today: date,
    constants: Mapping[str, Any] | None = None,
) -> Truth:
    constants = constants or {}
    if isinstance(expr, Or):
        left = evaluate_predicate(expr.left, slots, today=today, constants=constants)
        if left is Truth.TRUE:
            return Truth.TRUE
        right = evaluate_predicate(expr.right, slots, today=today, constants=constants)
        if right is Truth.TRUE:
            return Truth.TRUE
        if left is Truth.UNKNOWN or right is Truth.UNKNOWN:
            return Truth.UNKNOWN
        return Truth.FALSE
    if isinstance(expr, And):
        left = evaluate_predicate(expr.left, slots, today=today, constants=constants)
        if left is Truth.FALSE:
            return Truth.FALSE
        right = evaluate_predicate(expr.right, slots, today=today, constants=constants)
        if right is Truth.FALSE:
            return Truth.FALSE
        if left is Truth.UNKNOWN or right is Truth.UNKNOWN:
            return Truth.UNKNOWN
        return Truth.TRUE
    if isinstance(expr, Not):
        value = evaluate_predicate(expr.operand, slots, today=today, constants=constants)
        if value is Truth.UNKNOWN:
            return Truth.UNKNOWN
        return Truth.FALSE if value is Truth.TRUE else Truth.TRUE

    left_value = _resolve_path(expr.left, slots, today, constants)
    if isinstance(expr.right, ListLiteral):
        if left_value is None:
            return Truth.UNKNOWN
        values = [item.value for item in expr.right.items]
        if not values:
            return Truth.FALSE
        left_type = _type_name(left_value)
        if any(_type_name(item) != left_type for item in values):
            raise PredicateError("in 列表元素类型不一致")
        return Truth.TRUE if left_value in values else Truth.FALSE
    return _compare(left_value, expr.op, expr.right.value)


def evaluate_bool(
    expr: Expr,
    slots: Mapping[str, Any],
    *,
    today: date,
    constants: Mapping[str, Any] | None = None,
) -> bool | None:
    return evaluate_predicate(expr, slots, today=today, constants=constants).as_bool()


def slot_names(expr: Expr) -> set[str]:
    if isinstance(expr, Compare):
        return {expr.left.name} if expr.left.kind == "slots" and expr.left.name else set()
    if isinstance(expr, Not):
        return slot_names(expr.operand)
    return slot_names(expr.left) | slot_names(expr.right)
