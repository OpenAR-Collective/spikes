"""run-engine: the long-running services of one run (WORKLOADS.md, "What each
side provides"). Foundation-written.

It starts J projection workers (the prototype's own reader loop), K rule
runner partitions, and K register poller partitions, each in its own
process, and stops them all when it is stopped. K sets both, as on the C#
side. Idle readers and pollers sleep the poll interval.
"""

import logging
import multiprocessing as mp
import signal
import sys
import time

from bakeoff.drivers import setup
from bakeoff.keys import REGISTER, RULE_RUNNER

logger = logging.getLogger("bakeoff.engine")


def _projection_worker(poll_seconds):
    setup(count_retries=False)
    from django.core.management import call_command

    call_command("run_projections", poll_seconds=poll_seconds)


def _rule_runner(partition, partitions, idle_seconds, seconds_per_day):
    setup(count_retries=False)
    from bakeoff import rules
    from wax.rules import RuleRunner

    rules.SETTINGS["seconds_per_day"] = seconds_per_day
    RuleRunner(actor_pk=RULE_RUNNER, partition=partition, partitions=partitions).loop(idle_seconds)


def _register_poller(partition, partitions, poll_seconds):
    setup(count_retries=False)
    from hivear.commands import FirePendingEvent
    from wax import register
    from wax.commands import CommandRejected, dispatch

    def fire(key):
        try:
            dispatch(FirePendingEvent(pending_event_pk=key), actor_pk=REGISTER, actor_type="system", source="register")
        except CommandRejected as exc:
            logger.warning("fire %s refused: %s", key, exc)

    register.loop(fire, poll_seconds, partition=partition, partitions=partitions)


def run_engine(seconds_per_day, poll_interval_ms, rule_workers, projection_workers):
    poll_seconds = poll_interval_ms / 1000
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_projection_worker, args=(poll_seconds,), name=f"projections-{j}") for j in range(projection_workers)]
    procs += [
        ctx.Process(target=_rule_runner, args=(k, rule_workers, poll_seconds, seconds_per_day), name=f"rules-{k}")
        for k in range(rule_workers)
    ]
    procs += [
        ctx.Process(target=_register_poller, args=(k, rule_workers, poll_seconds), name=f"register-{k}")
        for k in range(rule_workers)
    ]
    for proc in procs:
        proc.start()
    print(f"[run-engine] {len(procs)} processes started", file=sys.stderr, flush=True)

    stopping = {"now": False}

    def stop(signum, frame):
        stopping["now"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = time.monotonic()
    exit_code = 0
    while not stopping["now"]:
        dead = [proc for proc in procs if not proc.is_alive()]
        if dead:
            logger.error("engine process %s exited with %s; stopping", dead[0].name, dead[0].exitcode)
            exit_code = 1
            break
        time.sleep(0.5)
    for proc in procs:
        proc.terminate()
    for proc in procs:
        proc.join(10)
    return exit_code, {
        "workload": "run-engine",
        "seconds_per_day": seconds_per_day,
        "poll_interval_ms": poll_interval_ms,
        "rule_workers": rule_workers,
        "projection_workers": projection_workers,
        "elapsed_s": round(time.monotonic() - started, 3),
    }
