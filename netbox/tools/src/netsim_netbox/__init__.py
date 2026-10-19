"""NetBox as the source of truth of the NetSim L2VPN lab (owner: 03-netbox).

* :mod:`netsim_netbox.bootstrap` (``netbox-bootstrap``): waits for NetBox and provisions the API tokens
  ``NETBOX_TOKEN_RW`` / ``NETBOX_TOKEN_RO`` (v2 tokens, written into ``.env``);
* :mod:`netsim_netbox.seed` (``netbox-seed``): makes NetBox match ``contract/topology.yaml`` (contract section 10),
  idempotently, with ``--dry-run`` and ``--prune``;
* :mod:`netsim_netbox.intent` (``netbox-intent``): reads intent from NetBox **only** (never the topology) and renders
  device configs, the BSM spec and the read-only queries the bot uses.

Everything talks plain REST to NetBox through :class:`netsim_netbox.client.NetBoxClient`.
"""

__version__ = "0.1.0"
