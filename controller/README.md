# controller/ - the Clixon controller in front of the PEs (owner: session 10-clixon-controller)

One container, `controller` at `172.30.10.70` on `labnet`, runs the [Clixon
controller](https://clixon-controller-docs.readthedocs.io/) **1.9.0** on Clixon/CLIgen **7.9.0** (the PEs' release). It
manages the ten PE routers over NETCONF over SSH, keeps a copy of each router's config, and notices when a router no
longer matches that copy: **out of sync**, a second drift signal next to NetBox. The three NOC roles read it through
grok-bot's `controller_cli` tool; lab-api is its only client. Report:
[`docs/reports/10-clixon-controller.md`](../docs/reports/10-clixon-controller.md).

```
Bot (Grok Bot cloud) -- /mcp, role token --> grok-bot --> NOC room: the CONTROLLER terminal shows command and output
                                               |
                                               +--> lab-api  POST /controller/cli
                                                      |  allow-list (wall 1), audit, 16 KiB cap, 15 s
                                                      +- SSH :22 as the role's controller user --> controller (NACM: wall 2)
                                                                                                    |
                                                                   NETCONF over SSH :830 -----------+--> 10 PEs (Clixon)
lab-api -- RESTCONF, unchanged: dry runs, approved changes, scenarios, reset ---------------------------> 10 PEs
```

```bash
make controller-build     # the image (first build compiles CLIgen, Clixon and the controller, ~2 min)
make controller-up        # start it (make up does too); it connects the ten PEs by itself
make controller-shell     # the controller CLI as admin: show connections, show devices spoke-1 diff, quit
make controller-test      # on the running lab: in sync after a reset, out of sync after vcid-typo, NACM alone
make controller-config    # regenerate startup_db from contract/topology.yaml (controller-check verifies it)
uv run labctl status      # first line: controller: 10/10 connected, 10 in sync
```

## What the controller holds

- **The PEs**, generated from `contract/topology.yaml` into `startup_db` (`controller/startup.py`; not by hand): one
  device per PE at its mgmt IP, through the profile `netsim-pe` (NETCONF over SSH on port 830 as the PEs' user
  `controller`, strict host key checking, YANG bound from the device), and a device group per region (`north`,
  `south`). The routers' YANG comes from the routers themselves (RFC 6022 `get-schema`).
- **Its copy of each router's config**: what the lab last put there on purpose. The controller takes a router's
  running config as its copy whenever it connects to it (the controller syncs on every connect) or pulls it; lab-api
  resyncs (pull, or connect a closed router) after `labctl reset`, after an approved change is applied and verified,
  and after a field repair powers a router back on. **Nothing else pulls.** Anything else that changes a router (a
  scenario such as `vcid-typo`, someone on the router's CLI) makes it out of sync.
- **A fresh datastore at every start**: the entrypoint copies `startup_db` into the datastore and the backend starts
  with `-s startup`; the agent then connects every PE that answers (within 10 minutes), which takes their current
  config as the copy. So a controller restart forgets earlier drift; lab-api's resync after a reset brings it back to
  golden.

## Users, NACM and the two walls

| User | Who | NACM (`CLICON_NACM_CREDENTIALS=exact`: the unix user is the NACM user) |
|---|---|---|
| `noc-engineer`, `network-engineer`, `noc-manager` | the NOC roles, through lab-api | read; RPCs `get`, `get-config` and the CLI's own (`clixon-cache`, `config-path-info`, `translate-format`, `nacm-autocli-filter-get`, `create-subscription`); never `edit-config`, `commit`, `config-pull`, `controller-commit`, `connection-change`, `device-rpc`, templates, locks, `kill-session`; `/nacm` hidden |
| `lab-api` | lab-api | read, `config-pull` (pull, and the transient pull behind check and diff), `connection-change`, `datastore-diff`, `device-rpc-result`, `transaction-error`; never `controller-commit` (no push) |
| `admin` | people, `make controller-shell` | everything |
| `root` | the container itself (its connect agent, `docker exec`) | the NACM recovery user |

`write-default` and `exec-default` are `deny`: an RPC runs only when a rule grants it. NACM names RPCs, never their
arguments: `show devices <router> check` and `diff` send `config-pull` with `transient=true`, the RPC a real `pull`
sends, so the roles do not get it and lab-api runs those two as its own user (the audit's actor is still the role).

**Wall 1** is lab-api's allow-list (`lab/src/netsim_lab/controller.py`); **wall 2** is this NACM. `make
controller-test` proves wall 2 alone: an SSH session as `network-engineer` that tries an edit and commit, a pull, a
push or a connection change is denied by the controller.

## SSH and keys

| | Listens | Lets in | Session |
|---|---|---|---|
| controller sshd | `:22` on labnet (not published) | the three role users and `lab-api`, from lab-api's address (172.30.10.50), key only | the controller CLI (`ForceCommand` and login shell `controller-cli`): no shell, no forwarding, no files |
| PE sshd (`devices/sshd_config`) | `:830` on labnet (not published) | `controller` from 172.30.10.70, key only | `clixon_netconf` (subsystem `netconf`, `ForceCommand` and login shell `netconf-session`) |

Keys are made where they are used and never move; only public halves travel, through the shared volume
`netsim_controller-keys` (`/etc/netsim/keys`):

| Key | Made by, kept in | Public half |
|---|---|---|
| the controller's key for the PEs | the controller's entrypoint, volume `netsim_controller-data` | `controller.pub`: the PEs' sshd authorizes it for `controller` |
| the controller's sshd host key | the controller's entrypoint, `netsim_controller-data` | `controller-host.pub`: the only host key lab-api's ssh accepts |
| lab-api's key | lab-api, on first use, `netsim_lab-api-data` | `lab-api.pub`: the controller's sshd authorizes it |
| each PE's host key | the PE's entrypoint, its own volume | learned by the controller's agent on first contact (`ssh-keyscan`, trust on first use), then checked |

Volumes survive `make down && make up`, so the keys do too; `make clean` deletes them and new ones are made.

## Inside the container

```
supervisord (PID 1)
+-- clixon_backend     the controller (controller.xml + clixon/netsim.xml), datastore /run/controller/db
+-- sshd               :22, the CLI for lab-api's sessions
+-- controller_agent   learns the PEs' host keys, connects every PE after each backend start
```

`clixon/netsim.xml` (`/etc/clixon/controller/netsim.xml`, read after the controller's own `controller.xml`):
`CLICON_AUTOLOCK=true` (no shared candidate), NACM internal and always on, credentials exact, no RESTCONF, no
services daemon, CLI output as curly-brace text.

## The commands the roles may run (lab-api's allow-list)

| Command | Runs as | Answers |
|---|---|---|
| `show connections` | the role | every router: `OPEN`, or `CLOSED` with the reason (a powered-off router shows at once) |
| `show connections <router> [detail]` | the role | one router; `detail`: connection and last sync time |
| `show devices <router> check` | lab-api | nothing when in sync; `Error: device out-of-sync` when not |
| `show devices <router> diff` | lab-api | `<router>:` then the differences: `-` lines what the router runs now, `+` lines the controller's copy |
| `show configuration devices device <router> config [<path>]` | the role | the controller's copy, whole or a subtree (`l2vpn xconnect SVC-0001`, `interfaces interface GigabitEthernet0/0/1.101`) |

`show devices` and `show devices <router>` alone are incomplete commands in controller 1.9.0; their content is `show
connections`. Anything else, a second command, pipes, control characters or more than 200 characters is refused by
lab-api with this list. A router is back in sync as soon as its config equals the controller's copy again; no pull
is needed for that.

## Files

| Path | |
|---|---|
| `Dockerfile`, `Dockerfile.dockerignore` | the image (build context: repo root) |
| `compose.yaml`, `controller.mk` | the service (included by the root `compose.yaml`), `make controller-*` |
| `startup.py`, `startup_db` | the startup datastore (PEs, groups, NACM), generated from the topology |
| `clixon/netsim.xml` | the lab's Clixon settings |
| `sshd_config`, `ssh_config` | sshd for lab-api's sessions; the ssh client settings for the PEs |
| `supervisord.conf`, `bin/controller-entrypoint`, `bin/controller-cli` | processes, start-up, the users' login shell |
| `agent.py`, `healthcheck.py` | the connect agent; the container healthcheck (backend socket, sshd listening) |
| `tests/` | unit tests (`make test`); `tests/integration/` on the running lab (`make controller-test`) |
