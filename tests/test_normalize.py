from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from mcp4api.ingest.convert import _normalize_schema
from mcp4api.model import JsonObject, JsonSchema, JsonValue


def test_nullable_becomes_type_array() -> None:
    schema = _normalize_schema(
        {"type": "string", "nullable": True, "enum": ["a"], "example": "a"},
        dialect="3.0",
    )
    assert schema == {
        "type": ["string", "null"],
        "enum": ["a", None],
        "examples": ["a"],
    }
    Draft202012Validator(schema).validate("a")
    Draft202012Validator(schema).validate(None)


def test_nullable_without_type_is_dropped() -> None:
    assert _normalize_schema({"nullable": True, "description": "any"}, dialect="3.0") == {
        "description": "any"
    }


def test_nullable_combinator_allows_null() -> None:
    schema = _normalize_schema(
        {"allOf": [{"type": "string"}], "nullable": True},
        dialect="3.0",
    )
    assert schema == {"anyOf": [{"allOf": [{"type": "string"}]}, {"type": "null"}]}
    Draft202012Validator(schema).validate(None)
    Draft202012Validator(schema).validate("a")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"minimum": 1, "exclusiveMinimum": True}, {"exclusiveMinimum": 1}),
        ({"minimum": 1, "exclusiveMinimum": False}, {"minimum": 1}),
        ({"maximum": 2, "exclusiveMaximum": True}, {"exclusiveMaximum": 2}),
        ({"maximum": 2, "exclusiveMaximum": False}, {"maximum": 2}),
        ({"type": "number", "exclusiveMinimum": 1}, {"type": "number", "exclusiveMinimum": 1}),
    ],
)
def test_exclusive_bounds(raw: JsonObject, expected: JsonObject) -> None:
    assert _normalize_schema(raw, dialect="3.0") == expected


def test_schema_examples_merge() -> None:
    schema = _normalize_schema(
        {"type": "string", "examples": ["a"], "example": "b"},
        dialect="3.1",
    )
    assert schema == {"type": "string", "examples": ["a", "b"]}


def test_openapi_31_type_array_stays() -> None:
    raw: JsonObject = {"type": ["string", "null"]}
    assert _normalize_schema(raw, dialect="3.1") == raw


def test_ref_siblings_follow_dialect() -> None:
    def resolve(ref: str, pointer: str) -> JsonSchema:
        del ref, pointer
        return {"$ref": "#/defs/Pet"}

    original: JsonObject = {"$ref": "#/components/schemas/Pet", "description": "pet"}
    assert _normalize_schema(original, dialect="3.0", resolve_ref=resolve) == {"$ref": "#/defs/Pet"}
    assert _normalize_schema(original, dialect="3.1", resolve_ref=resolve) == {
        "allOf": [{"$ref": "#/defs/Pet"}, {"description": "pet"}]
    }


@settings(max_examples=20, deadline=None)
@given(
    kind=st.sampled_from(["string", "integer", "number", "boolean"]),
    text=st.text(max_size=12),
    number=st.integers(min_value=-20, max_value=20),
    flag=st.booleans(),
)
def test_nullable_accepts_value_and_null(kind: str, text: str, number: int, flag: bool) -> None:
    schema = _normalize_schema({"type": kind, "nullable": True}, dialect="3.0")
    if kind == "string":
        value: JsonValue = text
    elif kind == "boolean":
        value = flag
    elif kind == "integer":
        value = number
    else:
        value = float(number)
    validator = Draft202012Validator(schema)
    validator.validate(value)
    validator.validate(None)
