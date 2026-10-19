// NetSim: how the PW row differs from its NetBox spec, in words (used in the "spec" event name), e.g.
// "peer 10.255.0.2, NetBox expects 10.255.0.1 (hub-a)". "matches NetBox" when it doesn't, "absent" without a row.
var row = JSON.parse(value);
if (!row.present) {
    return 'absent';
}
var diff = [];
if (row.peer !== '{#EXPECTED_PEER}') {
    diff.push('peer ' + row.peer + ', NetBox expects {#EXPECTED_PEER} ({#EXPECTED_PEER_HOST})');
}
if (row.vc_id !== {#VC_ID}) {
    diff.push('VC ID ' + row.vc_id + ', NetBox expects {#VC_ID}');
}
return diff.length ? diff.join('; ') : 'matches NetBox';
