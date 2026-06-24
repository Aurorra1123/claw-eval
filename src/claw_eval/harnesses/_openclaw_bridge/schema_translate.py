"""JSON Schema -> TypeBox expression translator.

Phase 3 Wave 2 §6.3 — see ``docs/harness_design.md``.

Maps the subset of JSON Schema actually used by ``task.yaml`` ``input_schema``
fields to TypeBox expressions that compile under ``@sinclair/typebox`` (the
``typebox`` npm package). The output is a TypeScript expression string, ready
to be embedded as the ``parameters: ...`` field of a ``defineToolPlugin`` tool.

Supported subset (must match the bullet list in §6.3 step 3):

* ``type: string`` -> ``Type.String()``
* ``type: integer`` -> ``Type.Integer()``
* ``type: number`` -> ``Type.Number()``
* ``type: boolean`` -> ``Type.Boolean()``
* ``type: null`` -> ``Type.Null()``
* ``type: array`` with ``items: <schema>`` -> ``Type.Array(<items>)``
* ``type: object`` with ``properties`` + ``required`` -> ``Type.Object({...})``
* ``enum: [...]`` -> ``Type.Union([Type.Literal(v), ...])``
* ``description: "..."`` is preserved as TypeBox annotation
  (``Type.String({ description: "..." })``)
* ``required: [...]`` flips non-required fields to ``Type.Optional(<schema>)``
* Empty / missing ``input_schema`` -> ``Type.Object({})`` (no-arg tool)

Anything outside that subset raises ``NotImplementedError`` with a message
naming the offending construct. Preflight in later waves catches these and
rejects the task at the harness boundary.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "json_schema_to_typebox",
    "SchemaTranslationError",
]


class SchemaTranslationError(NotImplementedError):
    """Raised when a JSON Schema construct is not supported by the translator.

    Subclasses ``NotImplementedError`` so call sites can catch the spec-mandated
    exception type while still preserving the schema-specific subclass for
    finer error reporting.
    """


# ---------------------------------------------------------------------------
# Public entry point


def json_schema_to_typebox(schema: dict[str, Any] | None) -> str:
    """Translate ``schema`` (a JSON Schema dict) into a TypeBox expression.

    Returns the raw expression text — no semicolon, no surrounding parens.
    The caller is expected to drop it in as the ``parameters:`` value of a
    ``tool({...})`` call.

    Raises ``SchemaTranslationError`` (which is a ``NotImplementedError``)
    when the input uses ``oneOf`` / ``anyOf`` / ``allOf`` / ``$ref`` or a
    ``format`` we don't model.
    """
    if not schema:
        # Empty or missing input_schema => tool takes no parameters. TypeBox
        # represents this as an empty object schema.
        return "Type.Object({})"
    return _translate(schema)


# ---------------------------------------------------------------------------
# Internals


# JSON Schema combinator keys we refuse to translate. ``not`` is included for
# completeness even though it's never used in claw-eval tasks today.
_UNSUPPORTED_COMBINATORS = ("oneOf", "anyOf", "allOf", "not", "$ref")

# Whitelist of ``format`` values we silently drop (TypeBox represents formats
# differently; the LLM still sees the field via description, so dropping is OK).
# Any other format string raises — surfacing the long tail at preflight time.
_KNOWN_FORMATS = {
    "date",
    "date-time",
    "time",
    "email",
    "uri",
    "uuid",
}


def _translate(schema: dict[str, Any]) -> str:
    """Dispatch on the schema shape and return a TypeBox expression."""
    # Reject combinators up-front. Doing this before the type-switch keeps the
    # error message consistent regardless of where in a nested schema the
    # offender hides.
    for key in _UNSUPPORTED_COMBINATORS:
        if key in schema:
            raise SchemaTranslationError(
                f"JSON Schema key {key!r} is not supported by the OpenClaw "
                f"bridge plugin translator. Offending schema: {schema!r}"
            )

    annotation = _annotation_options(schema)

    # ``enum`` short-circuits the type switch. JSON Schema permits ``enum``
    # alongside ``type``, but the enum constrains the value space strictly, so
    # we render it as a TypeBox union of literals and ignore ``type``.
    if "enum" in schema:
        return _translate_enum(schema["enum"], annotation)

    schema_type = schema.get("type")
    if schema_type is None:
        # No type, no enum, no combinators — treat as a free-form value. TypeBox
        # has ``Type.Any()`` for this; surfaces in tool params occasionally
        # when the task author wrote ``properties: { foo: {} }``.
        return _wrap("Type.Any", annotation)

    if isinstance(schema_type, list):
        # Multi-type schemas (``type: [string, "null"]``) are valid JSON Schema
        # but rare in claw-eval. Reject for now — preflight will surface them.
        raise SchemaTranslationError(
            f"Multi-type schemas are not supported: {schema!r}"
        )

    if schema_type == "string":
        return _translate_string(schema, annotation)
    if schema_type == "integer":
        return _wrap("Type.Integer", annotation)
    if schema_type == "number":
        return _wrap("Type.Number", annotation)
    if schema_type == "boolean":
        return _wrap("Type.Boolean", annotation)
    if schema_type == "null":
        return _wrap("Type.Null", annotation)
    if schema_type == "array":
        return _translate_array(schema, annotation)
    if schema_type == "object":
        return _translate_object(schema, annotation)

    raise SchemaTranslationError(
        f"Unknown JSON Schema type {schema_type!r} in {schema!r}"
    )


def _translate_string(schema: dict[str, Any], annotation: str) -> str:
    """``type: string`` with optional ``format`` / ``description``."""
    fmt = schema.get("format")
    if fmt is not None and fmt not in _KNOWN_FORMATS:
        raise SchemaTranslationError(
            f"Unsupported string format {fmt!r} in {schema!r}"
        )
    # We deliberately don't emit the format into TypeBox — formats vary across
    # TypeBox versions and the LLM gets the hint from the description anyway.
    return _wrap("Type.String", annotation)


def _translate_array(schema: dict[str, Any], annotation: str) -> str:
    """``type: array`` with ``items`` (single schema, not a tuple)."""
    items = schema.get("items")
    if isinstance(items, list):
        # Tuple-typed arrays are vanishingly rare and TypeBox models them
        # differently (``Type.Tuple([...])``). Reject for now.
        raise SchemaTranslationError(
            f"Tuple-typed arrays are not supported: {schema!r}"
        )
    if items is None:
        # ``type: array`` without ``items`` is permitted by JSON Schema but
        # leaves the element type unconstrained. TypeBox needs *something*, so
        # we degrade to ``Type.Any()``.
        items_expr = "Type.Any()"
    else:
        items_expr = _translate(items)
    inner = f"Type.Array({items_expr}{_inline_annotation(annotation)})"
    return inner


def _translate_object(schema: dict[str, Any], annotation: str) -> str:
    """``type: object`` — properties become a TypeBox dict, with required
    flipping non-required fields to ``Type.Optional(...)``.
    """
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise SchemaTranslationError(
            f"properties must be a dict, got {type(properties).__name__}: {schema!r}"
        )
    required = set(schema.get("required") or [])

    if not properties:
        # ``Type.Object({})`` — tool takes no parameters, or a free-form bag.
        # ``additionalProperties`` is silently ignored; TypeBox defaults to
        # closed objects and the bridge plugin doesn't care either way since
        # we re-serialise to JSON for the HTTP body.
        return f"Type.Object({{}}{_inline_annotation(annotation)})"

    # Render properties deterministically (sort by key) so the generated TS
    # is stable for snapshot-style tests.
    fields: list[str] = []
    for key in sorted(properties.keys()):
        prop_schema = properties[key]
        if not isinstance(prop_schema, dict):
            raise SchemaTranslationError(
                f"property {key!r} must be a dict, got "
                f"{type(prop_schema).__name__}: {prop_schema!r}"
            )
        rendered = _translate(prop_schema)
        if key not in required:
            rendered = f"Type.Optional({rendered})"
        # Property key in a TS object literal: quote it to be safe against
        # reserved words / characters. JSON-encoded keys are valid TS string
        # literals when emitted with ``json.dumps`` (handles escapes).
        fields.append(f"{json.dumps(key)}: {rendered}")

    body = "{ " + ", ".join(fields) + " }"
    return f"Type.Object({body}{_inline_annotation(annotation)})"


def _translate_enum(values: list[Any], annotation: str) -> str:
    """``enum: [...]`` -> ``Type.Union([Type.Literal(v), ...])``.

    Annotations (description) are attached to the outer ``Type.Union`` since
    TypeBox supports schema options on unions.
    """
    if not isinstance(values, list) or not values:
        raise SchemaTranslationError(
            f"enum must be a non-empty list, got {values!r}"
        )
    literals = [f"Type.Literal({_render_literal(v)})" for v in values]
    inner = "[" + ", ".join(literals) + "]"
    return f"Type.Union({inner}{_inline_annotation(annotation)})"


# ---------------------------------------------------------------------------
# Annotation handling


def _annotation_options(schema: dict[str, Any]) -> str:
    """Build the TypeBox ``options`` object literal from descriptive metadata.

    Returns ``""`` when there is nothing to annotate, otherwise a JS object
    literal of the form ``{ description: "...", default: <json> }``. The
    caller decides how to inline it (some constructors take it inline, others
    via ``_inline_annotation``).
    """
    parts: list[str] = []
    desc = schema.get("description")
    if isinstance(desc, str) and desc:
        parts.append(f"description: {json.dumps(desc)}")
    if "default" in schema:
        # ``default`` is preserved as JSON — TypeBox just forwards it to the
        # generated JSON Schema, where the LLM may see it.
        parts.append(f"default: {json.dumps(schema['default'])}")
    if "title" in schema and isinstance(schema["title"], str):
        parts.append(f"title: {json.dumps(schema['title'])}")
    if not parts:
        return ""
    return "{ " + ", ".join(parts) + " }"


def _wrap(constructor: str, annotation: str) -> str:
    """Render ``Type.X(<annotation?>)`` — used for scalar types."""
    if annotation:
        return f"{constructor}({annotation})"
    return f"{constructor}()"


def _inline_annotation(annotation: str) -> str:
    """Render the annotation as a trailing argument: ``, { ... }`` or ``""``.

    Used by composite constructors (``Type.Array(<inner>)``, ``Type.Object({...})``
    etc.) where the annotation is the *second* argument, not the first.
    """
    if not annotation:
        return ""
    return f", {annotation}"


def _render_literal(value: Any) -> str:
    """Render a JSON value as a TypeScript literal.

    Strings -> JSON-quoted; numbers / booleans -> as-is; null -> ``null``.
    Anything else (lists, dicts) is not a valid TypeBox literal -> raises.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        # ``json.dumps`` handles NaN/Infinity by raising, which is what we want.
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise SchemaTranslationError(
        f"enum value must be a JSON primitive, got {type(value).__name__}: {value!r}"
    )
