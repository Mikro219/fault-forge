"""Command line for FaultForge. Exit codes: 0 PASS, 1 FAIL, 2 INCONCLUSIVE, 3 ERROR, 4 not comparable.

compare exits 0 only when the fix is verified, 1 when comparable but not verified, 4 when not comparable.
"""

import argparse
import json
import signal
import sys
from pathlib import Path

from faultforge import lifecycle, report as report_module, safety
from faultforge.errors import FaultForgeError
from faultforge.lifecycle import NotComparable, RunOptions

EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "ERROR": 3}
EXIT_ERROR = 3
EXIT_NOT_COMPARABLE = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="faultforge", description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="project directory")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="show scenarios and limits; sends no load")

    run = commands.add_parser("run", help="run a scenario; this mutates the target's data")
    run.add_argument("scenario")
    run.add_argument("--trials", type=int)
    run.add_argument("--serial-control", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--vus", type=int)
    run.add_argument("--iterations", type=int)
    run.add_argument("--max-duration-s", type=int)
    run.add_argument("--compare-to")
    run.add_argument(
        "--wait-for-target-s", type=float,
        help="poll target health for up to this long first (at most 30 s, outside the run budget)",
    )
    run.add_argument("--json", action="store_true", help="print report.json instead of the summary")

    show = commands.add_parser("show", help="show a stored run")
    show.add_argument("run_id")
    show.add_argument("--json", action="store_true")

    compare = commands.add_parser("compare", help="compare two stored runs")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    return parser


def _print_report(report, as_json: bool) -> None:
    if as_json:
        print(report.model_dump_json(indent=2))
        return
    print(report_module.render_markdown(report), end="")
    print(f"\nrun_id: {report.run_id}")


def _dispatch(args: argparse.Namespace) -> int:
    project_dir = safety.resolve_project_dir(args.project, safety.allowed_roots([args.project]))
    if args.command == "list":
        print(json.dumps(lifecycle.describe_scenarios(project_dir, [project_dir]), indent=2))
        return 0
    if args.command == "run":
        report = lifecycle.run_scenario(
            project_dir,
            args.scenario,
            RunOptions(
                trials=args.trials,
                serial_control=args.serial_control,
                vus=args.vus,
                iterations=args.iterations,
                max_duration_s=args.max_duration_s,
                compare_to=args.compare_to,
            ),
            roots=[project_dir],
            wait_for_target_s=args.wait_for_target_s,
        )
        _print_report(report, args.json)
        return EXIT_CODES[report.verdict]
    if args.command == "show":
        report = report_module.read_report(project_dir, args.run_id)
        _print_report(report, args.json)
        return EXIT_CODES[report.verdict]
    comparison = report_module.compare_reports(
        report_module.read_report(project_dir, args.baseline),
        report_module.read_report(project_dir, args.candidate),
    )
    print(comparison.model_dump_json(indent=2))
    if comparison.status == "NOT_COMPARABLE":
        return EXIT_NOT_COMPARABLE
    return 0 if comparison.fix_verified else EXIT_CODES["FAIL"]


def _exit_on_term(signum, frame) -> None:
    # Raising unwinds through the runner's cleanup, which stops k6's process group.
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _exit_on_term)
    signal.signal(signal.SIGHUP, _exit_on_term)
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        # argparse exits 2 on bad usage, which here would mean INCONCLUSIVE.
        return EXIT_ERROR if error.code else 0
    try:
        return _dispatch(args)
    except FaultForgeError as error:
        print(json.dumps(error.to_dict(), indent=2))
        return EXIT_ERROR
    except NotComparable as error:
        print(error.comparison.model_dump_json(indent=2))
        return EXIT_NOT_COMPARABLE
    except Exception as error:
        # A traceback would exit 1, which means FAIL. An unexpected crash proves nothing.
        failure = FaultForgeError("engine_failed", f"{type(error).__name__}: {error}")
        print(json.dumps(failure.to_dict(), indent=2))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
