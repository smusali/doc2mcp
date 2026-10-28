from __future__ import annotations

import json
import keyword
import math
import re
from dataclasses import dataclass

from mcp4api.model import JsonObject, JsonSchema, JsonValue

_LINE = 100
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_DOC_KEYS = frozenset(
    {
        "$comment",
        "$id",
        "$schema",
        "contentEncoding",
        "contentMediaType",
        "default",
        "deprecated",
        "description",
        "discriminator",
        "example",
        "examples",
        "externalDocs",
        "format",
        "readOnly",
        "title",
        "writeOnly",
        "xml",
    }
)


_CONSTRAINT_KEYS = frozenset(
    {
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
    }
)


_CORE_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "items",
        "nullable",
        "properties",
        "required",
        "type",
    }
)


_RESERVED_MODELS = frozenset({"Annotated", "BaseModel", "ConfigDict", "Field", "Literal"})


class _SchemaProblem(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class _Type:
    annotation: str
    nullable: bool
    constraints: str
    model: str | None


class _Catalog:
    def __init__(self, spec_defs: dict[str, JsonObject]) -> None:
        self._spec_defs = spec_defs
        self._taken = set(_RESERVED_MODELS)
        self._order: list[str] = []
        self._fields: dict[str, tuple[list[str], bool]] = {}
        self._by_body: dict[str, str] = {}
        self._refs: dict[str, _Type] = {}
        self._building: set[str] = set()
        self._finished: set[str] = set()
        self._cyclic: set[str] = set()
        self._ref_stack: set[str] = set()
        self.needs_annotated = False
        self.needs_literal = False
        self.needs_field = False
        self.needs_config = False

    def _reserve(self, base: str) -> str:
        name = base or "Model"
        if name[0].isdigit():
            name = f"Model{name}"
        if keyword.iskeyword(name):
            name = f"{name}_"
        candidate = name
        index = 2
        while candidate in self._taken:
            candidate = f"{name}{index}"
            index += 1
        self._taken.add(candidate)
        return candidate

    @property
    def classes(self) -> set[str]:
        return set(self._finished)

    def compile_output(self, schema: JsonSchema, tool_name: str) -> _Type:
        hint = f"{_pascal(tool_name)}Result"
        if isinstance(schema, dict) and _named_object(schema):
            return self._compile(schema, hint=hint, model_name=self._reserve(hint), defs={})
        return self._compile(schema, hint=hint, model_name=None, defs={})

    def compile_input(self, schema: JsonSchema, tool_name: str, param: str) -> _Type:
        hint = f"{_pascal(tool_name)}{_pascal(param)}"
        return self._compile(schema, hint=hint, model_name=None, defs={})

    def source(self) -> str:
        if not self._order:
            return ""
        lines = ["from __future__ import annotations", ""]
        typing_names: list[str] = []
        if self.needs_annotated:
            typing_names.append("Annotated")
        if self.needs_literal:
            typing_names.append("Literal")
        if typing_names:
            lines.extend([f"from typing import {', '.join(typing_names)}", ""])
        pydantic = ["BaseModel"]
        if self.needs_config:
            pydantic.append("ConfigDict")
        if self.needs_field:
            pydantic.append("Field")
        lines.extend([f"from pydantic import {', '.join(pydantic)}", "", ""])
        for name in self._order:
            fields, forbid = self._fields[name]
            lines.extend(_class_lines(name, fields, forbid))
            lines.extend(["", ""])
        while lines and lines[-1] == "":
            lines.pop()
        if self._cyclic:
            lines.extend(["", ""])
            for name in self._order:
                if name in self._cyclic:
                    lines.append(f"{name}.model_rebuild()")
        return "\n".join(lines)

    def _compile(
        self,
        schema: JsonSchema,
        *,
        hint: str,
        model_name: str | None,
        defs: dict[str, JsonObject],
    ) -> _Type:
        if isinstance(schema, bool) or not isinstance(schema, dict):
            raise _SchemaProblem("boolean schema")
        _reject(schema)
        local = _child_defs(defs, schema)
        peeled, nullable = _peel_null(schema)
        if peeled is not schema:
            return _nullify(
                self._compile(peeled, hint=hint, model_name=model_name, defs=local), nullable
            )
        if "$ref" in schema:
            if _ref_blocked(schema):
                raise _SchemaProblem("ref is combined with other keywords")
            ref = schema.get("$ref")
            if not isinstance(ref, str):
                raise _SchemaProblem("ref is not a string")
            inner = self._compile_ref(ref, local, hint)
            return _nullify(_with_constraints(inner, _constraints(schema)), nullable)
        if "allOf" in schema:
            if _combined(schema, "allOf"):
                raise _SchemaProblem("allOf is combined with other keywords")
            merged, merged_defs = self._merge_all(schema.get("allOf"), local)
            return _nullify(
                self._object(merged, hint=hint, model_name=model_name, defs=merged_defs),
                nullable,
            )
        if "enum" in schema:
            return _nullify(self._enum(schema), nullable)
        if "const" in schema:
            return _nullify(self._const(schema), nullable)
        if _is_object(schema):
            return _nullify(
                self._object(schema, hint=hint, model_name=model_name, defs=local), nullable
            )
        if schema.get("type") == "array":
            return _nullify(self._array(schema, hint=hint, defs=local), nullable)
        return _nullify(self._scalar(schema), nullable)

    def _compile_ref(self, ref: str, defs: dict[str, JsonObject], hint: str) -> _Type:
        cached = self._refs.get(ref)
        if cached is not None:
            if cached.model is not None and cached.model in self._building:
                self._cyclic.add(cached.model)
            return cached
        if ref in self._ref_stack:
            raise _SchemaProblem(f"cyclic ref {ref}")
        body = _lookup(ref, defs, self._spec_defs)
        local = _child_defs(defs, body)
        if _is_object(body) and "$ref" not in body and "allOf" not in body:
            key = _canonical(body)
            found = self._by_body.get(key)
            if found is not None:
                typ = _Type(found, False, "", found)
                self._refs[ref] = typ
                if found in self._building:
                    self._cyclic.add(found)
                return typ
            name = self._reserve(_def_name(ref))
            self._by_body[key] = name
            self._refs[ref] = _Type(name, False, "", name)
            compiled = self._object(body, hint=hint, model_name=name, defs=local)
            self._refs[ref] = compiled
            return compiled
        self._ref_stack.add(ref)
        try:
            compiled = self._compile(body, hint=_def_name(ref), model_name=None, defs=local)
        finally:
            self._ref_stack.discard(ref)
        self._refs[ref] = compiled
        return compiled

    def _object(
        self,
        schema: JsonObject,
        *,
        hint: str,
        model_name: str | None,
        defs: dict[str, JsonObject],
    ) -> _Type:
        if model_name is None:
            key = _canonical(schema)
            found = self._by_body.get(key)
            if found is not None:
                if found in self._building:
                    self._cyclic.add(found)
                return _Type(found, False, "", found)
            name = self._reserve(_pascal(hint))
            self._by_body[key] = name
        else:
            name = model_name
            if name in self._finished:
                return _Type(name, False, "", name)
        if name in self._building:
            self._cyclic.add(name)
            return _Type(name, False, "", name)
        self._building.add(name)
        try:
            fields, forbid = self._properties(schema, owner=name, defs=defs)
        finally:
            self._building.discard(name)
        self._finished.add(name)
        self._order.append(name)
        self._fields[name] = (fields, forbid)
        if forbid:
            self.needs_config = True
        return _Type(name, False, "", name)

    def _properties(
        self, schema: JsonObject, *, owner: str, defs: dict[str, JsonObject]
    ) -> tuple[list[str], bool]:
        if "additionalProperties" in schema and schema.get("additionalProperties") is not False:
            if schema.get("additionalProperties") is True:
                raise _SchemaProblem("additionalProperties is true")
            raise _SchemaProblem("additionalProperties schema is not supported")
        raw_props = schema.get("properties", {})
        if raw_props is None:
            raw_props = {}
        props = _as_object(raw_props, "properties")
        required = _required(schema)
        fields: list[str] = []
        for name, prop in props.items():
            prop_schema = _as_object(prop, "property schema")
            description = prop_schema.get("description")
            text = description if isinstance(description, str) else ""
            typ = self._compile(
                prop_schema, hint=f"{owner}{_pascal(name)}", model_name=None, defs=defs
            )
            fields.append(self._field(name, typ, required=name in required, description=text))
        return fields, schema.get("additionalProperties") is False

    def _field(self, name: str, typ: _Type, *, required: bool, description: str) -> str:
        py_name = _ident(name)
        alias = name if py_name != name else None
        annotation = typ.annotation
        if not required and not typ.nullable and annotation != "None":
            annotation = f"{annotation} | None"
        parts: list[str] = []
        if description:
            parts.append(f"description={_py(description)}")
        if alias is not None:
            parts.append(f"alias={_py(alias)}")
        if typ.constraints:
            parts.append(typ.constraints)
        default = "" if required else " = None"
        if not parts:
            return f"{py_name}: {annotation}{default}"
        self.needs_field = True
        self.needs_annotated = True
        rendered = f"Field({', '.join(parts)})"
        one = f"{py_name}: Annotated[{annotation}, {rendered}]{default}"
        if len(one) + 4 <= _LINE:
            return one
        return "\n".join(
            [f"{py_name}: Annotated[", f"    {annotation},", f"    {rendered},", f"]{default}"]
        )

    def _array(self, schema: JsonObject, *, hint: str, defs: dict[str, JsonObject]) -> _Type:
        items = schema.get("items")
        if isinstance(items, list) or not isinstance(items, dict):
            raise _SchemaProblem("array items are not supported")
        item = self._compile(items, hint=f"{hint}Item", model_name=None, defs=defs)
        return _Type(f"list[{item.annotation}]", False, _constraints(schema), None)

    def _enum(self, schema: JsonObject) -> _Type:
        raw = schema.get("enum")
        if not isinstance(raw, list) or not raw:
            raise _SchemaProblem("enum is empty")
        values: list[JsonValue] = []
        nullable = False
        for item in raw:
            if item is None:
                nullable = True
                continue
            if not _scalar(item):
                raise _SchemaProblem("enum value is not a scalar")
            values.append(item)
        self.needs_literal = True
        if not values:
            return _Type("None", True, _constraints(schema), None)
        annotation = "Literal[" + ", ".join(_py(item) for item in values) + "]"
        if nullable:
            annotation = f"{annotation} | None"
        return _Type(annotation, nullable, _constraints(schema), None)

    def _const(self, schema: JsonObject) -> _Type:
        if "const" not in schema:
            raise _SchemaProblem("const is missing")
        value = schema.get("const")
        if not _scalar(value):
            raise _SchemaProblem("const value is not a scalar")
        self.needs_literal = True
        if value is None:
            return _Type("None", True, "", None)
        return _Type(f"Literal[{_py(value)}]", False, _constraints(schema), None)

    def _scalar(self, schema: JsonObject) -> _Type:
        for key in ("properties", "required", "additionalProperties", "items"):
            if key in schema:
                raise _SchemaProblem(f"keyword {key} is not supported")
        kind = schema.get("type")
        constraints = _constraints(schema)
        if kind == "string":
            return _Type("str", False, constraints, None)
        if kind == "integer":
            return _Type("int", False, constraints, None)
        if kind == "number":
            return _Type("float", False, constraints, None)
        if kind == "boolean":
            return _Type("bool", False, constraints, None)
        if kind == "null":
            return _Type("None", True, "", None)
        raise _SchemaProblem("schema is not supported")

    def _merge_all(
        self, branches: object, defs: dict[str, JsonObject]
    ) -> tuple[JsonObject, dict[str, JsonObject]]:
        if not isinstance(branches, list) or not branches:
            raise _SchemaProblem("allOf is empty")
        properties: JsonObject = {}
        required: list[JsonValue] = []
        seen: set[str] = set()
        forbid = False
        accumulated = dict(defs)
        for branch in branches:
            flat, accumulated = self._flatten_object(branch, accumulated, set())
            branch_props = flat.get("properties", {})
            if isinstance(branch_props, dict):
                for key, value in branch_props.items():
                    if key in properties and properties[key] != value:
                        raise _SchemaProblem(f"allOf property {key} conflicts")
                    properties[key] = value
            for name in _required(flat):
                if name not in seen:
                    seen.add(name)
                    required.append(name)
            if flat.get("additionalProperties") is False:
                forbid = True
        merged: JsonObject = {"type": "object", "properties": properties, "required": required}
        if forbid:
            merged["additionalProperties"] = False
        return merged, accumulated

    def _flatten_object(
        self,
        schema: object,
        defs: dict[str, JsonObject],
        seen: set[str],
    ) -> tuple[JsonObject, dict[str, JsonObject]]:
        if not isinstance(schema, dict):
            raise _SchemaProblem("allOf branch is not an object")
        _reject(schema)
        local = _child_defs(defs, schema)
        peeled, nullable = _peel_null(schema)
        if nullable:
            raise _SchemaProblem("allOf branch is nullable")
        current = peeled
        if "$ref" in current:
            ref = current.get("$ref")
            if not isinstance(ref, str):
                raise _SchemaProblem("ref is not a string")
            if ref in seen:
                raise _SchemaProblem("cyclic allOf")
            return self._flatten_object(_lookup(ref, local, self._spec_defs), local, seen | {ref})
        if "allOf" in current:
            merged, merged_defs = self._merge_all(current.get("allOf"), local)
            return merged, merged_defs
        if not _is_object(current):
            raise _SchemaProblem("allOf branch is not an object")
        return current, local


def _class_lines(name: str, fields: list[str], forbid: bool) -> list[str]:
    lines = [f"class {name}(BaseModel):"]
    if forbid:
        lines.append('    model_config = ConfigDict(extra="forbid")')
    if not fields:
        if not forbid:
            lines.append("    pass")
        return lines
    if forbid:
        lines.append("")
    for field_line in fields:
        lines.extend(f"    {line}" for line in field_line.split("\n"))
    return lines


def _named_object(schema: JsonObject) -> bool:
    current = schema
    for _ in range(32):
        if "$ref" in current:
            return False
        peeled, _nullable = _peel_null(current)
        if peeled is not current:
            current = peeled
            continue
        if "allOf" in current:
            return True
        return _is_object(current)
    return False


def _is_object(schema: JsonObject) -> bool:
    if schema.get("type") == "object":
        return True
    return "properties" in schema and "type" not in schema and "$ref" not in schema


def _peel_null(schema: JsonObject) -> tuple[JsonObject, bool]:
    nullable = schema.get("nullable") is True
    if "anyOf" in schema:
        if _combined(schema, "anyOf"):
            raise _SchemaProblem("anyOf is combined with other keywords")
        options = schema.get("anyOf")
        if not isinstance(options, list):
            raise _SchemaProblem("anyOf is not a list")
        branches = [item for item in options if not _is_null_schema(item)]
        nulls = [item for item in options if _is_null_schema(item)]
        if len(branches) != 1 or not nulls or len(branches) + len(nulls) != len(options):
            raise _SchemaProblem("anyOf is not a null union")
        branch = branches[0]
        if not isinstance(branch, dict):
            raise _SchemaProblem("anyOf branch is not an object")
        return branch, True
    kinds = schema.get("type")
    if isinstance(kinds, list):
        if not kinds or any(not isinstance(item, str) for item in kinds):
            raise _SchemaProblem("type union is not supported")
        rest = [item for item in kinds if item != "null"]
        if "null" not in kinds or len(rest) != 1:
            raise _SchemaProblem("type union is not supported")
        rewritten: JsonObject = {}
        for key, value in schema.items():
            if key != "type":
                rewritten[key] = value
        rewritten["type"] = rest[0]
        return rewritten, True
    return schema, nullable


def _is_null_schema(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") == "null":
        return True
    return "const" in value and value.get("const") is None


def _reject(schema: JsonObject) -> None:
    for key in schema:
        if key not in _DOC_KEYS and key not in _CONSTRAINT_KEYS and key not in _CORE_KEYS:
            raise _SchemaProblem(f"keyword {key} is not supported")


def _combined(schema: JsonObject, key: str) -> bool:
    allowed = _DOC_KEYS | {key, "$defs", "nullable"}
    return any(item not in allowed for item in schema)


def _ref_blocked(schema: JsonObject) -> bool:
    allowed = _DOC_KEYS | _CONSTRAINT_KEYS | {"$ref", "$defs"}
    return any(key not in allowed for key in schema)


def _constraints(schema: JsonObject) -> str:
    parts: list[str] = []
    _number_bound(schema, "minimum", "ge", parts)
    _number_bound(schema, "maximum", "le", parts)
    _number_bound(schema, "exclusiveMinimum", "gt", parts)
    _number_bound(schema, "exclusiveMaximum", "lt", parts)
    _int_bound(schema, "minLength", "min_length", parts)
    _int_bound(schema, "maxLength", "max_length", parts)
    _int_bound(schema, "minItems", "min_items", parts)
    _int_bound(schema, "maxItems", "max_items", parts)
    if "pattern" in schema:
        pattern = schema.get("pattern")
        if not isinstance(pattern, str):
            raise _SchemaProblem("pattern is not a string")
        parts.append(f"pattern={_py(pattern)}")
    return ", ".join(parts)


def _number_bound(schema: JsonObject, key: str, flag: str, parts: list[str]) -> None:
    if key not in schema:
        return
    value = schema.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _SchemaProblem(f"{key} is not a number")
    parts.append(f"{flag}={_number(value)}")


def _int_bound(schema: JsonObject, key: str, flag: str, parts: list[str]) -> None:
    if key not in schema:
        return
    value = schema.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _SchemaProblem(f"{key} is not an integer")
    parts.append(f"{flag}={value}")


def _number(value: int | float) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    raise _SchemaProblem("constraint is not a finite number")


def _required(schema: JsonObject) -> set[str]:
    raw = schema.get("required", [])
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise _SchemaProblem("required is not a list")
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise _SchemaProblem("required entry is not a string")
        names.add(item)
    return names


def _as_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise _SchemaProblem(f"{label} is not an object")
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise _SchemaProblem(f"{label} name is not a string")
        result[key] = item
    return result


def _child_defs(parent: dict[str, JsonObject], schema: JsonObject) -> dict[str, JsonObject]:
    raw = schema.get("$defs")
    if raw is None:
        return parent
    if not isinstance(raw, dict):
        raise _SchemaProblem("$defs is not an object")
    if not raw:
        return parent
    child = dict(parent)
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise _SchemaProblem("$defs entries must be schemas")
        child[key] = value
    return child


def _lookup(ref: str, defs: dict[str, JsonObject], spec_defs: dict[str, JsonObject]) -> JsonObject:
    if ref.startswith("#/$defs/"):
        body = defs.get(_unescape(ref[len("#/$defs/") :]))
    elif ref.startswith("#/defs/"):
        body = spec_defs.get(_unescape(ref[len("#/defs/") :]))
    else:
        raise _SchemaProblem(f"unsupported ref {ref}")
    if not isinstance(body, dict):
        raise _SchemaProblem(f"unknown ref {ref}")
    return body


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _def_name(ref: str) -> str:
    raw = _unescape(ref.rsplit("/", 1)[-1])
    segment = raw.rsplit("_", 1)[-1]
    return _pascal(segment or raw)


def _canonical(schema: JsonObject) -> str:
    return json.dumps(_strip(schema), sort_keys=True, ensure_ascii=False)


def _strip(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: _strip(item)
            for key, item in value.items()
            if key not in _DOC_KEYS and key != "$defs"
        }
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def _nullify(typ: _Type, nullable: bool) -> _Type:
    if not nullable or typ.nullable or typ.annotation == "None":
        return typ
    return _Type(f"{typ.annotation} | None", True, typ.constraints, typ.model)


def _with_constraints(typ: _Type, extra: str) -> _Type:
    if not extra:
        return typ
    if not typ.constraints:
        return _Type(typ.annotation, typ.nullable, extra, typ.model)
    return _Type(typ.annotation, typ.nullable, f"{typ.constraints}, {extra}", typ.model)


def _scalar(value: object) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _py(value: JsonValue) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _SchemaProblem("number is not finite")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise _SchemaProblem("value is not a scalar")


def _pascal(text: str) -> str:
    parts = re.findall(r"[0-9A-Za-z]+", text)
    if not parts:
        return "Model"
    name = "".join(part[:1].upper() + part[1:] for part in parts)
    if name[0].isdigit():
        name = f"Model{name}"
    if keyword.iskeyword(name):
        name = f"{name}_"
    return name


def _ident(name: str) -> str:
    if _IDENT.match(name) and not keyword.iskeyword(name):
        return name
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"param_{cleaned}" if cleaned else "param"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"
    return cleaned
