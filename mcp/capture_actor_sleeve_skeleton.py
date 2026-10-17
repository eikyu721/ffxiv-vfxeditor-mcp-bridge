"""Capture the narrow live sleeve-skeleton report without requiring MCP tool discovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from server import call_bridge
from skeleton_files import WORKSPACE_ROOT


def safe_output_path(value: str) -> Path:
    output = Path(value).resolve()
    try:
        output.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise ValueError(f"Output must remain under {WORKSPACE_ROOT}.") from exc
    if output.suffix.lower() != ".json":
        raise ValueError("Output must use the .json suffix.")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", help=f"New JSON path under {WORKSPACE_ROOT}")
    args = parser.parse_args()
    output = safe_output_path(args.output)
    report = call_bridge("inspect_actor_sleeve_skeleton")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "bytes": output.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
