"""Typed MCP job input backed by the broker's canonical offline JSON schema."""

from copy import deepcopy
from typing import Annotated

from jsonschema import ValidationError
from pydantic import ConfigDict, RootModel, WithJsonSchema, model_validator

from compute_relay.domain.validation import validate, validators


def client_job_schema():
    """Inline shipped references so clients need neither repository nor HTTP schemas."""
    schemas = {name: validator.schema for name, validator in validators().items()}

    def expand(value, document):
        if isinstance(value, list):
            return [expand(item, document) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            filename, _, fragment = value["$ref"].partition("#")
            document = filename or document
            target = schemas[document]
            for component in fragment.removeprefix("/").split("/") if fragment else []:
                target = target[component.replace("~1", "/").replace("~0", "~")]
            return expand(target, document)
        return {key: expand(item, document) for key, item in value.items() if key not in {"$schema", "$id"}}

    schema = expand(deepcopy(schemas["job-request.schema.json"]), "job-request.schema.json")
    schema["required"].remove("project_id")
    schema["properties"]["project_id"]["description"] = "Omit this internal identity; the API key selects the project."
    return schema


class InvalidJobRequest(RuntimeError):
    """A fixed input diagnostic, without Pydantic's rejected-value rendering."""


def _check_wire_types(value, depth=0):
    """Keep job JSON within the integer-only broker wire contract."""
    if depth > 64:
        raise InvalidJobRequest("Invalid job request: JSON nesting exceeds the supported limit.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidJobRequest("Invalid job request: JSON object keys must be strings.")
            _check_wire_types(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_wire_types(item, depth + 1)
    elif type(value) is int:
        if abs(value) > 2**53 - 1:
            raise InvalidJobRequest("Invalid job request: JSON integers must be within the exact wire range.")
    elif type(value) is float:
        raise InvalidJobRequest("Invalid job request: JSON numbers must be integers, without a decimal point or exponent.")
    elif not (value is None or type(value) is bool or isinstance(value, str)):
        raise InvalidJobRequest("Invalid job request: expected standard JSON values.")


def _constraint_message(error):
    keyword, expected = error.validator, error.validator_value
    if keyword == "required" and isinstance(error.instance, dict):
        missing = [name for name in expected if name not in error.instance]
        return "missing required field " + ", ".join(missing)
    if keyword in {"minimum", "maximum", "multipleOf", "minLength", "maxLength", "minItems", "maxItems"}:
        return f"must satisfy {keyword} {expected}"
    if keyword in {"type", "format"}:
        return f"expected {expected}"
    if keyword == "additionalProperties":
        return "unsupported fields are present"
    if keyword == "enum":
        return "choose one of the permitted values in the tool schema"
    if keyword in {"pattern", "const", "not", "uniqueItems"}:
        return f"does not satisfy the tool schema's {keyword} constraint"
    return "does not satisfy the tool schema"


class JobRequest(RootModel):
    """A normal JSON job object; project identity is supplied by the MCP adapter."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    root: Annotated[dict, WithJsonSchema(client_job_schema())]

    @model_validator(mode="before")
    @classmethod
    def validate_request(cls, value):
        if isinstance(value, cls):
            return value.root
        if not isinstance(value, dict):
            raise InvalidJobRequest("Invalid job request: expected a JSON object.")
        _check_wire_types(value)
        document = {**value}
        document.setdefault("project_id", "00000000-0000-0000-0000-000000000000")
        try:
            validate("job-request.schema.json", document)
        except ValidationError as error:
            field = ".".join(str(item) for item in error.absolute_path) or "request"
            raise InvalidJobRequest(f"Invalid job field {field[:120]}: {_constraint_message(error)}.") from None
        except ValueError as error:
            # These are fixed cross-field diagnostics from domain.validation;
            # they never interpolate submitted paths, arguments or file bytes.
            raise InvalidJobRequest(str(error)) from None
        return value
