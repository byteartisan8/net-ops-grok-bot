/*
 * netsim_cli.so - Clixon CLI plugin of the netsim-pe image (owner: 02-device-simulator).
 *
 * Operational "show" commands backed by simd: netsim_show("pw") runs `netsim-show pw`, which reads the
 * simd API on 127.0.0.1:9000 and prints a router-style table (oper status and the reason of every PW).
 * See devices/clixon/netsim.cli for the commands that use it.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/wait.h>

#include <cligen/cligen.h>
#include <clixon/clixon.h>
#include <clixon/clixon_cli.h>

#define NETSIM_SHOW "/opt/venv/bin/netsim-show"
#define MAX_ARGS    8

int netsim_show(clixon_handle h, cvec *cvv, cvec *argv);

/*! Run netsim-show with the callback's string arguments and wait for it */
int
netsim_show(clixon_handle h,
            cvec         *cvv,
            cvec         *argv)
{
    char   *args[MAX_ARGS + 2];
    int     n = 0;
    cg_var *cv = NULL;
    pid_t   pid;
    int     status;

    args[n++] = "netsim-show";
    while ((cv = cvec_each(argv, cv)) != NULL && n <= MAX_ARGS)
        args[n++] = cv_string_get(cv);
    args[n] = NULL;
    fflush(stdout);
    fflush(stderr);
    if ((pid = fork()) < 0){
        clixon_err(OE_UNIX, errno, "fork");
        return -1;
    }
    if (pid == 0){
        execv(NETSIM_SHOW, args);
        perror(NETSIM_SHOW);
        _exit(127);
    }
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR)
        ;
    return 0;
}

clixon_plugin_api *clixon_plugin_init(clixon_handle h);

static clixon_plugin_api api = {
    "netsim_cli",          /* name */
    clixon_plugin_init,    /* init */
    NULL,                  /* start */
    NULL,                  /* exit */
};

clixon_plugin_api *
clixon_plugin_init(clixon_handle h)
{
    return &api;
}
