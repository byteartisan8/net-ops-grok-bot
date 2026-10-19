// NetSim: "Expected PWs" discovery (contract section 9.2). The LLD data is the host's NetBox spec, i.e. the JSON
// lists in {$NETSIM.PW.SPEC} and, for hosts whose spec exceeds one macro value (2048 characters), the overflow
// macros {$NETSIM.PW.SPEC.2} .. (passed in as the script parameters spec1..specN, written by zabbix-sync).
var params = JSON.parse(value);
var rows = [];
var seen = {};
var names = Object.keys(params).sort(function (a, b) {
    return parseInt(a.slice(4), 10) - parseInt(b.slice(4), 10);
});

for (var i = 0; i < names.length; i++) {
    var part;
    try {
        part = JSON.parse(params[names[i]]);
    }
    catch (err) {
        throw 'NetSim: ' + names[i] + ' is not valid JSON (an unresolved or broken {$NETSIM.PW.SPEC*} macro?): ' + err;
    }
    if (!Array.isArray(part)) {
        throw 'NetSim: ' + names[i] + ' must be a JSON list';
    }
    for (var j = 0; j < part.length; j++) {
        var entry = part[j];
        if (typeof entry.service !== 'string' || typeof entry.role !== 'string' || typeof entry.vc_id !== 'number') {
            throw 'NetSim: malformed spec entry ' + JSON.stringify(entry);
        }
        entry.vcname = entry.service + ':' + entry.role;
        if (seen[entry.vcname]) {
            throw 'NetSim: duplicate spec entry ' + entry.vcname;
        }
        seen[entry.vcname] = true;
        rows.push(entry);
    }
}

return JSON.stringify(rows);
