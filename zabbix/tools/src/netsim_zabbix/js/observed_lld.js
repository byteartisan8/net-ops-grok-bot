// NetSim: "PW table" discovery rows from SNMP walk to JSON; cpwVcPeerAddr (4-octet InetAddress, contract
// section 8) arrives as hex ("0A FF 00 01") and becomes a dotted quad in {#PEER}.
var rows = JSON.parse(value);
for (var i = 0; i < rows.length; i++) {
    rows[i]['{#PEER}'] = nsHexAddress(rows[i]['{#PEER}']);
}
return JSON.stringify(rows);
