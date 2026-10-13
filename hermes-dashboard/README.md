# hermes-dashboard

Optional. A dashboard for each bot in the HermesBot app: tapping a pinned bot opens the panels it keeps for you (what it
found, things to follow up, reminders, a few numbers), with its chat one tap away. Items that need you put an amber dot on
the bot's face on the app's home screen and show in its Needs you list.

## What it adds

- **Four tools** for bots, in the `dashboard` toolset: `dashboard_read`, `dashboard_set_panel`, `dashboard_update_items`,
  `dashboard_remove_panel`. Each acts on the dashboard of the bot that calls it.
- **The `hermes-dashboard:dashboard` skill**: how to agree a panel with you, build it from whatever data you pick (mail,
  voicemail, a sheet, Paperless, the web, the server) and keep it fresh with a routine that delivers nowhere but the panel.
- **A short prompt line** so bots know the dashboard is there.
- **Routes for the app** under `/api/plugins/hermes-dashboard/`, behind the dashboard's sign-in:

| Route | What |
| --- | --- |
| `GET /status` | version, how many bots keep a dashboard |
| `GET /bots` | per bot: open items that need you, panels, last change |
| `GET /bots/{profile}` | that bot's panels, with `done_at` on what you marked done |
| `POST /bots/{profile}/panels/{panel}/items/{item}` | `{"done": true}` marks an item done (`false` undoes it) |

Panels are `list`, `checklist`, `metrics` or `summary`. A dashboard holds up to 12 panels of up to 50 items. Everything a
bot sends is checked and trimmed; an item's action may only open `tel:`, `sms:`, `mailto:` or `https://`.

## Install

Hermes loads user plugins per profile (each bot's own `plugins/` folder and `plugins.enabled`). Install it in the default
profile, which serves the app's routes, and in each bot that should keep a dashboard:

```sh
hermes plugins install xl0garithm/hermes-bot-plugins/hermes-dashboard --ref <commit sha> --enable
hermes -p concierge plugins install xl0garithm/hermes-bot-plugins/hermes-dashboard --ref <commit sha> --enable
```

Then restart the dashboard (and the messaging gateway, if one runs): plugins load when Hermes starts.

## Data

`~/.hermes/hermes-dashboard/<profile>.json` (the Hermes root, whichever bot writes it; 0600): the bot's panels, and apart
from them what you marked done, so a routine rewriting a panel never brings back what you handled. Done marks for items a
bot stops listing are forgotten after 30 days. Nothing leaves the server.

## Tests

    python -m unittest discover -s hermes-dashboard/tests
