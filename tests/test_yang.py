"""contract/yang: pyang --strict, yanglint, example instances, and agreement with the pydantic models."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml
from netsim_testlib import GOLDEN, REPO, YANG, YANG_DIR
from pydantic import ValidationError

from netsim_common.device_config import MODULE, NAMESPACE, PREFIX, DeviceConfig

EXAMPLES = YANG_DIR / "examples"
EXPECT = yaml.safe_load((EXAMPLES / "expectations.yaml").read_text(encoding="utf-8"))
VALID = sorted(EXPECT["valid"])
INVALID = sorted(EXPECT["invalid"])


def _pyang(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pyang", "-p", str(YANG_DIR), *args, str(YANG)], capture_output=True, text=True, cwd=REPO, check=False
    )


def test_pyang_strict():
    result = _pyang("--strict", "-Werror")
    assert result.returncode == 0, result.stderr


def test_pyang_lint_is_clean():
    result = _pyang("--strict", "--lint")
    assert result.returncode == 0, result.stderr + result.stdout


def test_yang_tree_file_is_current():
    result = _pyang("-f", "tree")
    assert result.returncode == 0, result.stderr
    assert (YANG_DIR / "netsim-device.tree").read_text(encoding="utf-8") == result.stdout, "run `make contract`"


def test_yang_header_matches_library_constants():
    text = YANG.read_text(encoding="utf-8")
    assert re.search(rf"^module {MODULE} \{{", text, re.MULTILINE)
    assert f'namespace "{NAMESPACE}";' in text
    assert f"prefix {PREFIX};" in text
    assert "yang-version 1.1;" in text
    assert "revision 2026-10-18 {" in text


def test_yang_is_ascii_only():
    # Clixon does not support Unicode in YANG.
    YANG.read_bytes().decode("ascii")


def test_yanglint_parses_the_schema(yanglint):
    result = yanglint(YANG)
    assert result.returncode == 0, result.stderr


def test_golden_configs_validate_with_yanglint(yanglint):
    files = sorted(GOLDEN.glob("*.json"))
    assert len(files) == 10
    result = yanglint("-t", "config", YANG, *files)
    assert result.returncode == 0, result.stderr


def test_examples_are_all_listed():
    on_disk = {p.relative_to(EXAMPLES).as_posix() for p in EXAMPLES.rglob("*.json")}
    listed = {f"valid/{f}" for f in VALID} | {f"invalid/{f}" for f in INVALID}
    assert on_disk == listed


def test_valid_examples_pass_yanglint(yanglint):
    result = yanglint("-t", "config", YANG, *(EXAMPLES / "valid" / f for f in VALID))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", VALID)
def test_valid_examples_pass_the_models(name):
    DeviceConfig.from_restconf((EXAMPLES / "valid" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", INVALID)
def test_invalid_examples_fail_yanglint(yanglint, name):
    result = yanglint("-t", "config", YANG, EXAMPLES / "invalid" / name)
    assert result.returncode != 0, f"{name} unexpectedly passed yanglint"
    assert EXPECT["invalid"][name]["yanglint"] in result.stderr


@pytest.mark.parametrize("name", INVALID)
def test_invalid_examples_fail_the_models(name):
    with pytest.raises(ValidationError) as info:
        DeviceConfig.from_restconf(Path(EXAMPLES / "invalid" / name).read_text(encoding="utf-8"))
    assert EXPECT["invalid"][name]["pydantic"] in str(info.value)
