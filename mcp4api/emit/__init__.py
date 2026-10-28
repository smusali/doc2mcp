from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from mcp4api.curate.manifest import Manifest, load_manifest
from mcp4api.emit.render import EmitError as EmitError
from mcp4api.emit.render import render_files
from mcp4api.model import ApiSpec

_LEDGER = ".mcp4api-files.json"
_GENERATED = "generated"


def emit(
    out: Path,
    *,
    force: bool = False,
    skip_tools: frozenset[str] | None = None,
    description_prefixes: dict[str, str] | None = None,
) -> None:
    """Write the generated MCP server for a curated manifest.

    Args:
        out: Directory that contains tools.yaml and endpoints.json.
        force: Overwrite generated files that were edited after the last emit.
        skip_tools: Tool names to leave out of the server.
        description_prefixes: Text prepended to those tools' descriptions.

    Returns:
        None. The server is written under ``out / "generated"``.
    """
    tools_path = out / "tools.yaml"
    endpoints_path = out / "endpoints.json"
    if not tools_path.is_file():
        raise EmitError(f"not found: {tools_path}")
    if not endpoints_path.is_file():
        raise EmitError(f"not found: {endpoints_path}")
    tools_bytes = _read(tools_path)
    endpoints_bytes = _read(endpoints_path)
    manifest = _load_manifest(tools_bytes)
    selected = _select_tools(manifest, skip_tools or frozenset(), description_prefixes or {})
    rendered = render_files(
        _load_spec(endpoints_bytes),
        manifest if selected is None else selected,
        tools_sha256=hashlib.sha256(tools_bytes).hexdigest(),
    )
    _write_tree(out / _GENERATED, rendered.files, rendered.header, force=force)


def _select_tools(
    manifest: Manifest,
    skip_tools: frozenset[str],
    description_prefixes: dict[str, str],
) -> Manifest | None:
    if not skip_tools and not description_prefixes:
        return None
    tools = []
    for tool in manifest.tools:
        if tool.name in skip_tools:
            continue
        prefix = description_prefixes.get(tool.name, "")
        if prefix:
            tool = tool.model_copy(update={"description": f"{prefix}{tool.description}"})
        tools.append(tool)
    return manifest.model_copy(update={"tools": tools})


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EmitError(f"not found: {path}") from exc


def _load_manifest(payload: bytes) -> Manifest:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EmitError("tools.yaml is invalid") from exc
    try:
        return load_manifest(text)
    except (yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
        raise EmitError("tools.yaml is invalid") from exc


def _load_spec(payload: bytes) -> ApiSpec:
    try:
        return ApiSpec.model_validate_json(payload)
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise EmitError("endpoints.json is invalid") from exc


def _write_tree(root: Path, files: dict[str, str], header: str, *, force: bool) -> None:
    encoded = {path: _encode(text) for path, text in files.items()}
    recorded = _read_ledger(root / _LEDGER, force=force)
    edited: list[str] = []
    removable: list[str] = []
    if recorded is not None:
        for rel, digest in recorded.items():
            path = _inside(root, rel)
            changed = not path.is_file() or _digest(path.read_bytes()) != digest
            if rel not in encoded:
                if changed and not force:
                    edited.append(rel)
                elif path.is_file() or not changed:
                    removable.append(rel)
                continue
            if changed:
                edited.append(rel)
        if edited and not force:
            raise EmitError(_refuse(edited))
    for rel in removable:
        path = _inside(root, rel)
        if path.is_file():
            path.unlink()
            _prune(root, path)
    root.mkdir(parents=True, exist_ok=True)
    for rel in sorted(encoded):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded[rel])
    ledger = {"files": {rel: _digest(encoded[rel]) for rel in sorted(encoded)}, "header": header}
    (root / _LEDGER).write_bytes(_encode(json.dumps(ledger, indent=2, sort_keys=True) + "\n"))


def _encode(text: str) -> bytes:
    if "\r" in text:
        raise EmitError("generated text contains a carriage return")
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_ledger(path: Path, *, force: bool) -> dict[str, str] | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        loaded: object = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if force:
            return None
        raise EmitError(f"{_GENERATED}/{_LEDGER} is invalid") from exc
    if not isinstance(loaded, dict):
        if force:
            return None
        raise EmitError(f"{_GENERATED}/{_LEDGER} is invalid")
    files = loaded.get("files")
    if not isinstance(files, dict):
        if force:
            return None
        raise EmitError(f"{_GENERATED}/{_LEDGER} is invalid")
    recorded: dict[str, str] = {}
    for key, value in files.items():
        if not isinstance(key, str) or not isinstance(value, str) or not _relative(key):
            if force:
                return None
            raise EmitError(f"{_GENERATED}/{_LEDGER} is invalid")
        recorded[key] = value
    return recorded


def _relative(rel: str) -> bool:
    path = Path(rel)
    return not path.is_absolute() and ".." not in path.parts and rel not in {"", "."}


def _inside(root: Path, rel: str) -> Path:
    if not _relative(rel):
        raise EmitError(f"{_GENERATED}/{_LEDGER} is invalid")
    return root / rel


def _prune(root: Path, path: Path) -> None:
    current = path.parent
    while current != root:
        if any(current.iterdir()):
            return
        current.rmdir()
        current = current.parent


def _refuse(paths: list[str]) -> str:
    lines = [
        "refusing to overwrite edited files:",
        *[f"{_GENERATED}/{path}" for path in sorted(paths)],
    ]
    lines.append("Change tools.yaml or overrides.yaml and run mcp4api again, or pass --force.")
    return "\n".join(lines)
