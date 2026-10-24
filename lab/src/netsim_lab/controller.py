"""The Clixon controller (10-clixon-controller) as lab-api reaches it: the controller CLI over SSH on labnet.

lab-api is the controller's only client. It logs in as one of the three NOC roles (``noc-engineer``,
``network-engineer``, ``noc-manager``) to run a role's read, and as ``lab-api`` to run ``check`` and ``diff`` (both
send ``config-pull``, the RPC a real pull sends, which the roles' NACM does not grant; the actor in the audit log is
still the role), to read the connection table for ``labctl status`` and to resync routers after a reset, an approved
change or a field repair. Nothing else pulls.

Two walls: :func:`allow` (here) decides what reaches the controller, and refuses everything else before any
connection is made; the controller's NACM decides what each of its users may do there (the roles read, lab-api also
pulls and reconnects, nobody pushes).

Every command runs in its own SSH session: ``ssh -T <user>@controller`` with the command on stdin, never through a
shell (the users' login shell and sshd's ForceCommand are the controller CLI), with a timeout, the echo and terminal
codes removed and the output capped.

Keys: lab-api makes its own key once (``<LAB_DATA_DIR>/controller/id_ed25519``; it never leaves lab-api's volume) and
publishes the public half in the shared volume (``<CONTROLLER_KEYS_DIR>/lab-api.pub``), which the controller's sshd
authorizes for those four users from lab-api's address only. The controller's host key comes the other way
(``controller-host.pub``) and is the only host key ssh accepts for it.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

#: The topology's name for the controller (``contract/topology.yaml`` infrastructure).
CONTROLLER = "controller"
ROLE_USERS = {"noc-engineer": "noc-engineer", "network-engineer": "network-engineer", "noc-manager": "noc-manager"}
LAB_API_USER = "lab-api"
MAX_COMMAND = 200
MAX_OUTPUT = 16 * 1024
TIMEOUT_S = 15.0
MAX_PATH_TOKENS = 12
#: A token of a config path: list keys and node names (SVC-0001, GigabitEthernet0/0/1.101, vc-id, 10.255.0.1).
PATH_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
#: Pipes, redirects and what a shell or the CLI would read as more than one command.
SPECIALS = set("|<>;&`$\\\"'(){}*?!#~")
ALLOWED = (
    "show connections",
    "show connections <router>",
    "show connections <router> detail",
    "show devices <router> check",
    "show devices <router> diff",
    "show configuration devices device <router> config",
    "show configuration devices device <router> config <path>",
)
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
#: Clixon's log prefix on its error lines: "Oct 24 04:55:29.404774: clicon_rpc_edit_config: 755: ".
LOG_PREFIX = re.compile(r"^[A-Z][a-z]{2} +\d+ \d\d:\d\d:\d\d\.\d+: (?:[A-Za-z_]+: \d+: )?")
UNAVAILABLE = (
    "ssh: ",
    "Connection refused",
    "No route to host",
    "Connection timed out",
    "Network is unreachable",
    "Connection closed by",
    "Connection reset",
    "kex_exchange_identification",
    "Permission denied",
    "Host key verification failed",
    "Could not resolve",
    "Connect to backend",
    "clicon_rpc_connect",
)


class ControllerError(RuntimeError):
    """The controller could not run the command (down, unreachable, not started yet, timed out)."""


class Refused(ValueError):
    """Wall 1: the command is not one the roles may run; nothing reached the controller."""


@dataclass(frozen=True)
class Allowed:
    """An allow-listed command: normalized, who runs it on the controller (the role's user or lab-api)."""

    command: str
    runs_as: Literal["role", "lab-api"]
    host: str | None = None


def refusal_text(reason: str, hosts: Iterable[str]) -> str:
    routers = ", ".join(hosts)
    return (
        f"refused: {reason}. Allowed commands (one per call): "
        + "; ".join(ALLOWED)
        + f". <router>: {routers}. <path>: a subtree of the router's config, e.g. 'l2vpn xconnect SVC-0001' or "
        "'interfaces interface GigabitEthernet0/0/1.101'."
    )


def allow(command: str, hosts: Iterable[str]) -> Allowed:
    """Wall 1. Raises :class:`Refused` (whose message lists the allowed commands) for anything else."""
    known = list(hosts)

    def refuse(reason: str) -> Refused:
        return Refused(refusal_text(reason, known))

    if not isinstance(command, str) or not command.strip():
        raise refuse("an empty command")
    if len(command) > MAX_COMMAND:
        raise refuse(f"more than {MAX_COMMAND} characters")
    if "\n" in command or "\r" in command or ";" in command:
        raise refuse("more than one command (a newline or ';')")
    if any(c in "|<>" for c in command):
        raise refuse("pipes and redirects")
    if any(ord(c) < 32 or ord(c) == 127 for c in command):
        raise refuse("control characters")
    if not command.isascii():
        raise refuse("characters outside ASCII")
    if any(c in SPECIALS for c in command):
        raise refuse(f"a special character ({''.join(sorted(c for c in set(command) if c in SPECIALS))})")
    words = command.split()

    def router(name: str) -> str:
        if name not in known:
            raise refuse(f"unknown router {name!r}")
        return name

    match words:
        case ["show", "connections"]:
            return Allowed("show connections", "role")
        case ["show", "connections", host]:
            return Allowed(f"show connections {router(host)}", "role", host)
        case ["show", "connections", host, "detail"]:
            return Allowed(f"show connections {router(host)} detail", "role", host)
        case ["show", "devices", host, ("check" | "diff") as what]:
            return Allowed(f"show devices {router(host)} {what}", "lab-api", host)
        case ["show", "configuration", "devices", "device", host, "config", *path]:
            router(host)
            if len(path) > MAX_PATH_TOKENS:
                raise refuse(f"a config path longer than {MAX_PATH_TOKENS} words")
            bad = [p for p in path if not PATH_TOKEN.fullmatch(p)]
            if bad:
                raise refuse(f"not a config path word: {bad[0]!r}")
            return Allowed(" ".join(["show configuration devices device", host, "config", *path]), "role", host)
    raise refuse(f"not an allowed command: {' '.join(words)!r}")


# ---------------------------------------------------------------------------------------------- output
def clean_output(stdout: str, stderr: str, command: str) -> str:
    """The CLI's answer: echo of the command and terminal codes removed, Clixon's log prefixes dropped, stderr (its
    errors) after stdout, no trailing blanks."""

    def plain(text: str) -> str:
        text = ANSI.sub("", text).replace("\r", "")
        return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))

    out = plain(stdout).lstrip("\n")
    # the CLI echoes the command it read, wrapped at 80 columns: drop it if it is there
    i = j = 0
    while i < len(out) and j < len(command):
        if out[i] == command[j]:
            i, j = i + 1, j + 1
        elif out[i] in " \n" and command[j] != out[i]:
            i += 1
        else:
            break
    if j == len(command):
        out = out[i:]

    def trimmed(text: str) -> list[str]:
        lines = [LOG_PREFIX.sub("", line).rstrip() for line in text.splitlines()]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return lines

    return "\n".join(trimmed(out) + trimmed(plain(stderr)))


def cap(text: str, limit: int = MAX_OUTPUT) -> tuple[str, bool]:
    data = text.encode()
    if len(data) <= limit:
        return text, False
    cut = data[:limit].decode(errors="ignore")
    if "\n" in cut:
        cut = cut[: cut.rindex("\n")]
    return cut + f"\n... (cut at {limit // 1024} KiB: narrow the command, e.g. a config subtree)", True


def parse_connections(text: str) -> dict[str, dict[str, str]]:
    """``show connections`` -> {router: {state, time, logmsg}}: the rows under the ``====`` line, cut at the header's
    column positions (Name, State, Time, Logmsg)."""
    out: dict[str, dict[str, str]] = {}
    cols: list[int] | None = None
    rows = False
    for line in text.splitlines():
        if line.startswith("Name") and "State" in line:
            cols = [line.index(word) for word in ("State", "Time", "Logmsg") if word in line]
        elif line.startswith("===="):
            rows = True
        elif rows and cols and len(cols) == 3 and line.strip():
            s, t, m = cols
            out[line[:s].strip()] = {"state": line[s:t].strip(), "time": line[t:m].strip(), "logmsg": line[m:].strip()}
    return out


def parse_diff(text: str, hosts: Iterable[str]) -> dict[str, str]:
    """``show devices * diff`` -> {router: its diff} for the routers that differ from the controller's copy."""
    names = set(hosts)
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.endswith(":") and line[:-1] in names:
            current = line[:-1]
            out[current] = []
        elif current and line.strip() and not line.startswith(("Warning:", "Error:")):
            out[current].append(line)
    return {h: "\n".join(lines) for h, lines in out.items() if lines}


# ---------------------------------------------------------------------------------------------- the client
@dataclass
class CliResult:
    user: str
    command: str
    output: str
    truncated: bool
    duration_ms: int
    exit_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "command": self.command,
            "output": self.output,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
        }


#: ``runner(argv, stdin, timeout) -> (exit status, stdout, stderr)``; tests pass a fake SSH session.
Runner = Callable[[list[str], str, float], tuple[int, str, str]]


def run_process(argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ControllerError(f"the controller did not answer within {timeout:g} s") from exc
    except OSError as exc:
        raise ControllerError(f"cannot run ssh: {exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr


@dataclass
class Controller:
    """lab-api's sessions with the controller (see the module docstring)."""

    address: str
    hosts: list[str]
    key_dir: Path
    keys_dir: Path
    port: int = 22
    timeout: float = TIMEOUT_S
    runner: Runner = run_process
    status_max_age: float = 5.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _status_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _status: tuple[float, dict[str, Any]] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ keys
    @property
    def key(self) -> Path:
        return self.key_dir / "id_ed25519"

    @property
    def known_hosts(self) -> Path:
        return self.key_dir / "known_hosts"

    def ensure_keys(self) -> None:
        """lab-api's key (made once), its public half in the shared volume, and the controller's host key pinned."""
        self.key_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.key_dir, 0o700)
        if not self.key.exists():
            code, _out, err = self.runner(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lab-api@netsim", "-f", str(self.key)], "", 30
            )
            if code != 0:
                raise ControllerError(f"cannot make lab-api's SSH key: {err.strip()}")
        public = Path(f"{self.key}.pub").read_text(encoding="ascii")
        published = self.keys_dir / "lab-api.pub"
        try:
            current = published.read_text(encoding="ascii")
        except OSError:
            current = None
        if current != public:
            try:
                tmp = self.keys_dir / ".lab-api.pub.tmp"
                tmp.write_text(public, encoding="ascii")
                tmp.chmod(0o644)
                tmp.replace(published)
            except OSError as exc:
                raise ControllerError(f"cannot publish lab-api's key in {self.keys_dir}: {exc}") from exc
        try:
            host_key = (self.keys_dir / "controller-host.pub").read_text(encoding="ascii").split()
        except OSError as exc:
            raise ControllerError("the controller has not started yet (no host key in the shared volume)") from exc
        line = f"{self.address} {host_key[0]} {host_key[1]}\n"
        if not self.known_hosts.exists() or self.known_hosts.read_text(encoding="ascii") != line:
            self.known_hosts.write_text(line, encoding="ascii")

    def ssh_argv(self, user: str) -> list[str]:
        return [
            "ssh",
            "-T",
            "-F",
            "/dev/null",
            "-i",
            str(self.key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=3",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "LogLevel=ERROR",
            "-p",
            str(self.port),
            f"{user}@{self.address}",
        ]

    # ------------------------------------------------------------------ sessions
    def run(self, user: str, commands: list[str] | str) -> CliResult:
        """One SSH session as ``user`` running ``commands`` (lines on stdin) in the controller CLI."""
        lines = [commands] if isinstance(commands, str) else list(commands)
        self.ensure_keys()
        start = time.monotonic()
        code, out, err = self.runner(self.ssh_argv(user), "".join(f"{c}\n" for c in lines), self.timeout)
        elapsed = int((time.monotonic() - start) * 1000)
        if code == 255 and any(marker in err for marker in UNAVAILABLE) and not out.strip():
            raise ControllerError(f"the controller is unavailable: {err.strip().splitlines()[-1][:200]}")
        output = clean_output(out, err, lines[0]) if len(lines) == 1 else clean_output(out, err, "")
        text, truncated = cap(output)
        return CliResult(user, "; ".join(lines), text, truncated, elapsed, code)

    def run_role(self, role: str, allowed: Allowed) -> CliResult:
        """An allow-listed command for ``role``: as the role's own user, or as lab-api for check/diff."""
        if allowed.runs_as == "lab-api":
            with self._lock:
                return self.run(LAB_API_USER, allowed.command)
        return self.run(ROLE_USERS[role], allowed.command)

    def connections(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return parse_connections(self.run(LAB_API_USER, "show connections").output)

    def status(self, max_age: float | None = None) -> dict[str, Any]:
        """Routers connected, in sync and out of sync (``show connections`` + ``show devices * diff``), cached
        ``status_max_age`` seconds. A closed router is neither in sync nor out of sync."""
        age = self.status_max_age if max_age is None else max_age
        with self._status_lock:
            if self._status and time.monotonic() - self._status[0] < age:
                return self._status[1]
            start = time.monotonic()
            with self._lock:
                table = parse_connections(self.run(LAB_API_USER, "show connections").output)
                diff = self.run(LAB_API_USER, "show devices * diff")
            diffs = parse_diff(diff.output, self.hosts)
            routers = {}
            for host in self.hosts:
                row = table.get(host, {"state": "UNKNOWN", "time": "", "logmsg": ""})
                is_open = row["state"] == "OPEN"
                routers[host] = {
                    **row,
                    "in_sync": (host not in diffs) if is_open else None,
                    "diff": diffs.get(host),
                }
            result = {
                "available": True,
                "connected": sum(r["state"] == "OPEN" for r in routers.values()),
                "in_sync": sorted(h for h, r in routers.items() if r["in_sync"] is True),
                "out_of_sync": sorted(h for h, r in routers.items() if r["in_sync"] is False),
                "closed": sorted(h for h, r in routers.items() if r["state"] != "OPEN"),
                "routers": routers,
                "checked_at": round(time.time(), 3),
                "duration_ms": int((time.monotonic() - start) * 1000),
            }
            self._status = (time.monotonic(), result)
            return result

    def forget_status(self) -> None:
        with self._status_lock:
            self._status = None

    def resync(self, hosts: Iterable[str] | None = None) -> dict[str, Any]:
        """The controller takes these routers' running config as its copy: ``pull`` a connected router, ``connection
        open`` a closed one (connecting is the controller's sync). As lab-api; one session."""
        wanted = [h for h in (hosts or self.hosts) if h in self.hosts]
        if not wanted:
            return {"hosts": {}, "output": ""}
        with self._lock:
            before = parse_connections(self.run(LAB_API_USER, "show connections").output)
            actions = {h: "pull" if before.get(h, {}).get("state") == "OPEN" else "connect" for h in wanted}
            commands = [f"pull {h}" if a == "pull" else f"connection open {h}" for h, a in actions.items()]
            done = self.run(LAB_API_USER, commands)
            after = parse_connections(self.run(LAB_API_USER, "show connections").output)
        self.forget_status()
        return {
            "hosts": {h: {"action": a, "state": after.get(h, {}).get("state", "UNKNOWN")} for h, a in actions.items()},
            "output": done.output,
            "duration_ms": done.duration_ms,
        }


def controller_from_settings(settings: Any, topology: Any, runner: Runner | None = None) -> Controller:
    """The controller of the topology (``infrastructure: controller``) with lab-api's key in ``LAB_DATA_DIR``."""
    host = topology.infra(CONTROLLER)
    return Controller(
        address=os.environ.get("CONTROLLER_ADDRESS") or host.mgmt_ip,
        hosts=list(settings.hosts),
        key_dir=Path(settings.data_dir) / "controller",
        keys_dir=Path(os.environ.get("CONTROLLER_KEYS_DIR") or "/etc/netsim/keys"),
        port=int(os.environ.get("CONTROLLER_SSH_PORT") or host.container_port or 22),
        runner=runner or run_process,
    )
