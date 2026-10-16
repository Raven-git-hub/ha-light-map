"""Deploy: reconcile helpers/automations/Area writes against live HA across namespaces;
create/update/remove; verify after write. Each namespace stays in its own lane.

Hard rules:

- Separate lanes: the switching deployer reconciles `lm_*` / `lm_soft_*` and never touches
  `al_*`, and vice versa. A plan for one namespace never creates, updates, or removes
  anything outside it.
- Every write is verified after the fact.
- Area/registry writes are a new write surface: verified, reconcilable, never destructive by
  surprise.
- Entity IDs only, never device IDs.
- During the parallel build (P0-P4) Light Map runs beside the live ha-adaptive-light, so
  only new artefacts are deployed and the new bridges stay unbound (no hard switches; bridge
  not yet authoritative) until cutover (P6).
"""

from collections.abc import Mapping
from typing import Any

from app.core.ha import HAClient


async def fetch_live(ha: HAClient, prefix: str) -> dict[str, Any]:
    """Return the artefacts currently in HA under one namespace prefix."""
    raise NotImplementedError


def plan(prefix: str, desired: Mapping[str, Any], live: Mapping[str, Any]) -> dict[str, Any]:
    """Diff desired against live artefacts in one namespace: what to create, update,
    remove."""
    raise NotImplementedError


async def apply(ha: HAClient, deploy_plan: Mapping[str, Any]) -> None:
    """Carry out a plan against HA."""
    raise NotImplementedError


async def verify(ha: HAClient, deploy_plan: Mapping[str, Any]) -> None:
    """Re-read HA and check that every write in the plan landed."""
    raise NotImplementedError


async def deploy(ha: HAClient, config: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    """Generate, plan, apply, and verify one namespace; return what was done."""
    raise NotImplementedError


async def deploy_areas(ha: HAClient, config: Mapping[str, Any]) -> dict[str, Any]:
    """Write the committed room snapshot to HA's Area/entity registry and verify it."""
    raise NotImplementedError
