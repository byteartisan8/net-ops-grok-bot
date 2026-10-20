"""``labctl``: drive and observe the lab from the host (an engineer's hands on the lab; lab-api is the bot's).

labctl status [--watch]                 intent + live device state + Zabbix status, per service
labctl fault uplink hub-a [--restore]   physical faults (core keys); fault ac / clear-all / list
labctl cut spoke-3 hub-b [--restore]    core path between two devices
labctl device stop|start hub-b          power off/on
labctl config get|set|delete|diff ...   RESTCONF, one commit per change, like an engineer
labctl drift [--json]                   NetBox vs devices; exit 1 on drift
labctl impact hub-a                     blast radius + live backup state
labctl scenario list|show|apply|verify|check|run NAME
labctl reset                            back to golden, faults cleared, wait until green
labctl events [--since 5m] [--follow]   simd events of every device
labctl sync [--local]                   NetBox -> Zabbix sync (through lab-api by default)
labctl wait                             wait until the lab is up and green (bring-up)
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Annotated, Any

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from netsim_lab.lab import Lab

app = typer.Typer(help=__doc__, no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
fault_app = typer.Typer(help="Physical faults (core keys; config untouched).", no_args_is_help=True)
device_app = typer.Typer(help="Device power (docker stop/start).", no_args_is_help=True)
config_app = typer.Typer(help="Device config over RESTCONF (paths as in drift output).", no_args_is_help=True)
scenario_app = typer.Typer(help="Declarative scenarios (lab/scenarios/*.yaml).", no_args_is_help=True)
app.add_typer(fault_app, name="fault")
app.add_typer(device_app, name="device")
app.add_typer(config_app, name="config")
app.add_typer(scenario_app, name="scenario")

console = Console()
err = Console(stderr=True)

STATUS_STYLE = {
    "OK": "green",
    "Information": "blue",
    "Warning": "yellow",
    "Average": "dark_orange",
    "High": "red",
    "Disaster": "bold red",
    None: "dim",
}
OPER_STYLE = {"up": "green", "down": "red", "lowerLayerDown": "red", "absent": "magenta", "unreachable": "bold red"}


def say(message: Any = "") -> None:
    """Print plain text (config paths contain [...], which rich would take for markup)."""
    console.print(message, markup=False, highlight=False)


def _lab() -> Lab:
    return Lab()


def _fail(message: str, code: int = 1) -> None:
    err.print(f"[red]labctl: {message}[/red]")
    raise typer.Exit(code)


def _json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------------------------- status
def _status_style(status: str | None) -> str:
    return STATUS_STYLE.get(status, "white")


def _end_text(end: Any) -> Text:
    style = OPER_STYLE.get(end.oper or "", "white")
    reason = "" if end.reason in (None, "ok") else f" {end.reason}"
    return Text(f"{end.oper}{reason}", style=style)


def render_status(snapshot: Any) -> Group:
    parts: list[Any] = []
    header = Text("NetSim L2VPN  ", style="bold")
    header.append(f"Zabbix L2VPN: {snapshot.zabbix_root or '-'}", style=_status_style(snapshot.zabbix_root))
    header.append(
        f"   {time.strftime('%H:%M:%S', time.localtime(snapshot.generated_at))}  ({snapshot.duration_s:.2f} s)",
        style="dim",
    )
    parts.append(header)
    for source, message in snapshot.errors.items():
        parts.append(Text(f"{source}: {message}", style="red"))
    down = [h for h, d in snapshot.devices.items() if not d.get("reachable")]
    faulty = [
        h
        for h, d in snapshot.devices.items()
        if d.get("reachable")
        and (
            d.get("faults", {}).get("uplink")
            or d.get("faults", {}).get("interfaces")
            or d.get("faults", {}).get("cuts")
        )
    ]
    if down or faulty:
        parts.append(
            Text(f"devices down: {', '.join(down) or '-'}   with faults: {', '.join(faulty) or '-'}", style="yellow")
        )
    parts.append(
        Text(
            "  PW        intent (NetBox)   spoke end (simd)          hub end (simd)            Zabbix leaf", style="dim"
        )
    )
    for svc in snapshot.services:
        title = Text(f"{svc.service}", style="bold")
        title.append(f"  {svc.customer or '?'} ({svc.sla_tier or '?'})  {svc.spoke}  ")
        title.append(f"live {svc.live}", style={"up": "green", "degraded": "yellow", "down": "bold red"}[svc.live])
        title.append("  Zabbix ")
        title.append(f"{svc.zabbix or '-'}", style=_status_style(svc.zabbix))
        if not svc.in_intent:
            title.append("  (not in NetBox: pruned on the next sync)", style="magenta")
        table = Table(title=title, title_justify="left", show_header=False, show_edge=False, pad_edge=False, box=None)
        table.add_column("PW", style="cyan", width=9, no_wrap=True)
        table.add_column("intent", width=17, no_wrap=True)
        table.add_column("spoke end", width=25, no_wrap=True)
        table.add_column("hub end", width=25, no_wrap=True)
        table.add_column("Zabbix leaf", width=10, no_wrap=True)
        for pw in svc.pws:
            table.add_row(
                f"  {pw.role}",
                f"{pw.hub} vc {pw.vc_id}",
                Text(f"{pw.spoke_end.host} ").append_text(_end_text(pw.spoke_end)),
                Text(f"{pw.hub_end.host} ").append_text(_end_text(pw.hub_end)),
                Text(pw.zabbix or "-", style=_status_style(pw.zabbix)),
            )
        parts.append(table)
    return Group(*parts)


@app.command()
def status(
    watch: Annotated[bool, typer.Option("--watch", "-w", help="Refresh continuously (the demo's live view)")] = False,
    interval: Annotated[float, typer.Option(help="Seconds between refreshes")] = 1.0,
    as_json: Annotated[bool, typer.Option("--json", help="JSON output (one snapshot)")] = False,
    plain: Annotated[bool, typer.Option(help="With --watch: print each frame (no terminal redraw)")] = False,
    count: Annotated[int, typer.Option(help="With --watch: stop after this many frames (0 = forever)")] = 0,
) -> None:
    """One table per service: intent (NetBox), live state of both ends of each PW (simd) and Zabbix status."""
    from netsim_lab.status import collect

    with _lab() as lab:
        if as_json:
            _json(collect(lab).to_dict())
            return
        if not watch:
            console.print(render_status(collect(lab)))
            return
        frames = 0
        if plain or not console.is_terminal:
            while count == 0 or frames < count:
                t0 = time.monotonic()
                console.print(render_status(collect(lab, intent_max_age=10.0)))
                console.print(Text("-" * 60, style="dim"))
                frames += 1
                time.sleep(max(0.0, interval - (time.monotonic() - t0)))
            return
        with Live(render_status(collect(lab)), console=console, refresh_per_second=4, screen=False) as live:
            try:
                while count == 0 or frames < count:
                    t0 = time.monotonic()
                    live.update(render_status(collect(lab, intent_max_age=10.0)))
                    frames += 1
                    time.sleep(max(0.0, interval - (time.monotonic() - t0)))
            except KeyboardInterrupt:
                pass


# ---------------------------------------------------------------------------------------------- faults
@fault_app.command("uplink")
def fault_uplink(host: str, restore: Annotated[bool, typer.Option("--restore", help="Repair it")] = False) -> None:
    """Core uplink of HOST physically down (or back up with --restore)."""
    with _lab() as lab:
        lab.settings.device(host)
        key = lab.core.set_uplink(host, not restore)
        say(f"{'restored' if restore else 'set'} {key}")


@fault_app.command("ac")
def fault_ac(host: str, interface: str, restore: Annotated[bool, typer.Option("--restore")] = False) -> None:
    """Attachment circuit INTERFACE on HOST physically down (a physical access interface takes its subinterfaces)."""
    with _lab() as lab:
        lab.settings.device(host)
        key = lab.core.set_ac(host, interface, not restore)
        say(f"{'restored' if restore else 'set'} {key}")


@fault_app.command("clear-all")
def fault_clear_all() -> None:
    """Delete every fault and cut key and power on every stopped device."""
    with _lab() as lab:
        keys = lab.core.clear_faults()
        started = [h for h in lab.stopped() if lab.docker.start(h)]
        cleared = f"cleared {len(keys)} key(s)" + (f": {', '.join(keys)}" if keys else "")
        say(f"{cleared}; started {', '.join(started) or 'no device'}")


@fault_app.command("list")
def fault_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Active faults, cuts and powered-off devices."""
    from netsim_lab.core import describe_fault, fault_dict

    with _lab() as lab:
        faults, stopped = lab.core.faults(), lab.stopped()
        if as_json:
            _json({"faults": [fault_dict(f) for f in faults], "stopped": stopped})
            return
        for f in faults:
            say(f"fault: {describe_fault(f)}  ({f.key})")
        for h in stopped:
            console.print(f"powered off: {h}")
        if not faults and not stopped:
            console.print("no faults")


@app.command()
def cut(a: str, b: str, restore: Annotated[bool, typer.Option("--restore")] = False) -> None:
    """Break (or --restore) the core path between devices A and B."""
    with _lab() as lab:
        lab.settings.device(a)
        lab.settings.device(b)
        key = lab.core.set_cut(a, b, not restore)
        say(f"{'restored' if restore else 'set'} {key}")


@device_app.command("stop")
def device_stop(host: str) -> None:
    """Power off HOST (docker stop)."""
    with _lab() as lab:
        changed = lab.docker.stop(host)
        console.print(f"{host}: {'stopped' if changed else 'already stopped'}")


@device_app.command("start")
def device_start(host: str) -> None:
    """Power on HOST (docker start)."""
    with _lab() as lab:
        changed = lab.docker.start(host)
        console.print(f"{host}: {'started' if changed else 'already running'}")


# ---------------------------------------------------------------------------------------------- config
@config_app.command("get")
def config_get(
    host: str,
    path: Annotated[str | None, typer.Argument(help="e.g. l2vpn/xconnect[SVC-0001]; default: the whole config")] = None,
    intent: Annotated[
        bool, typer.Option("--intent", help="NetBox's intended config instead of the running one")
    ] = False,
) -> None:
    """Running (or intended) config of HOST as RESTCONF JSON."""
    from netsim_common.canonical import canonical_dict
    from netsim_lab.configpath import get_path

    with _lab() as lab:
        lab.settings.device(host)
        if intent:
            from netsim_netbox.intent import render_device

            data: Any = canonical_dict(render_device(host, lab.intent()))
            if path:
                try:
                    data = get_path(data, path)
                except KeyError:
                    _fail(f"{path}: not in the intent of {host}")
        elif path:
            data = lab.devices[host].get_path(path)
            if data is None:
                _fail(f"{path}: does not exist on {host}")
        else:
            data = canonical_dict(lab.devices[host].running())
        _json(data)


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text  # bare strings: an address, an interface name


@config_app.command("set")
def config_set(
    host: str, path: str, value: Annotated[str, typer.Argument(help="JSON value (bare strings allowed)")]
) -> None:
    """Set PATH on HOST to VALUE: one RESTCONF PUT, one commit (e.g. ... pseudowire[backup]/vc-id 2010)."""
    with _lab() as lab:
        lab.settings.device(host)
        lab.devices[host].put_path(path, _parse_value(value))
        say(f"{host}: {path} = {value} (committed)")


@config_app.command("delete")
def config_delete(host: str, path: str) -> None:
    """Delete PATH on HOST (a leaf, a list entry such as l2vpn/xconnect[SVC-0002], a container)."""
    with _lab() as lab:
        lab.settings.device(host)
        lab.devices[host].delete_path(path)
        say(f"{host}: deleted {path} (committed)")


@config_app.command("diff")
def config_diff(
    host: str, golden: Annotated[bool, typer.Option("--golden", help="Against golden instead of NetBox")] = False
) -> None:
    """Differences between NetBox's intent (or golden) and HOST's running config."""
    from netsim_common.canonical import diff_configs, format_diffs

    with _lab() as lab:
        lab.settings.device(host)
        if golden:
            expected: Any = lab.golden()[host]
        else:
            from netsim_netbox.intent import render_device

            expected = render_device(host, lab.intent())
        diffs = diff_configs(expected, lab.devices[host].running())
        say(format_diffs(diffs))
        raise typer.Exit(1 if diffs else 0)


# ---------------------------------------------------------------------------------------------- analysis
CLASS_STYLE = {"config-drift": "red", "intent-only": "magenta", "fault": "yellow"}


@app.command()
def drift(
    as_json: Annotated[bool, typer.Option("--json")] = False,
    host: Annotated[str | None, typer.Option(help="Only findings involving this host")] = None,
) -> None:
    """NetBox intent vs running config of every device. Exit code 1 if any drift (config-drift/intent-only)."""
    with _lab() as lab:
        report = lab.drift()
    if as_json:
        _json(report.to_dict())
        raise typer.Exit(1 if report.drift else 0)
    findings = [f for f in report.findings if host is None or host in f.hosts]
    for name, dev in report.devices.items():
        if host and name != host:
            continue
        style = {"in-sync": "green", "drift": "red", "unreachable": "bold red", "no-intent": "magenta"}[dev.status]
        console.print(Text(f"{name:8} {dev.status:<11} baseline {dev.baseline_source}", style=style))
        for d in dev.diffs:
            console.print(Text(f"    [{d.cls}] {d.diff.message}", style=CLASS_STYLE[d.cls]))
        if dev.error:
            console.print(Text(f"    {dev.error}", style="dim"))
    console.print()
    if not findings:
        console.print(Text("no findings: devices match NetBox, no faults", style="green"))
    for f in findings:
        console.print(Text(f"[{f.cls}] ", style=CLASS_STYLE[f.cls]).append(f.message))
        for d in f.details:
            console.print(Text(f"    - {d}", style="dim"))
    for p in report.intent_problems:
        console.print(Text(f"NetBox intent problem: {p}", style="magenta"))
    raise typer.Exit(1 if report.drift else 0)


@app.command()
def impact(host: str, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """What losing HOST (maintenance, failure) would do: NetBox blast radius + live state of the surviving PWs."""
    from netsim_lab.impact import impact as compute

    with _lab() as lab:
        result = compute(lab, host)
    if as_json:
        _json(result)
        return
    table = Table(title=f"impact of losing {host}", title_justify="left")
    for col in ("service", "customer", "intent", "live", "Zabbix now", "why"):
        table.add_column(col)
    style = {"down": "bold red", "degraded": "yellow", "unaffected": "green"}
    for s in result["services"]:
        live = Text(s["live_impact"], style=style[s["live_impact"]])
        if s["worse_than_intent"]:
            live.append("  (worse than intent)", style="bold red")
        table.add_row(
            s["service"],
            s["customer"] or "",
            Text(s["intent_impact"], style=style[s["intent_impact"]]),
            live,
            s["zabbix_now"] or "-",
            s["explanation"],
        )
    console.print(table)


# ---------------------------------------------------------------------------------------------- scenarios
def _scenarios() -> dict[str, Any]:
    from netsim_lab.scenarios import load_scenarios
    from netsim_lab.settings import load_settings

    return load_scenarios(load_settings().scenarios_dir)


def _scenario(name: str) -> Any:
    found = _scenarios()
    if name not in found:
        _fail(f"no scenario {name!r} (have: {', '.join(found)})")
    return found[name]


@scenario_app.command("list")
def scenario_list() -> None:
    """Every scenario with its title."""
    for s in _scenarios().values():
        extra = f"  (+ {', '.join(p.name for p in s.then)})" if s.then else ""
        console.print(f"[cyan]{s.name:<20}[/cyan] {s.title}{extra}")


@scenario_app.command("show")
def scenario_show(name: str) -> None:
    """A scenario's description, steps and expectations."""
    s = _scenario(name)
    console.print(f"[bold]{s.name}[/bold]: {s.title}\n{s.description.strip()}\n")
    for phase in s.phases:
        console.print(f"[cyan]{phase.name}[/cyan] (budget {phase.budget_s} s)")
        for step in phase.steps:
            say(f"  step: {step.describe()}")
        say("  expect: " + json.dumps(phase.expect.model_dump(mode="json", by_alias=True, exclude_defaults=True)))


@scenario_app.command("apply")
def scenario_apply(name: str) -> None:
    """Run the scenario's main steps (start from `labctl reset`)."""
    from netsim_lab.scenarios import apply_scenario

    s = _scenario(name)
    with _lab() as lab:
        result = apply_scenario(lab, s, "labctl", log=say)
    console.print(f"applied {len(result['steps'])} step(s); now `labctl scenario verify {name}`")


@scenario_app.command("verify")
def scenario_verify(
    name: str,
    follow_ups: Annotated[bool, typer.Option("--follow-ups/--no-follow-ups", help="Also run the 'then' phases")] = True,
    timeout: Annotated[float | None, typer.Option(help="Seconds to wait for the main expectations")] = None,
) -> None:
    """Wait until the lab matches the scenario's expectations, then run its follow-up phases. Exit 1 on mismatch."""
    from netsim_lab.scenarios import verify_scenario

    s = _scenario(name)
    if timeout is not None:
        s.timeout_s = timeout
    with _lab() as lab:
        result = verify_scenario(lab, s, time.time() - 600, follow_ups=follow_ups, log=say)
    console.print(
        Text(f"scenario {name}: {'OK' if result['ok'] else 'FAILED'}", style="green" if result["ok"] else "bold red")
    )
    raise typer.Exit(0 if result["ok"] else 1)


@scenario_app.command("check")
def scenario_check(name: str) -> None:
    """Check the main expectations once, now (no waiting, no steps)."""
    from netsim_lab.scenarios import check_phase

    s = _scenario(name)
    with _lab() as lab:
        mismatches = check_phase(lab, s.expect, since=0.0)
    if not mismatches:
        console.print(Text(f"{name}: every expectation holds", style="green"))
        return
    for part, items in mismatches.items():
        for item in items:
            say(f"{part}: {item}")
    raise typer.Exit(1)


@scenario_app.command("run")
def scenario_run(
    name: str, keep: Annotated[bool, typer.Option("--keep", help="Do not reset at the end")] = False
) -> None:
    """reset -> apply -> verify (with follow-ups) -> reset."""
    from netsim_lab.reset import reset
    from netsim_lab.scenarios import apply_scenario, verify_scenario

    s = _scenario(name)
    with _lab() as lab:
        reset(lab, log=say)
        try:
            applied = apply_scenario(lab, s, "labctl", log=say)
            since = applied["steps"][-1]["t"] + applied["steps"][-1]["elapsed_s"] if applied["steps"] else time.time()
            result = verify_scenario(lab, s, since, log=say)
        finally:
            if not keep:
                reset(lab, log=say)
    console.print(
        Text(f"scenario {name}: {'OK' if result['ok'] else 'FAILED'}", style="green" if result["ok"] else "bold red")
    )
    raise typer.Exit(0 if result["ok"] else 1)


# ------------------------------------------------------------------------------------ reset, events, sync, wait
@app.command("reset")
def reset_cmd(
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait until simd and Zabbix are green")] = True,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for green")] = 150.0,
) -> None:
    """Golden config on every device, faults cleared, devices started, NetBox/Zabbix back to the topology."""
    from netsim_lab.reset import reset

    with _lab() as lab:
        result = reset(lab, wait=wait, timeout=timeout, log=say)
    if wait and not result.get("green"):
        _fail("the lab is not green: " + "; ".join(result.get("wait", {}).get("why", [])[:10]))


def _since(value: str | None) -> float | None:
    if value is None:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", value)
    if m and m[2]:
        return time.time() - float(m[1]) * {"s": 1, "m": 60, "h": 3600}[m[2]]
    try:
        return float(value)
    except ValueError:
        _fail(f"--since: {value!r} (use 30s, 5m, 1h or Unix seconds)")
        return None


@app.command()
def events(
    since: Annotated[str | None, typer.Option(help="30s, 5m, 1h or Unix seconds")] = "5m",
    limit: Annotated[int, typer.Option(help="At most this many")] = 100,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing new events")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """simd events of every device (config changes, PW transitions, traps), merged by time."""
    from netsim_lab.events import merged_events

    t = _since(since)
    with _lab() as lab:
        while True:
            result = merged_events(lab, since=t, limit=None if follow else limit)
            for e in result["events"]:
                if as_json:
                    print(json.dumps(e, default=str))
                    continue
                stamp = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
                detail = {
                    k: v
                    for k, v in e.items()
                    if k not in ("id", "ts", "time", "host", "boot_id", "type", "varbinds", "targets", "diff", "edits")
                }
                line = Text(f"{stamp} ")
                line.append(f"{e['host']:<8}", style="cyan")
                line.append(f" {e.get('type', '?'):<16} {json.dumps(detail, default=str)[:160]}")
                console.print(line)
                t = max(t or 0.0, e.get("ts", 0.0))
            for host, message in result["errors"].items():
                err.print(f"[yellow]{host}: {message}[/yellow]")
            if not follow:
                return
            time.sleep(1.0)


@app.command()
def sync(
    local: Annotated[
        bool, typer.Option("--local", help="Run zabbix-sync in this process instead of through lab-api")
    ] = False,
) -> None:
    """NetBox -> Zabbix sync (spec macros, BSM, SLAs, map)."""
    if local:
        from netsim_zabbix.sync import sync as run

        result = run(log=say)
        console.print(f"zabbix-sync: {result.plan.summary()} ({result.duration:.1f} s)")
        return
    from netsim_lab.labapi_client import LabApiClient, LabApiError
    from netsim_lab.settings import load_settings

    settings = load_settings()
    try:
        with LabApiClient(settings.lab_api_url, settings.lab_api_token) as api:
            _json(api.sync(wait=True, reason="labctl sync"))
    except LabApiError as exc:
        _fail(str(exc))


@app.command("wait")
def wait_cmd(
    timeout: Annotated[float, typer.Option(help="Seconds")] = 300.0,
    green: Annotated[
        bool, typer.Option("--green/--up", help="Also wait for Zabbix green (default) or only for devices up")
    ] = True,
) -> None:
    """Wait until every device answers with all PWs up (and Zabbix is green)."""
    from netsim_lab.reset import simd_green, wait_green

    with _lab() as lab:
        if green:
            result = wait_green(lab, timeout=timeout, log=say)
            if not result["green"]:
                _fail("not green: " + "; ".join(result["why"][:10]))
            console.print(f"green (simd {result['simd_s']} s, Zabbix {result['zabbix_s']} s)")
            return
        deadline = time.monotonic() + timeout
        why = simd_green(lab)
        while why and time.monotonic() < deadline:
            time.sleep(1.0)
            why = simd_green(lab)
        if why:
            _fail("devices not up: " + "; ".join(why[:10]))
        console.print("devices up")


@app.command("wait-healthy")
def wait_healthy_cmd(timeout: Annotated[float, typer.Option(help="Seconds")] = 600.0) -> None:
    """Wait until every container of the lab is running and healthy (bring-up; NetBox's first start takes minutes)."""
    from netsim_lab.bringup import BringupError, wait_healthy

    try:
        wait_healthy(timeout=timeout, log=say)
    except BringupError as exc:
        _fail(str(exc))


def main() -> None:
    from netsim_lab.changes import PlanError
    from netsim_lab.configpath import PathError
    from netsim_lab.core import CoreError
    from netsim_lab.devices import DeviceError
    from netsim_lab.docker_api import DockerError
    from netsim_lab.scenarios import ScenarioError
    from netsim_lab.settings import SettingsError

    errors: tuple[type[BaseException], ...] = (
        PlanError,
        PathError,
        CoreError,
        DeviceError,
        DockerError,
        ScenarioError,
        SettingsError,
    )
    try:
        from netsim_netbox.client import NetBoxError
        from netsim_netbox.intent import IntentError
        from netsim_netbox.settings import SettingsError as NetBoxSettingsError
        from netsim_zabbix.api import ZabbixApiError
        from netsim_zabbix.settings import SettingsError as ZabbixSettingsError

        errors += (NetBoxError, IntentError, NetBoxSettingsError, ZabbixApiError, ZabbixSettingsError)
    except ImportError:  # pragma: no cover
        pass
    try:
        app()
    except errors as exc:
        err.print(f"[red]labctl: {exc}[/red]")
        sys.exit(2)


if __name__ == "__main__":
    main()
