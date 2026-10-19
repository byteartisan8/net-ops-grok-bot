// NetSim: 1 if the observed PW (its cpwVcName, from SNMP walk value) is in this host's NetBox spec, else 0.
// The spec macros are JSON lists, i.e. valid JavaScript literals; Zabbix expands user macros in preprocessing
// scripts (generator placeholder: one macro per spec chunk).
var spec = [].concat(__SPEC_MACROS__);
for (var i = 0; i < spec.length; i++) {
    if (spec[i].service + ':' + spec[i].role === value) {
        return 1;
    }
}
return 0;
