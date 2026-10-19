// NetSim: helpers for a Zabbix SNMP walk[] value (prepended to the scripts that read the raw walk).
// Format (Zabbix 8.0 poller): one ".<numeric OID> = <TYPE>: <value>" line per varbind, numeric enums and TimeTicks,
// strings quoted with \" and \\ escaped, an empty string as "" without a type, octets that are not printable
// UTF-8 as "Hex-STRING: 0A FF 00 01".
function nsWalk(text) {
    var out = {};
    var lines = String(text).split('\n');
    for (var i = 0; i < lines.length; i++) {
        var m = /^\.?([0-9]+(?:\.[0-9]+)*) = (?:([A-Za-z0-9-]+): )?(.*)$/.exec(lines[i]);
        if (m) {
            out[m[1]] = {type: m[2] || 'STRING', value: m[3]};
        }
    }
    return out;
}

function nsString(v) {
    if (!v) {
        return '';
    }
    var s = v.value;
    if (s.length >= 2 && s.charAt(0) === '"' && s.charAt(s.length - 1) === '"') {
        s = s.slice(1, -1).replace(/\\(["\\])/g, '$1');
    }
    return s;
}

function nsInt(v) {
    var n = v ? parseInt(v.value, 10) : NaN;
    return isNaN(n) ? 0 : n;
}

// InetAddress (4 octets) -> dotted quad; anything else is returned as the raw text.
function nsInetAddress(v) {
    if (!v) {
        return '';
    }
    var bytes = [];
    var i;
    if (v.type === 'Hex-STRING') {
        var parts = v.value.trim().split(/\s+/);
        for (i = 0; i < parts.length; i++) {
            bytes.push(parseInt(parts[i], 16));
        }
    }
    else {
        var s = nsString(v);
        for (i = 0; i < s.length; i++) {
            bytes.push(s.charCodeAt(i));
        }
    }
    return bytes.length === 4 ? bytes.join('.') : v.value;
}
