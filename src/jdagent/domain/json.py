"""定义项目允许的 JSON 值，并在接收外部数据时检查类型。"""

from typing import TypeAlias, cast

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def normalize_json(value: object) -> JsonValue:
    """递归检查解码后的值，确保后续组件只接收受支持的 JSON 类型。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [normalize_json(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        converted: JsonObject = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            converted[key] = normalize_json(item)
        return converted
    raise ValueError(f"Unsupported JSON value: {type(value).__name__}")


def require_object(value: JsonValue, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def require_array(value: JsonValue, name: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def require_string(data: JsonObject, name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def optional_string(data: JsonObject, name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def require_integer(data: JsonObject, name: str, default: int | None = None) -> int:
    value = data.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value
