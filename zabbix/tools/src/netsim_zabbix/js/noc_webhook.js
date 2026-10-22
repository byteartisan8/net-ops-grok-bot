// NetSim NOC (grok-bot): one Zabbix service alert -> POST JSON to grok-bot's /hooks/zabbix (contract 9.6).
// Media type "NetSim NOC (grok-bot)", used by the service action "NetSim: customer alert" (managed by
// zabbix-bootstrap; source zabbix/tools/src/netsim_zabbix/js/noc_webhook.js). Zabbix 8.0.0rc1 facts it relies on:
// * the kind comes from the message template ({ALERT.MESSAGE} = problem, recovery, update; the escalation step's
//   own message = escalation): {EVENT.UPDATE.STATUS} stays 0 in service update operations;
// * a customer going from degraded to down is an UPDATE of the same event: {EVENT.UPDATE.SEVERITY} is the new
//   severity, {EVENT.SEVERITY} stays the first one;
// * macros that do not apply to the alert stay unresolved ("{EVENT.RECOVERY.ID}" in a problem alert): sent as null.
var p = JSON.parse(value);
var KINDS = ['problem', 'recovery', 'update', 'escalation'];

function clean(v) {
    if (v === undefined || v === null) {
        return null;
    }
    v = String(v);
    return /^\{[A-Z0-9_.$#]+\}$/.test(v) ? null : v;
}

function number(v) {
    v = clean(v);
    return v === null || v === '' || isNaN(parseInt(v, 10)) ? null : parseInt(v, 10);
}

function tags(text) {
    var out = {};
    try {
        var list = JSON.parse(clean(text) || '[]');
        for (var i = 0; i < list.length; i++) {
            out[list[i].tag] = list[i].value;
        }
    } catch (e) {
        Zabbix.log(3, '[NetSim NOC] service tags are not JSON: ' + text);
    }
    return out;
}

var kind = String(clean(p.kind) || '').replace(/^\s+|\s+$/g, '').toLowerCase();
if (KINDS.indexOf(kind) < 0) {
    kind = 'problem';
}
var severity = clean(p.event_severity);
if (kind === 'update' && clean(p.update_severity)) {
    severity = clean(p.update_severity);
} else if (kind === 'recovery') {
    severity = 'OK';
}
var body = {
    source: 'zabbix',
    role: clean(p.role),
    kind: kind,
    event_id: clean(p.event_id),
    recovery_event_id: clean(p.recovery_event_id),
    severity: severity,
    event_severity: clean(p.event_severity),
    update_severity: clean(p.update_severity),
    status: clean(p.event_status),
    event_time: number(p.event_time),
    update_time: number(p.update_time),
    recovery_time: number(p.recovery_time),
    event_name: clean(p.event_name),
    service: {id: clean(p.service_id), name: clean(p.service_name), tags: tags(p.service_tags)},
    root_cause: clean(p.root_cause) || '',
    action: clean(p.action),
    subject: clean(p.subject)
};

var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.addHeader('Authorization: Bearer ' + p.token);
var resp = req.post(p.url, JSON.stringify(body));
var status = req.getStatus();
if (status < 200 || status >= 300) {
    // throwing makes Zabbix retry (the media type's attempts) and show the error in the action log
    throw 'grok-bot ' + p.url + ': HTTP ' + status + ' ' + String(resp || '').substring(0, 200);
}
Zabbix.log(4, '[NetSim NOC] ' + kind + ' ' + body.service.name + ' (' + severity + ') to ' + body.role + ': HTTP ' + status);
return JSON.stringify({kind: kind, role: body.role, http: status});
