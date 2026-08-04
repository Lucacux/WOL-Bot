"""CLI local para que otras automatizaciones cooperen con WOL-Bot."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import config
from maintenance import LeaseConflict, acquire_lease, active_lease, release_lease
from orchestrator import ensure_online, restore_power_state
from schedule_store import should_be_online


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control local de WOL-Bot")
    sub = parser.add_subparsers(dest="command", required=True)

    ensure = sub.add_parser("ensure-online", help="encender y esperar a un servidor")
    ensure.add_argument("server", choices=config.SERVERS)
    ensure.add_argument("--attempts", type=int, default=3)
    ensure.add_argument("--attempt-timeout", type=int, default=180)
    ensure.add_argument("--poll-seconds", type=int, default=10)
    ensure.add_argument("--boot-grace", type=int, default=90)
    ensure.add_argument("--json", action="store_true")

    acquire = sub.add_parser("maintenance-acquire", help="tomar una reserva con TTL")
    acquire.add_argument("server", choices=config.SERVERS)
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--ttl", type=int, required=True)

    release = sub.add_parser("maintenance-release", help="liberar una reserva propia")
    release.add_argument("server", choices=config.SERVERS)
    release.add_argument("--owner", required=True)

    status = sub.add_parser("maintenance-status", help="consultar una reserva")
    status.add_argument("server", choices=config.SERVERS)

    restore = sub.add_parser(
        "power-restore",
        help="apagar un server que encendiste, si está fuera de su franja",
    )
    restore.add_argument("server", choices=config.SERVERS)
    restore.add_argument(
        "--owner",
        default="",
        help="ignorar la reserva propia con este owner (la de otro siempre bloquea)",
    )
    restore.add_argument("--json", action="store_true")

    window = sub.add_parser(
        "schedule-window",
        help="¿el horario dice que este server debería estar encendido ahora?",
    )
    window.add_argument("server", choices=config.SERVERS)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "ensure-online":
        result = await ensure_online(
            args.server,
            attempts=args.attempts,
            attempt_timeout=args.attempt_timeout,
            poll_seconds=args.poll_seconds,
            boot_grace=args.boot_grace,
        )
        payload = result.as_dict()
        print(json.dumps(payload) if args.json else payload["reason"])
        return 0 if result.ready else 4

    if args.command == "maintenance-acquire":
        try:
            lease = acquire_lease(args.server, args.owner, args.ttl)
        except LeaseConflict as exc:
            print(str(exc), file=sys.stderr)
            return 5
        print(json.dumps({
            "server_key": lease.server_key,
            "owner": lease.owner,
            "expires_at": lease.expires_at,
        }))
        return 0

    if args.command == "maintenance-release":
        released = release_lease(args.server, args.owner)
        print(json.dumps({"server_key": args.server, "released": released}))
        return 0 if released else 6

    if args.command == "power-restore":
        result = await restore_power_state(args.server, owner=args.owner)
        payload = result.as_dict()
        print(json.dumps(payload) if args.json else payload["reason"])
        return 0 if result.ok else 7

    if args.command == "schedule-window":
        expected, reason = should_be_online(args.server)
        print(json.dumps({
            "server_key": args.server,
            "should_be_online": expected,
            "reason": reason,
        }))
        return 0

    lease = active_lease(args.server)
    print(json.dumps(None if lease is None else {
        "server_key": lease.server_key,
        "owner": lease.owner,
        "expires_at": lease.expires_at,
        "remaining_seconds": lease.remaining_seconds,
    }))
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
