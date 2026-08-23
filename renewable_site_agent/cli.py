"""CLI for the same durable queue used by the API and Web Demo."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.durable.store import DurableJobStore
from renewable_site_agent.durable.worker import CanonicalWorker
from renewable_site_agent.service import CanonicalSiteSelectionService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="renewable-site-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Enqueue a site-selection job")
    create.add_argument("--request", required=True)
    create.add_argument("--idempotency-key")
    show = commands.add_parser("show", help="Show a durable job")
    show.add_argument("job_id")
    events = commands.add_parser("events", help="Show job events")
    events.add_argument("job_id")
    commands.add_parser("work-once", help="Process jobs until the queue is idle")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = DurableJobStore()
    if args.command == "create":
        job, reused = CanonicalSiteSelectionService(store=store).submit(
            args.request, idempotency_key=args.idempotency_key
        )
        print(json.dumps({"job_id": job["job_id"], "status": job["status"], "idempotency_hit": reused}, ensure_ascii=False, indent=2))
    elif args.command == "show":
        job = store.get_job(args.job_id)
        job["steps"] = store.steps(args.job_id)
        job["artifacts"] = store.artifacts(args.job_id)
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif args.command == "events":
        print(json.dumps(store.events(args.job_id), ensure_ascii=False, indent=2))
    elif args.command == "work-once":
        CanonicalWorker(store=store).run_forever(stop_after_idle=True)


if __name__ == "__main__":
    main()
