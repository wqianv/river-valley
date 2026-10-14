"""Command line entry point: python -m river_valley."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .core import Action, World, replay


def apply_input(world, entry):
    if entry["type"] == "rainfall":
        world.set_rainfall(entry["value"])
    elif entry["type"] == "action":
        world.act(Action(entry["actor"], entry["kind"], entry.get("target"), entry.get("amount", 0)))
    else:
        raise ValueError("input type must be action or rainfall")


def simulate(args):
    days = args.days if args.days is not None else args.years * 360
    if days < 0:
        raise ValueError("duration cannot be negative")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(args.output or f"runs/{args.scenario}-{args.seed}-{stamp}")
    output.mkdir(parents=True, exist_ok=True)
    entries = []
    if args.inputs:
        entries = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("inputs must be a JSON list")
    schedule = {}
    for entry in entries:
        if type(entry.get("day")) is not int or not 0 <= entry["day"] < days:
            raise ValueError("scheduled day must be inside the simulation")
        schedule.setdefault(entry["day"], []).append(entry)
    started = time.perf_counter()
    with ExitStack() as stack:
        sink = (stack.enter_context((output / "events.jsonl").open("w", encoding="utf-8"))
                if not args.no_events else None)
        world = World(args.seed, args.scenario, sink)
        history = [world.summary()]
        for day in range(days):
            for entry in schedule.get(day, []):
                apply_input(world, entry)
            world.step()
            if world.state["day"] % 360 == 0:
                history.append(world.summary())
                world.save(output / f"year-{world.state['day'] // 360:02d}.json")
        world.save(output / "snapshot.json")
        report = world.summary()
        report.update(elapsed_seconds=round(time.perf_counter() - started, 4),
                      history=history, event_log=not args.no_events)
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
    print(json.dumps(dict(output=str(output), **report), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="河谷 River Valley v0.1")
    sub = parser.add_subparsers(dest="command", required=True)
    sim = sub.add_parser("simulate", help="run autonomous daily simulation")
    sim.add_argument("--seed", type=int, default=42)
    sim.add_argument("--scenario", choices=("normal", "drought"), default="normal")
    duration = sim.add_mutually_exclusive_group()
    duration.add_argument("--years", type=int, default=10)
    duration.add_argument("--days", type=int)
    sim.add_argument("--output", help="output directory; existing named files are replaced")
    sim.add_argument("--inputs", help="JSON list of scheduled interventions")
    sim.add_argument("--no-events", action="store_true", help="omit full log for faster exploratory runs")
    history = sub.add_parser("replay", help="verify log and reconstruct state without running NPCs")
    history.add_argument("log")
    history.add_argument("--day", type=int, help="reconstruct beginning of this day")
    history.add_argument("--save", help="write resumable reconstructed snapshot")
    web = sub.add_parser("serve", help="start the local 2D map and action controls")
    web.add_argument("--seed", type=int, default=42)
    web.add_argument("--scenario", choices=("normal", "drought"), default="normal")
    web.add_argument("--load", help="resume a snapshot")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--output", default="runs/interactive", help="interactive log and checkpoint directory")
    args = parser.parse_args()
    try:
        if args.command == "simulate":
            simulate(args)
        elif args.command == "replay":
            with open(args.log, encoding="utf-8") as file:
                world = replay(file, until_day=args.day)
            if args.save:
                world.save(args.save)
            print(json.dumps(world.summary(), ensure_ascii=False, indent=2))
        else:
            from .web import serve
            serve(args)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    main()
