"""hermes-dashboard: panels each bot keeps for its person, shown in the HermesBot app.

A bot keeps its dashboard with four tools (`tools.py`): what it found, things to follow up, reminders, a few numbers, often
refreshed by a routine. The phone reads the panels and marks items done through the dashboard routes
(`dashboard/plugin_api.py`). They share one JSON file per bot under the Hermes root (`board.py`). The `dashboard` skill
(`skills/dashboard/`) teaches a bot how to design a panel from whatever data its person picks and keep it fresh.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("hermes-dashboard")

SKILL_MD = Path(__file__).resolve().parent / "skills" / "dashboard" / "SKILL.md"
SKILL_NAME = "dashboard"
SKILL_DESCRIPTION = "Keep panels on your person's phone dashboard (HermesBot app) and keep them fresh with a routine."

PROMPT_HINT = (
    "Your person has a dashboard for you in the HermesBot app (it opens when they tap your face). When they ask you to track, "
    "watch or collect something for them, keep it there as a panel with the dashboard_* tools (dashboard_read, "
    "dashboard_set_panel, dashboard_update_items, dashboard_remove_panel; find them with tool search if they are not listed), "
    "refreshed by a routine. First load the skill with skill_view(\"hermes-dashboard:dashboard\") for how."
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
