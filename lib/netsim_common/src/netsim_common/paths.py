"""Locate the repository's ``contract/`` directory.

Lookup order:

1. ``$NETSIM_CONTRACT_DIR`` (use this in containers, e.g. ``/opt/netsim/contract``);
2. a ``contract/`` directory next to any parent of this file (editable/workspace installs);
3. a ``contract/`` directory in the current working directory or any of its parents.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_CONTRACT_DIR = "NETSIM_CONTRACT_DIR"
_MARKER = "topology.yaml"


class ContractNotFoundError(FileNotFoundError):
    """Raised when the contract directory cannot be located."""


def _is_contract_dir(path: Path) -> bool:
    return (path / _MARKER).is_file()


def contract_dir() -> Path:
    """Return the absolute path of the contract directory."""
    env = os.environ.get(ENV_CONTRACT_DIR)
    if env:
        path = Path(env).expanduser().resolve()
        if not _is_contract_dir(path):
            raise ContractNotFoundError(f"${ENV_CONTRACT_DIR}={env!r} does not contain {_MARKER}")
        return path
    here = Path(__file__).resolve()
    cwd = Path.cwd().resolve()
    for parent in [*here.parents, cwd, *cwd.parents]:
        candidate = parent / "contract"
        if _is_contract_dir(candidate):
            return candidate
    raise ContractNotFoundError(
        f"cannot find contract/{_MARKER} above {here} or {cwd}; set ${ENV_CONTRACT_DIR} to the contract directory"
    )


def repo_root() -> Path:
    """Return the repository root (the parent of the contract directory)."""
    return contract_dir().parent
