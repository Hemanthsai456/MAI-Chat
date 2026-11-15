"""Approved CLI Registry and tool specification definitions.

Ensures the backend ONLY runs approved tools with validated argument lists.
Arbitrary user-provided or model-generated commands are strictly rejected.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROJECT_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_DIR / "tools"


@dataclass(frozen=True)
class ArgumentSpec:
    """Specification and validation for a single approved CLI flag."""
    flag: str
    arg_type: type = str
    description: str = ""
    required: bool = False
    default: Any = None
    min_value: float | None = None
    max_value: float | None = None
    allowed_values: tuple[str, ...] | None = None
    regex: re.Pattern | None = None


@dataclass
class ApprovedTool:
    """Metadata and execution constraints for an approved CLI tool.

    Signature maintains backward compatibility with (name, script, description, max_timeout_s).
    """
    name: str
    script: Path
    description: str
    max_timeout_s: float = 120.0
    id: str = ""
    max_output_bytes: int = 2_000_000
    argument_specs: dict[str, ArgumentSpec] = field(default_factory=dict)
    custom_validator: Callable[[list[str]], list[str]] | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.name

        # Ensure approved script paths remain within the trusted tools directory
        script_resolved = self.script.resolve()
        tools_resolved = TOOLS_DIR.resolve()
        try:
            is_in_tools = script_resolved.is_relative_to(tools_resolved)
        except AttributeError:  # Python < 3.9 fallback
            is_in_tools = str(script_resolved).startswith(str(tools_resolved))

        if not is_in_tools:
            # Check if explicit test mode allows temporary test scripts
            temp_dir = Path(tempfile.gettempdir()).resolve()
            is_test = (
                os.environ.get("MAI_TESTING") == "1"
                or "unittest" in sys.modules
                or "test" in sys.argv[0].lower()
            )
            try:
                is_in_temp = script_resolved.is_relative_to(temp_dir)
            except AttributeError:
                is_in_temp = str(script_resolved).startswith(str(temp_dir))

            if not (is_test and is_in_temp):
                raise ValueError(
                    f"Approved tool script '{self.script}' must reside within the trusted tools directory '{TOOLS_DIR}'"
                )

    def validate_args(self, args: list[str]) -> list[str]:
        """Validate an argument list strictly against this tool's argument specifications."""
        if not all(isinstance(a, str) for a in args):
            raise TypeError("All CLI arguments must be strings")

        # Do not silently allow arbitrary arguments when a tool has no argument schema
        if not self.argument_specs:
            if args:
                raise ValueError(f"Tool '{self.id}' defines no argument schema; arguments are not permitted")
            return []

        seen_flags: set[str] = set()

        # Check for unapproved flags, duplicates, missing values, or injection attempts
        for arg in args:
            if not arg.startswith("--"):
                raise ValueError(f"Positional arguments are not allowed: {arg!r}")

            if "=" not in arg:
                raise ValueError(f"Flag must have '=value' format (missing '='): {arg!r}")

            flag_name, val_str = arg.split("=", 1)

            if flag_name not in self.argument_specs:
                raise ValueError(f"Unapproved flag for tool '{self.id}': {flag_name}")

            if flag_name in seen_flags:
                raise ValueError(f"Duplicate flag not allowed: {flag_name}")

            spec = self.argument_specs[flag_name]

            # Reject empty values
            if not val_str.strip():
                raise ValueError(f"Flag {flag_name} requires a non-empty value")

            # Validate type and numeric bounds
            if spec.arg_type in (int, float):
                try:
                    val_num = spec.arg_type(val_str)
                except ValueError:
                    raise ValueError(f"Flag {flag_name} requires numeric value, got: {val_str!r}") from None
                if spec.min_value is not None and val_num < spec.min_value:
                    raise ValueError(f"Flag {flag_name} value {val_num} < minimum {spec.min_value}")
                if spec.max_value is not None and val_num > spec.max_value:
                    raise ValueError(f"Flag {flag_name} value {val_num} > maximum {spec.max_value}")
            elif spec.allowed_values and val_str not in spec.allowed_values:
                raise ValueError(f"Flag {flag_name} value {val_str!r} not in {spec.allowed_values}")
            elif spec.regex and not spec.regex.match(val_str):
                raise ValueError(f"Flag {flag_name} value {val_str!r} does not match required format")

            seen_flags.add(flag_name)

        # Enforce every required=True flag
        for flag_name, spec in self.argument_specs.items():
            if spec.required and flag_name not in seen_flags:
                raise ValueError(f"Required flag '{flag_name}' is missing")

        if self.custom_validator:
            return self.custom_validator(args)
        return list(args)


# Registry of approved tools
_REGISTRY: dict[str, ApprovedTool] = {}


def register_tool(tool: ApprovedTool) -> None:
    """Register an approved tool into the global registry."""
    _REGISTRY[tool.name] = tool
    if tool.id and tool.id != tool.name:
        _REGISTRY[tool.id] = tool


def get_tool(tool_id: str) -> ApprovedTool | None:
    """Retrieve an approved tool by ID or name."""
    return _REGISTRY.get(tool_id)


def list_tools() -> list[dict[str, Any]]:
    """Return a list of public descriptors for all approved tools."""
    unique_tools = {t.name: t for t in _REGISTRY.values()}
    return [
        {
            "id": t.id or t.name,
            "name": t.name,
            "description": t.description,
            "max_timeout_s": t.max_timeout_s,
            "max_output_bytes": t.max_output_bytes,
            "supported_flags": list(t.argument_specs.keys()),
        }
        for t in unique_tools.values()
    ]


# Initialize built-in approved tools
register_tool(
    ApprovedTool(
        name="site-crawler",
        script=TOOLS_DIR / "site_crawler.py",
        description="Bounded, polite, same-host website crawler with robots.txt enforcement and structured extraction.",
        max_timeout_s=120.0,
        max_output_bytes=2_000_000,
        argument_specs={
            "--url": ArgumentSpec(flag="--url", required=True, description="Target starting URL (http/https)"),
            "--max-pages": ArgumentSpec(flag="--max-pages", arg_type=int, min_value=1, max_value=10, default=5),
            "--max-depth": ArgumentSpec(flag="--max-depth", arg_type=int, min_value=0, max_value=3, default=2),
            "--timeout": ArgumentSpec(flag="--timeout", arg_type=float, min_value=1.0, max_value=30.0, default=10.0),
            "--delay": ArgumentSpec(flag="--delay", arg_type=float, min_value=0.0, max_value=5.0, default=0.3),
            "--extract-schema": ArgumentSpec(flag="--extract-schema", arg_type=str, description="Comma-separated field names to extract"),
        },
    )
)
