#!/bin/sh
# netsim-pe entrypoint (owner: 02-device-simulator): prepare runtime directories, then start supervisord
# as PID 1. With arguments, run that command instead (e.g. `netsim-trapsink`, `snmpwalk ...`).
set -eu

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

: "${HOSTNAME:?HOSTNAME is required}"
: "${MGMT_IP:?MGMT_IP is required}"

# /run survives `docker restart`: drop stale sockets and pid files of the previous run.
rm -rf /run/clixon /run/netsim /run/supervisor.sock /run/supervisord.pid
install -d -m 0750 -g clicon /run/clixon
install -d -m 0755 /run/netsim /run/netsim/snmp /run/netsim/snmpsim /run/netsim/snmpsim/data
install -d -m 0755 -o snmpsim -g snmpsim /run/netsim/snmpsim/cache
install -d -m 0755 /var/lib/netsim /var/lib/netsim/clixon /var/lib/netsim/simd

# snmpsim data file <community>.snmprec: one subtree line handing every request to the "netsim" module.
netsim-devices snmp-datafile --dir /run/netsim/snmpsim/data --community "${SNMP_COMMUNITY:-}"

exec supervisord -n -c /etc/supervisor/supervisord.conf
