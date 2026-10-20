// NetSim: "PW table" discovery rows from SNMP walk to JSON; cpwVcPeerAddr (4-octet InetAddress, contract
// section 8) arrives as hex ("0A FF 00 01") and becomes a dotted quad in {#PEER}. {#SERVICE} and {#ROLE} split
// cpwVcName ("<service>:<role>", contract section 8) so the "unexpected" problem carries the same service/pw_role
// tags as the PW checks: a moved PW's problems on several hosts then share the service tag (correlation, 05).
var rows = JSON.parse(value);
for (var i = 0; i < rows.length; i++) {
    rows[i]['{#PEER}'] = nsHexAddress(rows[i]['{#PEER}']);
    var name = String(rows[i]['{#VCNAME}'] || '');
    var sep = name.indexOf(':');
    rows[i]['{#SERVICE}'] = sep > 0 ? name.substring(0, sep) : name;
    rows[i]['{#ROLE}'] = sep > 0 ? name.substring(sep + 1) : '';
}
return JSON.stringify(rows);
