"""Helpers for contract-level tests (importable from any test: `tests/` is on pytest's pythonpath)."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "contract"
YANG_DIR = CONTRACT / "yang"
YANG = YANG_DIR / "netsim-device.yang"
GOLDEN = CONTRACT / "golden"
CONTRACT_MD = REPO / "docs" / "CONTRACT.md"


@cache
def load_tool(name: str) -> ModuleType:
    """Import tools/<name>.py as a module."""
    path = REPO / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"netsim_tools_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@cache
def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_yanglint(*args: object) -> subprocess.CompletedProcess[str]:
    """``tools/yanglint.sh -D -p contract/yang <args>`` from the repo root."""
    cmd = [str(REPO / "tools" / "yanglint.sh"), "-D", "-p", str(YANG_DIR), *map(str, args)]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=600)


def contract_example(marker: str) -> dict:
    """The first ```json block after ``marker`` in docs/CONTRACT.md."""
    text = CONTRACT_MD.read_text(encoding="utf-8")
    start = text.index(marker)
    match = re.search(r"```json\n(.*?)\n```", text[start:], re.DOTALL)
    assert match, f"no JSON block after {marker!r} in docs/CONTRACT.md"
    return json.loads(match.group(1))
