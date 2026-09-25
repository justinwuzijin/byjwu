"""A small JSON Schema subset: validate, strip for the wire, and build a mock.

Creative answers from Opus arrive as JSON and are checked here before any of
it reaches a proposal. The subset is what Cut Conductor's schemas use:
``type``, ``enum``, ``const``, ``properties``, ``required``,
``additionalProperties``, ``items``, ``minItems``/``maxItems``,
``minimum``/``maximum``, ``minLength``/``maxLength``, and ``anyOf``.

Anthropic's structured outputs reject numeric and string-length constraints
and require ``additionalProperties: false`` with every property required.
:func:`wire` produces that form. :func:`validate` always checks the full
schema, so a constraint the wire could not carry is still enforced here.

Enum casing is not guaranteed by constrained decoding, so string enums match
case-insensitively and come back in the schema's spelling.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import ConductorError

_WIRE_DROP = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "maxItems",
    }
)


class SchemaError(ConductorError):
    """The value does not match the schema."""


def validate(value: Any, schema: Mapping[str, Any], path: str = "$") -> Any:
    """Return ``value`` (with enum casing normalised) or raise :class:`SchemaError`."""
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                return validate(value, option, path)
            except SchemaError as exc:
                errors.append(str(exc))
        raise SchemaError(f"{path}: matched no anyOf branch ({'; '.join(errors)[:200]})")
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: expected {schema['const']!r}")
    if "enum" in schema:
        value = _enum(value, schema["enum"], path)
    kind = schema.get("type")
    if kind is not None:
        kinds = [kind] if isinstance(kind, str) else list(kind)
        if not any(_is(value, item) for item in kinds):
            raise SchemaError(f"{path}: expected {'/'.join(kinds)}, got {type(value).__name__}")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: {value} is below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: {value} is above {schema['maximum']}")
        return value
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaError(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(f"{path}: longer than {schema['maxLength']}")
        return value
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaError(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path}: more than {schema['maxItems']} items")
        items = schema.get("items")
        if items is None:
            return list(value)
        return [validate(item, items, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                raise SchemaError(f"{path}: missing {name!r}")
        extra = [name for name in value if name not in properties]
        if extra and schema.get("additionalProperties") is False:
            raise SchemaError(f"{path}: unexpected {extra[0]!r}")
        return {
            name: validate(item, properties[name], f"{path}.{name}") if name in properties else item
            for name, item in value.items()
        }
    return value


def wire(schema: Mapping[str, Any]) -> dict:
    """The schema Anthropic accepts: no numeric/length limits, closed objects."""
    out: dict[str, Any] = {}
    for key, item in schema.items():
        if key in _WIRE_DROP:
            continue
        if key == "minItems" and item not in (0, 1):
            out[key] = 1
            continue
        if key == "properties":
            out[key] = {name: wire(sub) for name, sub in item.items()}
        elif key == "items":
            out[key] = wire(item)
        elif key == "anyOf":
            out[key] = [wire(sub) for sub in item]
        else:
            out[key] = item
    if _object_schema(out):
        out["additionalProperties"] = False
        out["required"] = list((out.get("properties") or {}).keys())
    return out


def example(schema: Mapping[str, Any]) -> Any:
    """A minimal instance that validates. The dry-run mock uses this."""
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return example(schema["anyOf"][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), kind[0])
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        return {name: example(properties[name]) for name in schema.get("required") or properties}
    if kind == "array":
        count = int(schema.get("minItems") or 0)
        return [example(schema.get("items") or {}) for _ in range(count)]
    if kind == "string":
        return "x" * int(schema.get("minLength") or 0)
    if kind in {"number", "integer"}:
        low = schema.get("minimum", 0)
        high = schema.get("maximum", low)
        value = low if low >= 0 or high < 0 else 0
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        return False
    return None


def _enum(value: Any, allowed: list, path: str) -> Any:
    if value in allowed:
        return value
    if isinstance(value, str):
        for option in allowed:
            if isinstance(option, str) and option.lower() == value.lower():
                return option
    raise SchemaError(f"{path}: {value!r} is not one of {allowed}")


def _is(value: Any, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return False


def _object_schema(schema: Mapping[str, Any]) -> bool:
    kind = schema.get("type")
    return kind == "object" or (isinstance(kind, list) and "object" in kind) or "properties" in schema
