"""REST client, .env handling, settings and bootstrap helpers (no NetBox: httpx.MockTransport and stubs)."""

from __future__ import annotations

import json
import stat

import httpx
import pytest
from netsim_netbox import bootstrap
from netsim_netbox.client import NetBoxClient, NetBoxError, auth_header, describe_token, token_key
from netsim_netbox.envfile import read_env_file, update_env_file
from netsim_netbox.settings import SettingsError, load_settings

V2 = "nbt_AbCdEf123456." + "x" * 40


# --------------------------------------------------------------------------------------------- client
def test_auth_headers_and_token_keys():
    assert auth_header(V2) == f"Bearer {V2}"
    assert auth_header("0123456789abcdef0123456789abcdef01234567") == "Token 0123456789abcdef0123456789abcdef01234567"
    assert token_key(V2) == "AbCdEf123456"
    assert token_key("legacy") is None and token_key(None) is None
    assert describe_token(V2) == "nbt_AbCdEf123456.***"
    assert "x" * 10 not in describe_token(V2)


def test_list_follows_next_links_rebased_on_our_url():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, dict(request.url.params), request.headers["Authorization"]))
        if request.url.params.get("offset") == "2":
            return httpx.Response(200, json={"count": 3, "next": None, "results": [{"id": 3}]})
        nxt = "http://172.30.10.30:8080/api/dcim/devices/?limit=2&offset=2"  # NetBox builds links from its own host
        return httpx.Response(200, json={"count": 3, "next": nxt, "results": [{"id": 1}, {"id": 2}]})

    with NetBoxClient("http://127.0.0.1:8000/", V2, transport=httpx.MockTransport(handler)) as client:
        assert [o["id"] for o in client.list("dcim/devices", name=["a", "b"])] == [1, 2, 3]
    assert seen[0][:2] == ("127.0.0.1", "/api/dcim/devices/") and seen[1][0] == "127.0.0.1"
    assert seen[0][3] == f"Bearer {V2}"


def test_repeated_query_parameters():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("device_id") == ["1", "2"]
        return httpx.Response(200, json={"count": 0, "next": None, "results": []})

    with NetBoxClient("http://nb", V2, transport=httpx.MockTransport(handler)) as client:
        assert client.list("dcim/interfaces", device_id=[1, 2]) == []


def test_errors_carry_status_and_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"tag": ["Select a valid choice."]})

    with (
        NetBoxClient("http://nb", V2, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(NetBoxError) as err,
    ):
        client.list("vpn/l2vpns", tag="nope")
    assert err.value.status == 400 and err.value.detail == {"tag": ["Select a valid choice."]}
    assert V2 not in str(err.value)


def test_unreachable_netbox_is_a_netbox_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with NetBoxClient("http://nb", transport=httpx.MockTransport(handler)) as client, pytest.raises(NetBoxError) as err:
        client.status()
    assert err.value.status is None and "ConnectError" in str(err.value)


def test_bulk_writes_are_chunked_lists():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.method, request.url.path, len(body)))
        if request.method == "DELETE":
            assert all(set(item) == {"id"} for item in body)
            return httpx.Response(204)
        return httpx.Response(201, json=[{"id": i} for i in range(len(body))])

    with NetBoxClient("http://nb", V2, transport=httpx.MockTransport(handler)) as client:
        assert len(client.bulk_create("tenancy/tenants", [{"name": str(i)} for i in range(250)])) == 250
        client.bulk_delete("tenancy/tenants", range(120))
    assert calls == [
        ("POST", "/api/tenancy/tenants/", 100),
        ("POST", "/api/tenancy/tenants/", 100),
        ("POST", "/api/tenancy/tenants/", 50),
        ("DELETE", "/api/tenancy/tenants/", 100),
        ("DELETE", "/api/tenancy/tenants/", 20),
    ]


# --------------------------------------------------------------------------------------------- .env
def test_update_env_file_in_place_append_and_private(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# header\nNETBOX_TOKEN_RW=\nOTHER=keep\n", encoding="utf-8")
    env.chmod(0o644)
    assert update_env_file(env, {"NETBOX_TOKEN_RW": V2, "NETBOX_TOKEN_RO": "ro"}) == [
        "NETBOX_TOKEN_RW",
        "NETBOX_TOKEN_RO",
    ]
    assert env.read_text(encoding="utf-8") == f"# header\nNETBOX_TOKEN_RW={V2}\nOTHER=keep\nNETBOX_TOKEN_RO=ro\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert update_env_file(env, {"NETBOX_TOKEN_RW": V2}) == []
    assert read_env_file(env) == {"NETBOX_TOKEN_RW": V2, "OTHER": "keep", "NETBOX_TOKEN_RO": "ro"}


def test_read_env_file_unquotes_and_ignores_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A='x y'\n# B=no\nC=\"z\"\nD=plain=with=equals\n", encoding="utf-8")
    assert read_env_file(env) == {"A": "x y", "C": "z", "D": "plain=with=equals"}
    assert read_env_file(tmp_path / "missing") == {}


def test_settings_precedence(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NETBOX_URL=http://from-file:8000/\nNETBOX_TOKEN_RO=file-ro\n", encoding="utf-8")
    s = load_settings(environ={"NETBOX_TOKEN_RO": "env-ro"}, env_file=env)
    assert (s.url, s.token_ro, s.token_rw) == ("http://from-file:8000", "env-ro", None)
    assert s.public_url == "http://127.0.0.1:8000"  # browser-facing default from the topology
    defaults = load_settings(environ={}, env_file=tmp_path / "missing")
    assert defaults.url == "http://127.0.0.1:8000"
    with pytest.raises(SettingsError, match="make netbox-bootstrap"):
        defaults.client(write=True)


# --------------------------------------------------------------------------------------------- bootstrap
def test_generated_passwords_meet_netbox_validators():
    for _ in range(200):
        pw = bootstrap._password()
        assert len(pw) >= 12 and any(c.isdigit() for c in pw)
        assert any(c.isupper() for c in pw) and any(c.islower() for c in pw)


def test_full_token_from_creation_response():
    assert bootstrap._full_token({"version": 2, "key": "AbCdEf123456", "token": "x" * 40}) == V2
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap._full_token({"version": 1, "token": "x" * 40})


def test_readonly_permission_skips_accounts_and_secrets():
    class Admin:
        def list(self, endpoint, **params):
            assert (endpoint, params) == ("core/object-types", {"public": True})
            rows = [
                ("dcim", "device"),
                ("users", "token"),
                ("extras", "webhook"),
                ("vpn", "l2vpn"),
                ("core", "objectchange"),
            ]
            return [{"app_label": a, "model": m} for a, m in rows]

    assert bootstrap.readonly_object_types(Admin()) == ["core.objectchange", "dcim.device", "vpn.l2vpn"]


def _mock_netbox(monkeypatch, routes):
    """Route bootstrap's own NetBoxClient instances to a MockTransport: routes[(method, path)] -> response."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("Authorization")))
        status, body = routes[(request.method, request.url.path)](request)
        return httpx.Response(status, json=body)

    real = bootstrap.NetBoxClient
    monkeypatch.setattr(
        bootstrap,
        "NetBoxClient",
        lambda url, token=None, **kw: real(url, token, transport=httpx.MockTransport(handler)),
    )
    return calls


def test_rw_token_is_kept_when_valid(monkeypatch):
    calls = _mock_netbox(
        monkeypatch,
        {
            ("GET", "/api/authentication-check/"): lambda r: (200, {"username": "admin"}),
            ("GET", "/api/users/tokens/"): lambda r: (
                200,
                {"next": None, "results": [{"id": 2, "enabled": True, "write_enabled": True}]},
            ),
        },
    )
    result = bootstrap.ensure_rw_token("http://nb", V2, "admin", "pw")
    assert (result.action, result.token) == ("kept", V2)
    assert all(c[0] == "GET" for c in calls)


def test_rw_token_is_reprovisioned_when_rejected(monkeypatch):
    def provision(request):
        assert json.loads(request.content)["username"] == "admin"
        return 201, {"version": 2, "key": "NewKey123456", "token": "y" * 40}

    calls = _mock_netbox(
        monkeypatch,
        {
            ("GET", "/api/authentication-check/"): lambda r: (403, {"detail": "Invalid v2 token"}),
            ("POST", "/api/users/tokens/provision/"): provision,
        },
    )
    result = bootstrap.ensure_rw_token("http://nb", V2, "admin", "pw")
    assert (result.action, result.token) == ("created", "nbt_NewKey123456." + "y" * 40)
    assert "rejected" in result.replaced
    assert calls[-1][2] is None  # provisioning is anonymous (username + password)
