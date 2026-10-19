"""Names of everything the Zabbix side of the lab creates (one place; lab-api and the bot use them too)."""

from __future__ import annotations

from netsim_common.bsm import PW_SPEC_MACRO, SCOPE_PW, SCOPE_UNEXPECTED

# ------------------------------------------------------------------ template, groups
TEMPLATE = "NetSim L2VPN PE SNMP"
TEMPLATE_GROUP = "Templates/NetSim"
HOST_GROUPS = {"hub": "L2VPN/Hubs", "spoke": "L2VPN/Spokes"}

# Marks objects that zabbix-provision / zabbix-sync own; only those are ever pruned.
MANAGED_TAG = "managed_by"
MANAGED_VALUE = "netsim-zabbix"

# ------------------------------------------------------------------ macros
#: Contract section 9.2. A Zabbix 8.0 macro value holds at most 2048 characters (DB schema hostmacro.value
#: varchar(2048)); a host whose spec is longer gets the overflow chunks below (zabbix-sync splits the list).
SPEC_MACRO = PW_SPEC_MACRO
SPEC_MACRO_MAX_LEN = 2048
SPEC_CHUNKS = 8
SPEC_OVERFLOW_MACROS = [f"{{$NETSIM.PW.SPEC.{n}}}" for n in range(2, SPEC_CHUNKS + 1)]
SPEC_MACROS = [SPEC_MACRO, *SPEC_OVERFLOW_MACROS]
SNMP_COMMUNITY_MACRO = "{$SNMP_COMMUNITY}"

# Global macros (written by zabbix-bootstrap): what the check-now webhook calls.
API_URL_MACRO = "{$NETSIM.ZABBIX.API.URL}"
API_TOKEN_MACRO = "{$NETSIM.ZABBIX.API.TOKEN}"

# ------------------------------------------------------------------ item / LLD keys
WALK_KEY = "netsim.snmp.walk"
SNMP_AVAILABLE_KEY = "zabbix[host,snmp,available]"
SPEC_LLD_KEY = "netsim.pw.spec.discovery"
OBSERVED_LLD_KEY = "netsim.pw.discovery"
IF_LLD_KEY = "netsim.if.discovery"


def pw_key(field: str, service: str = "{#SERVICE}", role: str = "{#ROLE}") -> str:
    """Key of an "Expected PWs" item, e.g. ``netsim.pw.oper[SVC-0001,primary]``."""
    return f"netsim.pw.{field}[{service},{role}]"


#: Fields of the intent items (all dependent on the row item ``netsim.pw.row[...]``).
PW_FIELDS = ("present", "oper", "admin", "peer", "vcid", "remote_mtu", "spec_diff")

# ------------------------------------------------------------------ tags
TAG_SCOPE = "scope"
SCOPE_PW_TAG = SCOPE_PW
SCOPE_UNEXPECTED_TAG = SCOPE_UNEXPECTED
SCOPE_TRAP = "l2vpn-trap"
SCOPE_AVAILABILITY = "availability"
SCOPE_INTERFACE = "interface"
TRAP_TAG = "trap"
TRAP_KINDS = ("pw", "config", "boot")

# ------------------------------------------------------------------ trap -> check now
CHECK_NOW_SCRIPT = "NetSim: check now"
TRAP_ACTION = "NetSim: SNMP trap -> check now"

# ------------------------------------------------------------------ API user (zabbix-bootstrap)
API_ROLE = "NetSim API"
API_USER_GROUP = "NetSim API"
API_TOKEN_NAME = "netsim-api"

# ------------------------------------------------------------------ BSM, SLA, dashboard, map
SLA_NAME = "L2VPN {tier}"
DASHBOARD = "L2VPN Services"
MAP = "L2VPN Topology"
