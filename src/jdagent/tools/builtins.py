"""The three built-in v0.1 tools."""

import ast
import asyncio
import math
from collections.abc import Callable
from pathlib import Path

from jdagent.domain.json import JsonObject, JsonValue
from jdagent.domain.tools import RiskLevel, ToolDefinition, ToolExecutionContext
from jdagent.tools.workspace import WorkspacePathResolver

BinaryOperation = Callable[[float, float], float]
UnaryOperation = Callable[[float], float]

_BINARY_OPERATIONS: dict[type[ast.operator], BinaryOperation] = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.FloorDiv: lambda left, right: left // right,
    ast.Mod: lambda left, right: left % right,
    ast.Pow: lambda left, right: left**right,
}
_UNARY_OPERATIONS: dict[type[ast.unaryop], UnaryOperation] = {
    ast.UAdd: lambda value: value,
    ast.USub: lambda value: -value,
}


def _required_string(arguments: JsonObject, name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            raise ValueError("Boolean values are not arithmetic operands")
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATIONS:
        return _UNARY_OPERATIONS[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATIONS:
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("Exponent is outside the safe range")
        result = _BINARY_OPERATIONS[type(node.op)](left, right)
        if not math.isfinite(result):
            raise ValueError("Result is not finite")
        return result
    raise ValueError("Expression contains an unsupported operation")


async def _calculate(arguments: JsonObject, context: ToolExecutionContext) -> str:
    del context
    expression = _required_string(arguments, "expression")
    if len(expression) > 200:
        raise ValueError("Expression is too long")
    result = _evaluate(ast.parse(expression, mode="eval"))
    return str(int(result)) if result.is_integer() else str(result)


def _path_preflight(
    resolver: WorkspacePathResolver,
    mode: str,
) -> Callable[[JsonObject, ToolExecutionContext], JsonObject]:
    def validate(arguments: JsonObject, context: ToolExecutionContext) -> JsonObject:
        del context
        requested = _required_string(arguments, "path")
        resolved = (
            resolver.resolve_read(requested)
            if mode == "read"
            else resolver.resolve_write(requested)
        )
        prepared = dict(arguments)
        prepared["path"] = str(resolved)
        return prepared

    return validate


async def _read_text_file(
    arguments: JsonObject,
    context: ToolExecutionContext,
    resolver: WorkspacePathResolver,
) -> str:
    del context
    path = resolver.resolve_read(_required_string(arguments, "path"))
    return await asyncio.to_thread(path.read_text, encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _write_text_file(
    arguments: JsonObject,
    context: ToolExecutionContext,
    resolver: WorkspacePathResolver,
) -> str:
    del context
    path = resolver.resolve_write(_required_string(arguments, "path"))
    content = _required_string(arguments, "content")
    await asyncio.to_thread(_write, path, content)
    return f"Wrote {len(content)} characters to {path.name}"


def _object_schema(properties: JsonObject, required: list[JsonValue]) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def create_builtin_tools(resolver: WorkspacePathResolver) -> tuple[ToolDefinition, ...]:
    """Create the fixed calculator/read/write tool set for one workspace."""

    async def read_handler(arguments: JsonObject, context: ToolExecutionContext) -> str:
        return await _read_text_file(arguments, context, resolver)

    async def write_handler(arguments: JsonObject, context: ToolExecutionContext) -> str:
        return await _write_text_file(arguments, context, resolver)

    return (
        ToolDefinition(
            "calculator",
            "Evaluate a basic arithmetic expression.",
            _object_schema(
                {"expression": {"type": "string", "maxLength": 200}},
                ["expression"],
            ),
            RiskLevel.PURE,
            _calculate,
        ),
        ToolDefinition(
            "read_text_file",
            "Read a UTF-8 text file inside the configured workspace.",
            _object_schema({"path": {"type": "string", "minLength": 1}}, ["path"]),
            RiskLevel.READ,
            read_handler,
            _path_preflight(resolver, "read"),
        ),
        ToolDefinition(
            "write_text_file",
            "Write UTF-8 text inside the configured workspace after approval.",
            _object_schema(
                {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                ["path", "content"],
            ),
            RiskLevel.WRITE,
            write_handler,
            _path_preflight(resolver, "write"),
        ),
    )
