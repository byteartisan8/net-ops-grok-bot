"""snmpsim variation module ``netsim`` (snmp-command-responder --variation-modules-dir=<this directory>).

snmpsim executes this file and calls init/variate/shutdown; the implementation and its documentation live in
``netsim_simd.snmp_variation`` (it serves the MIB view simd writes to SQLite).
"""

from netsim_simd.snmp_variation import init, shutdown, variate

__all__ = ["init", "shutdown", "variate"]
