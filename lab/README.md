# lab/ - owner: session 05-integration

Placeholder created by 01-foundation. Everything in this directory belongs to 05-integration.

Stubs that exist only so the workspace works today (replace them freely):

- `compose.yaml` - empty compose fragment (`services: {}` + the external `labnet` network);
- `pyproject.toml` - empty uv workspace member `netsim-lab`.

Make targets for this component go in `lab/lab.mk` (included by the root `Makefile`), prefixed `lab-`;
`make reset` calls `lab-reset` and `make e2e` calls `lab-e2e`.
