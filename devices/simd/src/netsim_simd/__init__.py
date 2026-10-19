"""simd: the brain of the ``netsim-pe`` device image (owner: 02-device-simulator).

simd watches the Clixon running config, simulates targeted-LDP signaling with its peers over the ``core``
Redis bus (contract section 7), evaluates every pseudowire with the contract's first-match table, feeds the
SNMP agent (snmpsim) through a SQLite database, sends traps and serves the state API on port 9000.

Pure logic lives in :mod:`netsim_simd.evaluate` (no I/O) so it can be tested table-driven; the asyncio
orchestration is :mod:`netsim_simd.daemon`.
"""

__version__ = "0.1.0"
