"""Doctor: probe a real HA instance; validate connectivity and entities.

Run by hand against a live Home Assistant; the connection comes from LM_HA_BASE_URL and
LM_HA_TOKEN (or LM_HA_TOKEN_FILE):

    export LM_HA_BASE_URL=http://homeassistant.local:8123
    export LM_HA_TOKEN=...
    python tools/doctor.py [--config /data/config.json]

It checks REST and the WebSocket, reads the Area, entity, and device registries, and
prints the rooms Light Map would build from them, with anything that needs attention.
Given `--config`, it also shows what a sync would change in that committed snapshot, and
what deploying its switching plane (the `lm_` lane) would do: a preview of the plan, or why
the deploy would be refused.

Strictly read-only: it never writes to HA, and never writes the config.
"""

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Run as a script (`python tools/doctor.py`): put the repo root on the path for `app`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import config as config_mod  # noqa: E402
from app.core import deploy, generate, inventory, switching  # noqa: E402
from app.core.ha import HAAuthError, HAClient, HAError  # noqa: E402

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "


class Report:
    def __init__(self) -> None:
        self.results: list[str] = []

    def record(self, status: str, check: str, detail: str = "") -> None:
        self.results.append(status)
        print(f"[{status}] {check}" + (f"\n         {detail}" if detail else ""))

    def listing(self, status: str, check: str, lines: Sequence[str]) -> None:
        self.record(status, check, "\n         ".join(lines))

    def summary(self) -> int:
        fails, warns = self.results.count(FAIL), self.results.count(WARN)
        print("=" * 72)
        print(
            f"{len(self.results)} checks, {fails} failed, {warns} warnings "
            f"({datetime.now():%H:%M:%S})"
        )
        return 1 if fails else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Probe HA and report problems; return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--config", type=Path, help="a committed config to compare the live Areas against"
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="accept HA's TLS certificate unverified (self-signed HTTPS)",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds per HA request")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    report = Report()
    committed: dict[str, Any] | None = None
    if args.config is not None:
        try:
            committed = config_mod.load(args.config)
            report.record(OK, f"Config {args.config} loads and validates")
        except config_mod.ConfigError as exc:
            report.listing(FAIL, f"Config {args.config} does not load", exc.problems)
    base = committed or config_mod.blank()

    try:
        connection = config_mod.connection(base)
    except (config_mod.ConfigError, HAError) as exc:
        print(f"{exc}\nSet LM_HA_BASE_URL and LM_HA_TOKEN (or LM_HA_TOKEN_FILE) first.")
        return 2
    if args.no_verify_ssl:
        connection["verify_ssl"] = False

    print(f"\nLight Map doctor -> {connection['base_url']}\n" + "=" * 72)
    async with HAClient(**connection, timeout=args.timeout) as ha:
        if not await _probe(ha, report):
            return report.summary()
        try:
            areas = await ha.list_areas()
            lights = await inventory.discover_lights(ha)
        except HAError as exc:
            report.record(FAIL, "Could not read the Area, entity, and device registries", str(exc))
            return report.summary()
        report.record(OK, "Read the Area, entity, and device registries")
        _inspect(report, areas, lights)
        snapshot = config_mod.reconcile_area_snapshot(base, areas, lights)
        _print_rooms(snapshot)
        _check_snapshot(report, committed, snapshot)
        if committed is not None:
            await _check_switching(report, ha, committed)
    return report.summary()


async def _probe(ha: HAClient, report: Report) -> bool:
    try:
        await ha.ping()
        report.record(OK, "REST API reachable and token accepted")
    except HAAuthError:
        report.record(FAIL, "Token rejected", "Create a new long-lived access token in HA.")
        return False
    except HAError as exc:
        report.record(FAIL, "REST API unreachable", str(exc))
        return False
    try:
        info = await ha.get_config()
        report.record(
            OK, f"Home Assistant {info.get('version')}", f"time zone {info.get('time_zone')}"
        )
    except HAError as exc:
        report.record(WARN, "Could not read HA's core config", str(exc))

    try:
        await ha.connect()
        report.record(OK, f"WebSocket authenticated (HA {ha.ha_version})")
    except HAError as exc:
        report.record(FAIL, "WebSocket did not authenticate", str(exc))
        return False

    try:
        user = await ha.command({"type": "auth/current_user"})
    except HAError as exc:
        report.record(WARN, "Could not tell whether the token's user is an administrator", str(exc))
        return True
    if not (user or {}).get("is_admin"):
        report.record(
            WARN,
            "The token's user is not an administrator",
            "Reading works, but moving bulbs and creating or renaming rooms (Area writes)\n"
            "         need an administrator's token.",
        )
    return True


def _inspect(
    report: Report, areas: Sequence[Mapping[str, Any]], lights: Sequence[Mapping[str, Any]]
) -> None:
    known_areas = {a.get("area_id") for a in areas}
    bulbs = [light for light in lights if not light["is_group"]]
    groups = [light for light in lights if light["is_group"]]
    report.record(
        OK,
        "Inventory",
        f"{len(areas)} Areas, {len(bulbs)} lights, {len(groups)} light groups",
    )

    def names(items: Sequence[Mapping[str, Any]]) -> list[str]:
        return [f"{light['entity_id']}  ({light['name']})" for light in items]

    if groups:
        report.listing(
            WARN,
            f"{len(groups)} light groups are skipped: a group is not a bulb",
            ["Use the member lights, joined by a switching group, instead:", *names(groups)],
        )
    unregistered = [b for b in bulbs if not b["in_registry"]]
    if unregistered:
        report.listing(
            WARN,
            f"{len(unregistered)} lights have no entity-registry entry (no unique_id)",
            ["HA can't give these an Area, so they stay Unassigned:", *names(unregistered)],
        )
    stale = [b for b in bulbs if b["area_id"] and b["area_id"] not in known_areas]
    if stale:
        report.listing(
            WARN,
            f"{len(stale)} lights point at an Area HA doesn't list",
            [f"{b['entity_id']}  -> {b['area_id']}" for b in stale],
        )
    unassigned = [b for b in bulbs if not b["area_id"] and b["in_registry"]]
    if unassigned:
        report.listing(
            WARN, f"{len(unassigned)} lights have no Area (Unassigned)", names(unassigned)
        )
    unavailable = [b for b in bulbs if b["state"] in ("unavailable", "unknown")]
    if unavailable:
        report.listing(
            WARN,
            f"{len(unavailable)} lights are unavailable right now",
            names(unavailable),
        )


def _print_rooms(snapshot: Mapping[str, Any]) -> None:
    rooms = snapshot["rooms"]
    print(f"\nRooms Light Map would build ({len(rooms)}):")
    for room in rooms:
        extra = (
            f", {len(room['switching_groups'])} switching groups"
            if room["switching_groups"]
            else ""
        )
        adaptive = ", adaptive" if room["adaptive"] else ""
        print(f"  {room['name']}  [room {room['id']}, Area {room['ha_area_id']}{adaptive}{extra}]")
        for bulb in room["bulbs"]:
            print(f"      {bulb['id']:<28} {bulb['entity_id']:<36} {bulb['name']}")
    if snapshot["unassigned"]:
        print(f"  {inventory.UNASSIGNED}")
        for bulb in snapshot["unassigned"]:
            print(f"      {bulb['id']:<28} {bulb['entity_id']:<36} {bulb['name']}")
    print()


def _check_snapshot(
    report: Report, committed: Mapping[str, Any] | None, snapshot: Mapping[str, Any]
) -> None:
    try:
        config_mod.validate(snapshot)
        config_mod.cross_check(snapshot)
        report.record(OK, "The snapshot built from HA validates")
    except config_mod.ConfigError as exc:
        report.listing(FAIL, "The snapshot built from HA does not validate", exc.problems)
    if committed is None:
        return
    changes = config_mod.snapshot_changes(committed, snapshot)
    if changes:
        report.listing(
            WARN,
            f"The committed snapshot is stale: a sync would make {len(changes)} changes",
            changes,
        )
    else:
        report.record(OK, "The committed snapshot matches HA")


async def _check_switching(report: Report, ha: HAClient, committed: Mapping[str, Any]) -> None:
    """Preview a deploy of the committed config's switching plane, without writing."""
    try:
        await deploy.check_references(ha, switching.references(committed))
        deploy_plan = deploy.plan(
            switching.PREFIX,
            generate.generate(committed, switching.PREFIX),
            await deploy.fetch_live(ha, switching.PREFIX),
            mirrors=switching.bridge_mirrors(committed),
        )
    except deploy.DeployError as exc:
        report.listing(FAIL, "A deploy of the switching plane would be refused", exc.problems)
        return
    except HAError as exc:
        report.record(WARN, "Could not read the switching plane's lane (lm_)", str(exc))
        return
    preview = deploy.summarise(deploy_plan)
    lines = [
        f"{verb} {kind} {item_id}"
        for label, verb in (("created", "create"), ("updated", "update"), ("removed", "remove"))
        for kind, ids in preview[label].items()
        for item_id in ids
    ]
    lines += [f"set {helper} to its bulb's current state first" for helper in preview["sync"]]
    lines += preview["warnings"]
    if lines:
        report.listing(
            OK, f"A deploy of the switching plane would make {len(lines)} changes", lines
        )
    else:
        report.record(OK, "The switching plane in HA matches the committed config")


if __name__ == "__main__":
    raise SystemExit(main())
