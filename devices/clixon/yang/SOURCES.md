# Vendored YANG modules (owner: 02-device-simulator)

Loaded by the device image next to `contract/yang/netsim-device.yang` (which is copied from `contract/` at build
time, never duplicated here).

| File | Module | Source | sha256 |
|---|---|---|---|
| `ietf-netconf-notifications@2012-02-06.yang` | RFC 6470 `ietf-netconf-notifications` (`netconf-config-change`) | PyPI `pyang==2.7.1` wheel, `share/yang/modules/ietf/ietf-netconf-notifications.yang` (the workspace dev pin; listed with the same hash in the wheel's `RECORD`) | `b2d798cae1a3e6dbe339c1db68f502ff3211caf760190287f77f3347aae8c2f2` |

Its imports (`ietf-netconf`, `ietf-inet-types`) are the copies Clixon 7.9.0 installs in `share/clixon`.
`devices/simd/tests/test_image_files.py` checks the hash.
