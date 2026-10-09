"""The Python side's command line for the bake-off harness (WORKLOADS.md,
"What each side provides"). Foundation-written.

  python -m bakeoff seed --accounts N
  python -m bakeoff bench batch-payments --workers W
  python -m bakeoff bench batch-accrual --workers W
  python -m bakeoff bench batch-payments-bulk --workers W
  python -m bakeoff bench placement --workers W --accounts M
  python -m bakeoff bench placement-bulk --workers W --accounts M
  python -m bakeoff bench realtime --workers W --warmup S --duration S --random-seed R
  python -m bakeoff run-engine [--seconds-per-day D] [--poll-interval-ms P] [--rule-workers K] [--projection-workers J]
  python -m bakeoff verify-chain

Each prints one line of JSON on stdout when it ends; progress and logs go to
stderr. The database comes from the DATABASE_* environment variables.
"""

import argparse
import json
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    parser = argparse.ArgumentParser(prog="python -m bakeoff")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed")
    seed.add_argument("--accounts", type=int, required=True)

    bench = sub.add_parser("bench")
    bench.add_argument(
        "workload",
        choices=["batch-payments", "batch-accrual", "batch-payments-bulk", "placement", "placement-bulk", "realtime"],
    )
    bench.add_argument("--workers", type=int, required=True)
    bench.add_argument("--accounts", type=int, default=100_000, help="new accounts to place (placement workloads)")
    bench.add_argument("--warmup", type=float, default=10)
    bench.add_argument("--duration", type=float, default=60)
    bench.add_argument("--random-seed", default="1")

    engine = sub.add_parser("run-engine")
    engine.add_argument("--seconds-per-day", type=float, default=2)
    engine.add_argument("--poll-interval-ms", type=int, default=250)
    engine.add_argument("--rule-workers", type=int, default=1)
    engine.add_argument("--projection-workers", type=int, default=1)

    sub.add_parser("verify-chain")
    args = parser.parse_args()
    exit_code = 0

    if args.command == "bench":
        from bakeoff import drivers

        if args.workload == "realtime":
            result = drivers.run_realtime(args.workers, args.warmup, args.duration, args.random_seed)
        elif args.workload in ("placement", "placement-bulk"):
            result = drivers.run_placement(args.workload, args.workers, args.accounts)
        else:
            result = drivers.run_batch(args.workload, args.workers)
    elif args.command == "run-engine":
        from bakeoff.engine import run_engine

        exit_code, result = run_engine(args.seconds_per_day, args.poll_interval_ms, args.rule_workers, args.projection_workers)
    else:
        import django

        django.setup()
        if args.command == "seed":
            from bakeoff.seed import seed as load

            result = load(args.accounts)
        else:
            from bakeoff.verify import verify_chain

            result = verify_chain()
            exit_code = 0 if result["ok"] else 1
    print(json.dumps(result), flush=True)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
