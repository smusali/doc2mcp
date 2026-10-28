from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE = _ROOT / "examples" / "sample-api"
_SKIP = ".mcp4api"


def main(argv: list[str] | None = None) -> int:
    """Run the Shelf sample offline and compare it with the committed tree.

    Args:
        argv: Process arguments.

    Returns:
        0 when the sample matches, 1 when it does not.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--timed", type=float, default=0)
    parser.add_argument("--command", action="store_true")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.runs < 1:
        print("--runs must be positive", file=sys.stderr)
        return 1
    trees: list[dict[str, bytes]] = []
    with tempfile.TemporaryDirectory() as raw:
        last = Path(raw) / "run"
        for index in range(args.runs):
            copy = Path(raw) / f"copy-{index}"
            shutil.copytree(_SAMPLE, copy, ignore=shutil.ignore_patterns(_SKIP))
            started = time.perf_counter()
            completed = _run(copy, external=args.command)
            elapsed = time.perf_counter() - started
            if args.timed and elapsed > args.timed:
                print(f"sample run took {elapsed:.1f}s", file=sys.stderr)
                return 1
            if completed.returncode != 3:
                print(completed.stdout)
                print(completed.stderr, file=sys.stderr)
                print(f"expected exit 3, got {completed.returncode}", file=sys.stderr)
                return 1
            if not _summary_ok(completed.stdout, copy):
                return 1
            if _secrets(copy):
                print("cassette contains a secret", file=sys.stderr)
                return 1
            trees.append(_files(copy))
            last = copy
        for previous, current in itertools.pairwise(trees):
            if previous != current:
                print("repeated sample runs differ", file=sys.stderr)
                return 1
        committed = _files(_SAMPLE)
        if trees[0] != committed:
            if args.update:
                _write_back(last)
                return 0
            print("sample run differs from examples/sample-api", file=sys.stderr)
            return 1
    return 0


def _run(copy: Path, *, external: bool) -> subprocess.CompletedProcess[str]:
    if external:
        command = ["uvx", "--from", str(_ROOT), "mcp4api"]
    else:
        command = ["uv", "run", "--project", str(_ROOT), "mcp4api"]
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env["HYPOTHESIS_STORAGE_DIRECTORY"] = str(copy.parent / "hypothesis")
    return subprocess.run(
        [*command, "openapi.yaml", "--offline", "--deterministic"],
        cwd=copy,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _summary_ok(stdout: str, copy: Path) -> bool:
    report_path = copy / "report" / "verification.json"
    try:
        loaded = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"missing report: {exc}", file=sys.stderr)
        return False
    if not isinstance(loaded, dict):
        print("report is not an object", file=sys.stderr)
        return False
    summary = loaded.get("summary")
    if not isinstance(summary, dict):
        print("report is missing a summary", file=sys.stderr)
        return False
    if summary.get("generated") != 15 or summary.get("verified") != 14 or summary.get("gaps") != 1:
        print(f"unexpected summary {summary}", file=sys.stderr)
        return False
    text = stdout + "\n" + (copy / "report" / "verification.md").read_text(encoding="utf-8")
    for phrase in ("15 tools generated", "14 verified", "1 known gap"):
        if phrase not in text:
            print(f"summary is missing {phrase}", file=sys.stderr)
            return False
    failures = loaded.get("failures")
    if not isinstance(failures, list) or not failures:
        print("expected get_book failures", file=sys.stderr)
        return False
    for failure in failures:
        if not isinstance(failure, dict):
            print("failure is not an object", file=sys.stderr)
            return False
        if failure.get("tool") != "get_book" or failure.get("field") != "page_count":
            print(
                f"unexpected failure {failure.get('tool')} {failure.get('field')}",
                file=sys.stderr,
            )
            return False
    return True


def _secrets(copy: Path) -> bool:
    directory = copy / "tests" / "cassettes"
    if not directory.is_dir():
        return True
    for path in directory.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        if "shelf-sample-key" in text or "Authorization" in text:
            return True
    return False


def _files(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _SKIP in relative.parts or "__pycache__" in relative.parts:
            continue
        found[relative.as_posix()] = path.read_bytes()
    return found


def _write_back(copy: Path) -> None:
    produced = _files(copy)
    current = _files(_SAMPLE)
    for relative in current:
        if relative not in produced:
            (_SAMPLE / relative).unlink()
    for relative, payload in produced.items():
        target = _SAMPLE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != payload:
            target.write_bytes(payload)


if __name__ == "__main__":
    raise SystemExit(main())
