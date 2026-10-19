// NetSim: "check now" after an SNMP trap (contract section 9.1: traps are hints, polling is truth).
// Webhook global script, run by the action "NetSim: SNMP trap -> check now" on the trap trigger's problem event.
// Parameters: api_url, token (secret global macros), hostid ({HOST.ID}), trap ({EVENT.TAGS.trap}), walk_key,
// event ({EVENT.ID}). trap=pw: execute now the walk master item (every PW/interface item and the dependent "PW table"
// and interface discoveries follow from its value). trap=config|boot: also the non-dependent discovery rules
// (the "Expected PWs" spec discovery), since a config change or a reboot may add or remove PWs.
var p = JSON.parse(value);

function api(method, params) {
    var req = new HttpRequest();
    req.addHeader('Content-Type: application/json-rpc');
    req.addHeader('Authorization: Bearer ' + p.token);
    var raw = req.post(p.api_url, JSON.stringify({jsonrpc: '2.0', method: method, params: params, id: 1}));
    if (req.getStatus() !== 200) {
        throw 'NetSim check-now: ' + method + ': HTTP ' + req.getStatus();
    }
    var resp = JSON.parse(raw);
    if (resp.error) {
        throw 'NetSim check-now: ' + method + ': ' + resp.error.message + ' ' + resp.error.data;
    }
    return resp.result;
}

var ids = [];
var what = [];
var items = api('item.get', {hostids: [p.hostid], filter: {key_: [p.walk_key]}, output: ['itemid', 'key_']});
for (var i = 0; i < items.length; i++) {
    ids.push(items[i].itemid);
    what.push(items[i].key_);
}
if (p.trap === 'config' || p.trap === 'boot') {
    // type 18 = dependent: those are refreshed by the walk value itself.
    var rules = api('discoveryrule.get', {hostids: [p.hostid], output: ['itemid', 'key_', 'type']});
    for (var j = 0; j < rules.length; j++) {
        if (parseInt(rules[j].type, 10) !== 18) {
            ids.push(rules[j].itemid);
            what.push(rules[j].key_);
        }
    }
}
if (ids.length === 0) {
    throw 'NetSim check-now: host ' + p.hostid + ' has no item ' + p.walk_key;
}

var tasks = [];
for (var k = 0; k < ids.length; k++) {
    tasks.push({type: 6, request: {itemid: ids[k]}});
}
var result = api('task.create', tasks);
Zabbix.log(4, '[NetSim check-now] event ' + p.event + ' trap=' + p.trap + ' host ' + p.hostid + ': ' + what.join(', ')
    + ' -> tasks ' + result.taskids.join(','));
return JSON.stringify({trap: p.trap, executed: what, taskids: result.taskids});
