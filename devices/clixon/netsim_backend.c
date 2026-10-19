/*
 * netsim_backend.so - Clixon backend plugin of the netsim-pe image (owner: 02-device-simulator).
 *
 * Emits the standard RFC 6470 notification ietf-netconf-notifications:netconf-config-change on the
 * RFC 5277 default stream "NETCONF" after every commit that changed the running datastore, like a real
 * router does. Any NETCONF client (create-subscription) or RESTCONF client (GET /streams/NETCONF,
 * server-sent events) can subscribe; simd uses it to see config changes within milliseconds.
 *
 *   changed-by  <username> of the committing session (CLI: the unix user, RESTCONF: CLICON_ANONYMOUS_USER)
 *               and session-id 0 (RFC 6470: "a non-NETCONF session MAY be identified by the value zero"),
 *               or <server/> for the commit that loads the datastore at boot
 *   datastore   running
 *   edit        one per changed node: target = instance-identifier, operation = delete | create | replace
 *
 * The notification text is built in the commit-done callback (the transaction diff vectors are still
 * valid there) and sent from the transaction-end callback, i.e. after the running datastore was written,
 * so a subscriber that reads the config when notified always sees the new content.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <syslog.h>
#include <sys/time.h>

#include <cligen/cligen.h>
#include <clixon/clixon.h>
#include <clixon/clixon_backend.h>

#define NETCONF_STREAM  "NETCONF"
#define NCN_NAMESPACE   "urn:ietf:params:xml:ns:yang:ietf-netconf-notifications"
#define NSD_PREFIX      "nsd"
#define NSD_NAMESPACE   "urn:netsim:device"

/* Append one <edit> per node of a transaction vector (best effort: a node whose path cannot be built is skipped) */
static int
append_edits(cbuf       *cb,
             cvec       *nsc,
             cxobj     **vec,
             size_t      len,
             const char *operation)
{
    size_t i;
    char  *xpath;

    for (i = 0; i < len; i++){
        xpath = NULL;
        if (vec[i] == NULL || xml2xpath(vec[i], nsc, 0, 1, &xpath) < 0 || xpath == NULL){
            if (xpath)
                free(xpath);
            continue;
        }
        cprintf(cb, "<edit><target xmlns:%s=\"%s\">", NSD_PREFIX, NSD_NAMESPACE);
        if (xml_chardata_cbuf_append(cb, 0, xpath) < 0){
            free(xpath);
            return -1;
        }
        cprintf(cb, "</target><operation>%s</operation></edit>", operation);
        free(xpath);
    }
    return 0;
}

static int
netsim_commit_done(clixon_handle    h,
                   transaction_data td)
{
    int   retval = -1;
    cbuf *cb = NULL;
    cvec *nsc = NULL;
    char *user;
    char *text;

    if (transaction_dlen(td) + transaction_alen(td) + transaction_clen(td) == 0)
        return 0; /* nothing changed in running */
    if ((cb = cbuf_new()) == NULL){
        clixon_err(OE_UNIX, errno, "cbuf_new");
        goto done;
    }
    if ((nsc = xml_nsctx_init(NSD_PREFIX, NSD_NAMESPACE)) == NULL)
        goto done;
    cprintf(cb, "<netconf-config-change xmlns=\"%s\"><changed-by>", NCN_NAMESPACE);
    user = clicon_username_get(h);
    if (user && strlen(user)){
        cprintf(cb, "<username>");
        if (xml_chardata_cbuf_append(cb, 0, user) < 0)
            goto done;
        cprintf(cb, "</username><session-id>0</session-id>");
    }
    else
        cprintf(cb, "<server/>");
    cprintf(cb, "</changed-by><datastore>running</datastore>");
    if (append_edits(cb, nsc, transaction_dvec(td), transaction_dlen(td), "delete") < 0 ||
        append_edits(cb, nsc, transaction_avec(td), transaction_alen(td), "create") < 0 ||
        append_edits(cb, nsc, transaction_tcvec(td), transaction_clen(td), "replace") < 0)
        goto done;
    cprintf(cb, "</netconf-config-change>");
    if ((text = strdup(cbuf_get(cb))) == NULL){
        clixon_err(OE_UNIX, errno, "strdup");
        goto done;
    }
    if ((user = transaction_arg(td)) != NULL)
        free(user);
    transaction_arg_set(td, text);
    retval = 0;
 done:
    if (nsc)
        xml_nsctx_free(nsc);
    if (cb)
        cbuf_free(cb);
    return retval;
}

static int
netsim_trans_end(clixon_handle    h,
                 transaction_data td)
{
    char *text;

    if ((text = transaction_arg(td)) == NULL)
        return 0;
    transaction_arg_set(td, NULL);
    if (stream_notify(h, NETCONF_STREAM, "%s", text) < 0)
        clixon_log(h, LOG_WARNING, "netsim: netconf-config-change notification failed");
    free(text);
    return 0;
}

static int
netsim_trans_abort(clixon_handle    h,
                   transaction_data td)
{
    char *text;

    if ((text = transaction_arg(td)) != NULL){
        transaction_arg_set(td, NULL);
        free(text);
    }
    return 0;
}

clixon_plugin_api *clixon_plugin_init(clixon_handle h);

static clixon_plugin_api api = {
    "netsim",                 /* name */
    clixon_plugin_init,       /* init */
    NULL,                     /* start */
    NULL,                     /* exit */
    .ca_trans_commit_done = netsim_commit_done,
    .ca_trans_end = netsim_trans_end,
    .ca_trans_abort = netsim_trans_abort,
};

clixon_plugin_api *
clixon_plugin_init(clixon_handle h)
{
    struct timeval retention = {0, 0};

    if (clicon_option_exists(h, "CLICON_STREAM_RETENTION"))
        retention.tv_sec = clicon_option_int(h, "CLICON_STREAM_RETENTION");
    if (stream_add(h, NETCONF_STREAM, "Default NETCONF event stream (RFC 5277): netconf-config-change", 1, &retention) < 0)
        return NULL;
    return &api;
}
