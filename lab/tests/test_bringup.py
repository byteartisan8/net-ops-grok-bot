"""Bring-up plumbing that only breaks once every component has landed."""

from __future__ import annotations

import subprocess

from netsim_common.paths import repo_root

FRAGMENTS = "devices/compose.yaml netbox/compose.yaml zabbix/compose.yaml lab/compose.yaml"


def test_require_services_works_when_no_fragment_is_a_stub():
    """The root Makefile's stub note (`make up` / `make build`) runs under `bash -eu -o pipefail`: a grep without
    matches must not abort the recipe. It did, as soon as lab/compose.yaml (the last stub) became real."""
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    line = next(x for x in makefile.splitlines() if x.strip().startswith('stubs="$$('))
    snippet = line.strip().removesuffix("\\").strip().rstrip(";").replace("$$", "$").replace("$(FRAGMENTS)", FRAGMENTS)
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", f'{snippet}; echo "stubs=[$stubs]"'],
        cwd=repo_root(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stubs=[]"


def test_every_compose_fragment_is_real():
    for fragment in FRAGMENTS.split():
        text = (repo_root() / fragment).read_text(encoding="utf-8")
        assert "services: {}" not in text, fragment
