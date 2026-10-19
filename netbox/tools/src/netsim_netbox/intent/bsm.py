"""The BSM spec (contract section 9.3) built from NetBox intent only; consumed by Zabbix's ``zabbix-sync``.

Tenants (``sla_tier``) -> customers, pseudowires grouped by ``service_id`` -> services, each L2VPN -> a PW leaf with
both ends (host, AC, expected peer = the other end's loopback) and ``vc_id`` = identifier. NetBox references:
customers carry tag ``netbox_tenant_id`` and the tenant's URL, PW leaves ``netbox_l2vpn_id`` (field and tag) and the
L2VPN's URL, services the URL of their L2VPN list, the root the URL of all ``l2vpn-pw`` L2VPNs. Built with the
``netsim_common.bsm`` builders, so ``spec.normalized() == bsm_spec_from_topology(...)`` on a freshly seeded NetBox.
"""

from __future__ import annotations

from netsim_common.bsm import BsmSpec, PwEnd, build_customer, build_pseudowire, build_root, build_service
from netsim_netbox.intent.model import IntentError, IntentProblem, LabIntent
from netsim_netbox.objects import CF_SERVICE_ID, PW_TAG


def bsm_spec(intent: LabIntent) -> BsmSpec:
    """The spec (sorted). :class:`IntentError` if any pseudowire or customer cannot be interpreted."""
    if problems := intent.spec_problems():
        raise IntentError(problems, "the BSM spec")
    base = intent.public_url
    problems_found: list[IntentProblem] = []
    services_by_customer: dict[str, list] = {name: [] for name, c in intent.customers.items() if c.in_group}
    for service_id, pws in intent.services().items():
        customer = pws[0].customer
        if customer not in intent.customers:
            problems_found.append(IntentProblem(f"service {service_id}: tenant {customer} has no SLA tier", "customer"))
            continue
        leaves = [
            build_pseudowire(
                service_id,
                pw.role,
                pw.vc_id,
                PwEnd(host=pw.a_end.host, ac=pw.a_end.ac, expected_peer=pw.z_end.loopback),
                PwEnd(host=pw.z_end.host, ac=pw.z_end.ac, expected_peer=pw.a_end.loopback),
                netbox_l2vpn_id=pw.l2vpn_id,
                netbox_url=pw.url,
                extra_tags={"netbox_l2vpn_id": str(pw.l2vpn_id)},
            )
            for pw in pws
        ]
        services_by_customer.setdefault(customer, []).append(
            build_service(
                service_id,
                customer,
                pws[0].a_end.host,
                leaves,
                netbox_url=f"{base}/vpn/l2vpns/?cf_{CF_SERVICE_ID}={service_id}",
            )
        )
    if problems_found:
        raise IntentError(problems_found, "the BSM spec")
    customers = []
    for name, services in sorted(services_by_customer.items()):
        c = intent.customers[name]
        customers.append(
            build_customer(
                name,
                c.sla_tier,
                services,
                netbox_url=c.url,
                extra_tags={"netbox_tenant_id": str(c.tenant_id)},
            )
        )
    root = build_root(netbox_url=f"{base}/vpn/l2vpns/?tag={PW_TAG}")
    return BsmSpec(root=root, customers=customers).sorted()
