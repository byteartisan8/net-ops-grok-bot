// NetSim: the cpwVcTable row of the expected PW "{#SERVICE}:{#ROLE}" (contract section 8: cpwVcName =
// "<service-id>:<role>"), as JSON. A missing row is a defined value ("present": 0), never an error, so the
// "missing" check is reliable. Column OIDs come from contract/oids.yaml (generator placeholders).
var NAME = '{#SERVICE}:{#ROLE}';
var ENTRY = '__CPW_ENTRY__';
var walk = nsWalk(value);
var prefix = ENTRY + '.__COL_NAME__.';

for (var oid in walk) {
    if (oid.indexOf(prefix) === 0 && nsString(walk[oid]) === NAME) {
        var idx = oid.substring(prefix.length);
        var col = function (c) {
            return walk[ENTRY + '.' + c + '.' + idx];
        };
        return JSON.stringify({
            present: 1,
            index: parseInt(idx, 10),
            name: NAME,
            oper: nsInt(col('__COL_OPER__')),
            admin: nsInt(col('__COL_ADMIN__')),
            peer: nsInetAddress(col('__COL_PEER__')),
            vc_id: nsInt(col('__COL_VCID__')),
            remote_mtu: nsInt(col('__COL_RMTU__')),
            customer: nsString(col('__COL_DESCR__'))
        });
    }
}

return JSON.stringify({
    present: 0, index: 0, name: NAME, oper: 0, admin: 0, peer: 'absent', vc_id: 0, remote_mtu: 0, customer: ''
});
