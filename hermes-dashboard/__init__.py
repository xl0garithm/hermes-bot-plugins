"""hermes-dashboard: collections each bot designs and keeps for its person, shown as panels in the HermesBot app.

A bot designs collections (its own typed fields), adds and updates records in them, and builds the views on its dashboard
with four tools (`tools.py`); the person acts on records from the app through the dashboard routes
(`dashboard/plugin_api.py`). Both share one SQLite database under the Hermes root (`store.py`). The `dashboard` skill
(`skills/dashboard/`) teaches a bot to turn "keep track of X for me" into a collection and views, kept fresh by a routine.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("hermes-dashboard")

SKILL_MD = Path(__file__).resolve().parent / "skills" / "dashboard" / "SKILL.md"
SKILL_NAME = "dashboard"
SKILL_DESCRIPTION = "Track things for your person in collections you design, shown as panels on their HermesBot app dashboard, kept fresh by routines."

PROMPT_HINT = (
    "Your person has a dashboard for you in the HermesBot app (it opens when they tap your face). When they ask you to track, "
    "watch or collect something for them, keep it in a collection you design and show it as panels, with the dashboard_* "
    "tools (dashboard_collection, dashboard_record, dashboard_query, dashboard_view; find them with tool search if they are "
    "not listed). First load the skill with skill_view(\"hermes-dashboard:dashboard\") for how. A message starting "
    "[Dashboard button \"…\"] is your person pressing a button on a record: do that job, then update the record."
)


def register(ctx) -> None:
    from .tools import TOOLSET, DashboardTools

    tools = DashboardTools()

    for name, schema, handler in tools.registrations():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, description=schema["description"], emoji="📋")

    register_skill = getattr(ctx, "register_skill", None)

    if register_skill is not None:
        try:
            register_skill(SKILL_NAME, SKILL_MD, SKILL_DESCRIPTION)
        except Exception as error:  # a skill that can't load must not take the tools down with it
            log.warning("hermes-dashboard: the dashboard skill was not registered: %s", error)

    register_section = getattr(ctx, "register_system_prompt_section", None)

    if register_section is not None:
        try:
            register_section("hermes-dashboard.hint", PROMPT_HINT, max_chars=len(PROMPT_HINT) + 40)
        except Exception as error:
            log.warning("hermes-dashboard: the prompt hint was not added: %s", error)
