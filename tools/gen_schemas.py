#!/usr/bin/env python3
"""Generate the contract JSON Schemas from the netsim_common pydantic models.

    uv run python tools/gen_schemas.py            # (re)write the schema files
    uv run python tools/gen_schemas.py --check    # exit 1 if a schema file is stale

* contract/topology.schema.json  <- netsim_common.topology.Topology  (structure of contract/topology.yaml;
  cross-references such as "service.spoke is a spoke" are checked by the model, not the schema)
* contract/bsm-spec.schema.json  <- netsim_common.bsm.BsmSpec        (contract section 9.3)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from netsim_common.bsm import BsmSpec
from netsim_common.topology import Topology

REPO_ROOT = Path(__file__).resolve().parents[1]
DRAFT = "https://json-schema.org/draft/2020-12/schema"

SCHEMAS: dict[str, tuple[type[BaseModel], str, str]] = {
    "contract/topology.schema.json": (
        Topology,
        "urn:netsim:schema:topology",
        "NetSim lab topology (contract/topology.yaml). Generated from netsim_common.topology.Topology.",
    ),
    "contract/bsm-spec.schema.json": (
        BsmSpec,
        "urn:netsim:schema:bsm-spec",
        "BSM spec (docs/CONTRACT.md section 9.3). Generated from netsim_common.bsm.BsmSpec.",
    ),
}


def render(model: type[BaseModel], schema_id: str, description: str) -> str:
    schema = model.model_json_schema(mode="validation")
    doc = {"$schema": DRAFT, "$id": schema_id, "description": description}
    doc.update({k: v for k, v in schema.items() if k != "description"})
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify only, do not write")
    args = parser.parse_args(argv)
    stale = []
    for rel, spec in SCHEMAS.items():
        path = REPO_ROOT / rel
        text = render(*spec)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(rel)
        elif not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {rel}")
    for rel in stale:
        print(f"ERROR: {rel} is out of date - run `make contract`", file=sys.stderr)
    if args.check and not stale:
        print(f"OK: {len(SCHEMAS)} schemas are up to date")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
