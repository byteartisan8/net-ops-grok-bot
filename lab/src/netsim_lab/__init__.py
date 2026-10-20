"""NetSim L2VPN lab integration (owner: 05-integration).

* :mod:`netsim_lab.lab` - one object for the whole lab (devices, core, Docker, NetBox intent, Zabbix);
* :mod:`netsim_lab.drift` - NetBox intent vs running config, semantic findings per service;
* :mod:`netsim_lab.status` / :mod:`netsim_lab.impact` - combined service views;
* :mod:`netsim_lab.scenarios` / :mod:`netsim_lab.reset` - declarative scenarios and the reset procedure;
* :mod:`netsim_lab.changes` - propose -> approve -> apply -> verify for device config (lab-api);
* :mod:`netsim_lab.cli` (``labctl``) and :mod:`netsim_lab.api` (``lab-api``).
"""

__version__ = "0.1.0"
