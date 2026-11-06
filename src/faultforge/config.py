"""Loads faultforge.yaml. The file is untrusted: it can't run commands or raise any limit."""

import json
import re
from pathlib import Path

import yaml

from faultforge import expressions, k6_runner, safety
from faultforge.errors import FaultForgeError
from faultforge.models import ProjectConfig, ScenarioConfig

CONFIG_NAME = "faultforge.yaml"
MAX_CONFIG_BYTES = 256_000
MAX_BODY_BYTES = 16_000
MAX_YAML_DEPTH = 32
CAPTURE_FIELD = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _invalid(message: str, hint: str = "") -> FaultForgeError:
    return FaultForgeError("config_invalid", message, hint or f"Fix {CONFIG_NAME}.")


def _check_body(label: str, body: dict | None) -> None:
    if body is None:
        return
    try:
        encoded = json.dumps(body, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise _invalid(f"{label} can't be sent as JSON: {error}") from error
    if len(encoded) > MAX_BODY_BYTES:
        raise _invalid(f"{label} is larger than {MAX_BODY_BYTES} bytes")


def _check_scenario(scenario: ScenarioConfig) -> None:
    safety.validate_path(scenario.request.path)
    _check_body(f"{scenario.id} request body", scenario.request.json_body)
    if scenario.script not in k6_runner.SCRIPTS:
        raise _invalid(f"{scenario.id} uses unknown script {scenario.script!r}")
    capture = scenario.outcomes.capture_field
    if capture is not None and not CAPTURE_FIELD.fullmatch(capture):
        raise _invalid(f"{scenario.id} has an invalid capture_field")
    if any(
        status < 100 or status > 599
        for status in scenario.outcomes.accepted_status + scenario.outcomes.rejected_status
    ):
        raise _invalid(f"{scenario.id} has an invalid status code")
    for invariant in scenario.invariants:
        try:
            parsed = expressions.parse(invariant.check)
        except expressions.ExpressionError as error:
            raise _invalid(f"invariant {invariant.name!r}: {error}") from error
        if invariant.type == "capacity" and not expressions.is_client_upper_bound(parsed):
            raise _invalid(
                f"capacity invariant {invariant.name!r} must be client.<counter> <= <expr> or <, "
                "using only before.* and integers on the right",
                "Capacity checks also run on incomplete trials, so they must be client-observed upper bounds.",
            )


def _reject_hostile_yaml(text: str) -> None:
    """Runs before any object is built. An alias can expand exponentially, and a deep document
    is slow to parse before it fails."""
    depth = 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.AliasEvent):
            raise _invalid(f"{CONFIG_NAME} can't use YAML aliases")
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > MAX_YAML_DEPTH:
                raise _invalid(f"{CONFIG_NAME} is nested more than {MAX_YAML_DEPTH} levels deep")
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1


def parse_config(text: str) -> ProjectConfig:
    try:
        _reject_hostile_yaml(text)
        config = ProjectConfig.model_validate(yaml.safe_load(text))
    except yaml.YAMLError as error:
        raise _invalid(f"{CONFIG_NAME} isn't valid YAML: {error}") from error
    except (ValueError, TypeError, RecursionError) as error:
        # Includes pydantic's ValidationError, and values YAML can't build such as 2020-13-45.
        raise _invalid(f"{CONFIG_NAME} is invalid: {error}") from error

    config.target.origin = safety.validate_origin(config.target.origin)
    safety.validate_path(config.target.health)
    safety.validate_path(config.target.state)
    if config.target.reset is not None:
        safety.validate_path(config.target.reset.path)
        _check_body("reset body", config.target.reset.json_body)
    for scenario in config.scenarios:
        _check_scenario(scenario)
    return config


def load_config(project_dir: Path) -> ProjectConfig:
    path = safety.contained(project_dir, project_dir / CONFIG_NAME)
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise _invalid(f"{CONFIG_NAME} is larger than {MAX_CONFIG_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise _invalid(f"{path} doesn't exist", f"Add {CONFIG_NAME} to the project.") from error
    except (OSError, UnicodeDecodeError) as error:
        raise _invalid(f"can't read {path}: {error}") from error
    return parse_config(text)


def find_scenario(config: ProjectConfig, scenario_id: str) -> ScenarioConfig:
    for scenario in config.scenarios:
        if scenario.id == scenario_id:
            return scenario
    known = ", ".join(scenario.id for scenario in config.scenarios)
    raise _invalid(f"unknown scenario {scenario_id!r}", f"Known scenarios: {known}.")
