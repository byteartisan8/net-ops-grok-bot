"""The controller on the running lab: in sync after a reset, out of sync after drift, and its NACM on its own."""

from __future__ import annotations

import time

import httpx

BACKUP_VC = "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id"
ROLE_EDITS = (
    ("configure", "set devices device spoke-1 config l2vpn xconnect SVC-0001 pseudowire backup vc-id 2010", "commit"),
    (
        "configure",
        "set devices device spoke-1 config l2vpn xconnect SVC-0001 pseudowire backup vc-id 2010",
        "commit local",
    ),
    ("pull spoke-1",),
    ("push spoke-1",),
    ("configure", "commit push"),
    ("connection close spoke-1",),
    ("show devices spoke-1 check",),  # a transient pull: config-pull, which the roles do not get
)


def copy_vc_id(root_cli) -> str:
    out = root_cli(
        "show",
        "configuration",
        "devices",
        "device",
        "spoke-1",
        "config",
        "l2vpn",
        "xconnect",
        "SVC-0001",
        "pseudowire",
        "backup",
        "vc-id",
    )
    return out.split("vc-id", 1)[1].split(";", 1)[0].strip()


def test_after_a_reset_every_router_is_connected_and_in_sync(lab, status, cli) -> None:
    st = status()
    assert st["available"] and st["connected"] == 10, st
    assert st["out_of_sync"] == [] and st["closed"] == [] and len(st["in_sync"]) == 10
    check = cli("network-engineer", "show devices hub-c check").json()
    assert check["user"] == "lab-api" and check["output"] == ""
    table = cli("noc-engineer", "show connections").json()
    assert table["user"] == "noc-engineer" and table["output"].count(" OPEN") == 10


def test_vcid_typo_puts_spoke_1_out_of_sync_with_its_diff(lab, lab_api, status, cli, root_cli, wait, reset_green):
    assert lab_api.post("/scenarios/vcid-typo/apply").status_code == 200
    st = wait(lambda: (s := status())["out_of_sync"] and s, 15, "spoke-1 out of sync")
    assert st["out_of_sync"] == ["spoke-1"] and len(st["in_sync"]) == 9 and st["connected"] == 10
    check = cli("network-engineer", "show devices spoke-1 check").json()
    assert check["output"] == "Error: device out-of-sync"
    diff = cli("network-engineer", "show devices spoke-1 diff").json()["output"]
    lines = [line.strip() for line in diff.splitlines()]
    assert lines[0] == "spoke-1:" and "pseudowire backup {" in lines
    assert '-           vc-id "2010";' in diff  # '-': what the router runs now
    assert '+           vc-id "2001";' in diff  # '+': the controller's copy
    assert copy_vc_id(root_cli) == "2001"
    others = [h for h in lab.hosts if h != "spoke-1"]
    for host in ("hub-b", "spoke-2"):
        assert cli("noc-manager", f"show devices {host} check").json()["output"] == ""
    assert set(st["in_sync"]) == set(others)

    # back to the controller's copy by hand: in sync again without a pull (the sync time does not move)
    before = root_cli("show", "connections", "spoke-1", "detail")
    lab.devices["spoke-1"].put_path(BACKUP_VC, 2001)
    wait(lambda: status()["out_of_sync"] == [], 15, "spoke-1 in sync again")
    after = root_cli("show", "connections", "spoke-1", "detail")
    sync = [line for line in before.splitlines() if "sync-timestamp" in line]
    assert sync and sync == [line for line in after.splitlines() if "sync-timestamp" in line]
    reset_green()


def test_the_controller_denies_the_role_users_without_lab_api(lab, ssh_as, status, root_cli) -> None:
    for lines in ROLE_EDITS:
        r = ssh_as("network-engineer", *lines)
        out = r.stdout + r.stderr
        assert "access-denied" in out or "access denied" in out, (lines, out[-600:])
    assert copy_vc_id(root_cli) == "2001"
    assert lab.devices["spoke-1"].get_path(BACKUP_VC) == {"netsim-device:vc-id": 2001}  # the router too
    st = status()
    assert st["connected"] == 10 and st["out_of_sync"] == [] and "spoke-1" in st["in_sync"]
    # lab-api's own user pulls and reconnects, but never pushes
    pushed = ssh_as("lab-api", "push spoke-1")
    assert "access denied" in pushed.stdout + pushed.stderr


def test_a_session_is_the_cli_never_a_shell_and_only_from_lab_api(ssh_as, run_docker) -> None:
    r = ssh_as("noc-engineer", remote="id; cat /etc/passwd")  # ForceCommand: the CLI, which reads no command
    assert "uid=" not in r.stdout + r.stderr and "root:" not in r.stdout
    for user in ("admin", "root", "controller"):
        refused = ssh_as(user, "show connections")
        assert refused.returncode == 255 and "Permission denied" in refused.stderr, (user, refused.stderr)
    # the PEs' NETCONF only for the controller's own key and address
    pe = run_docker(
        "exec",
        "lab-api",
        "ssh",
        "-T",
        "-F",
        "/dev/null",
        "-p",
        "830",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "controller@172.30.10.21",
        "-s",
        "netconf",
    )
    assert pe.returncode == 255 and "Permission denied" in pe.stderr


def test_nobody_poses_as_another_controller_user(run_docker) -> None:
    r = run_docker(
        "exec",
        "-u",
        "noc-engineer",
        "controller",
        "clixon_cli",
        "-1",
        "-U",
        "admin",
        "-f",
        "/etc/clixon/controller.xml",
        "show",
        "connections",
    )
    assert "credential not matching NACM user admin" in r.stdout + r.stderr


def test_the_controller_learned_the_routers_yang_from_the_routers(root_cli) -> None:
    out = root_cli("show", "devices", "spoke-1", "yang")
    assert "netsim-device" in out and "ietf-netconf-monitoring" in out


def test_a_netconf_commit_on_a_pe_is_attributed_to_the_controller_user(lab, run_docker, wait) -> None:
    """Commits over NETCONF carry the SSH user: simd's config-change event and the ciscoConfigManEvent trap name it."""
    port = lab.settings.device("spoke-3").simd_url

    def edit(description: str) -> str:
        hello = (
            '<?xml version="1.0" encoding="UTF-8"?><hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
            "<capabilities><capability>urn:ietf:params:netconf:base:1.0</capability></capabilities></hello>]]>]]>"
        )
        rpc = '<rpc xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" message-id="{}">{}</rpc>]]>]]>'
        body = (
            '<edit-config><target><candidate/></target><config><interfaces xmlns="urn:netsim:device"><interface>'
            f"<name>GigabitEthernet0/0/0</name><description>{description}</description></interface></interfaces>"
            "</config></edit-config>"
        )
        stdin = hello + rpc.format(1, body) + rpc.format(2, "<commit/>") + rpc.format(3, "<close-session/>")
        r = run_docker(
            "exec",
            "-i",
            "controller",
            "ssh",
            "-T",
            "-p",
            "830",
            "-i",
            "/var/lib/controller/ssh/id_ed25519",
            "-o",
            "BatchMode=yes",
            f"controller@{lab.settings.device('spoke-3').mgmt_ip}",
            "-s",
            "netconf",
            stdin=stdin,
        )
        return r.stdout

    t0 = time.time()
    assert edit("CORE-NETCONF-TEST").count("<ok/>") == 3
    try:

        def events() -> list[dict]:
            data = httpx.get(f"{port}/events", params={"since": t0}, timeout=5).json()["events"]
            return [e for e in data if e["type"] in ("config-change", "trap-sent")]

        found = wait(lambda: len(events()) >= 2 and events(), 10, "simd's config-change and trap")
        change = next(e for e in found if e["type"] == "config-change")
        assert change["changed_by"] == "controller"
        trap = next(e for e in found if e["type"] == "trap-sent" and e.get("name") == "ciscoConfigManEvent")
        user = next(v for v in trap["varbinds"] if v["name"] == "ccmHistoryEventTerminalUser")
        assert user["value"] == "controller"
    finally:
        assert edit("CORE").count("<ok/>") == 3
