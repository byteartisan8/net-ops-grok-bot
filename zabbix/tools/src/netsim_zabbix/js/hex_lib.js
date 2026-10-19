// NetSim: an InetAddress value as SNMP walk value/to JSON return it ("0A FF 00 01" for octets that are not
// printable UTF-8, else the characters themselves) -> dotted quad. Anything that is not 4 octets is returned as is.
function nsHexAddress(text) {
    var s = String(text === undefined || text === null ? '' : text).trim();
    var bytes = [];
    var i;
    if (/^([0-9A-Fa-f]{2})( [0-9A-Fa-f]{2})*$/.test(s)) {
        var parts = s.split(' ');
        for (i = 0; i < parts.length; i++) {
            bytes.push(parseInt(parts[i], 16));
        }
    }
    else {
        for (i = 0; i < s.length; i++) {
            bytes.push(s.charCodeAt(i));
        }
    }
    return bytes.length === 4 ? bytes.join('.') : s;
}
