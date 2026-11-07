"""The fixed targets. They're built from the repo's demo directories at startup, and nothing about
a target ever comes from a request. Only the hostile definition sets allow_hostile."""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, get_args

from faultforge import config as config_module, fix_agent
from faultforge.sandbox import TICKET_API_STATE_VERIFIER, TargetSpec

TargetId = Literal["ticket-api", "hostile-build", "sportsdeck"]
TARGET_IDS: tuple[str, ...] = get_args(TargetId)
DEMO_ROOT = Path(__file__).resolve().parents[3] / "demo"


@dataclass(frozen=True)
class AgentInputs:
    """Prompt material. fix_agent refuses the reference and false fixes, whatever a caller passes."""

    app_source: bytes
    support_sources: MappingProxyType
    business_rules: bytes
    support_dir: Path
    config_path: Path
    regression_scenarios: tuple[str, ...] = ()


@dataclass(frozen=True)
class FixedTarget:
    id: TargetId
    title: str
    description: str
    scenario_id: str
    spec: TargetSpec
    invariant_names: frozenset[str]
    disclosures: frozenset[str]
    agent: AgentInputs | None = None
    # The state field that limits what can be accepted, and what it counts, for the page's headline.
    capacity_field: str = "stock"
    unit: str = "ticket"
    # True for a run recorded on a development machine and only ever shown as a replay.
    off_site: bool = False


def _trusted_config_text(config_path: Path, scenario_id: str) -> tuple[frozenset[str], frozenset[str]]:
    config = config_module.parse_config(config_path.read_text(encoding="utf-8"))
    scenario = config_module.find_scenario(config, scenario_id)
    return frozenset(item.name for item in scenario.invariants), frozenset(scenario.disclosures)


def sportsdeck_replay_target(demo_root: Path = DEMO_ROOT) -> FixedTarget:
    """For recording replays. It isn't in load_targets, so no request can ever run it on the server:
    its builds run AI-written code, which doesn't belong on a host whose app user controls Docker."""
    config = demo_root / "sportsdeck" / "faultforge.yaml"
    names, disclosures = _trusted_config_text(config, "refresh-token-race")
    return FixedTarget(
        id="sportsdeck", title="SportsDeck", description="A real Next.js app with a token race.",
        scenario_id="refresh-token-race", spec=TargetSpec(app_source=b"", config_path=config),
        invariant_names=names, disclosures=disclosures, capacity_field="live_tokens", unit="refresh token", off_site=True,
    )


def load_targets(demo_root: Path = DEMO_ROOT) -> MappingProxyType:
    ticket = demo_root / "ticket-api"
    support_dir = ticket / "ticket_support"
    ticket_config = ticket / "faultforge.yaml"
    ticket_names, ticket_disclosures = _trusted_config_text(ticket_config, "last-ticket-rush")
    hostile = demo_root / "hostile-build"
    hostile_config = hostile / "faultforge.yaml"
    hostile_names, hostile_disclosures = _trusted_config_text(hostile_config, "hostile-build")
    app_source = fix_agent.read_prompt_file(ticket / "app.py")
    return MappingProxyType({
        "ticket-api": FixedTarget(
            id="ticket-api",
            title="Ticket API",
            description="20 buyers race for the last ticket.",
            scenario_id="last-ticket-rush",
            spec=TargetSpec(
                app_source=app_source, support_dir=support_dir, config_path=ticket_config,
                state_verifier=TICKET_API_STATE_VERIFIER,
            ),
            invariant_names=ticket_names,
            disclosures=ticket_disclosures,
            agent=AgentInputs(
                app_source=app_source,
                support_sources=MappingProxyType(fix_agent.load_support_sources(support_dir)),
                business_rules=fix_agent.read_prompt_file(ticket / "BUSINESS_RULES.md"),
                support_dir=support_dir,
                config_path=ticket_config,
                regression_scenarios=fix_agent.regression_scenarios_for(ticket_config, "last-ticket-rush"),
            ),
        ),
        "hostile-build": FixedTarget(
            id="hostile-build",
            title="Hostile build",
            description="Real code that tries to break out of its sandbox.",
            scenario_id="hostile-build",
            spec=TargetSpec(
                app_source=(hostile / "app.py").read_bytes(), config_path=hostile_config, allow_hostile=True,
            ),
            invariant_names=hostile_names,
            disclosures=hostile_disclosures,
        ),
    })
