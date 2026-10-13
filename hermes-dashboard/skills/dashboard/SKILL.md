---
name: dashboard
description: Keep panels on your person's phone dashboard (HermesBot app) - what you found, things to follow up, reminders, a few numbers - and keep them fresh with a routine. Use when they ask you to track, watch, collect or keep an eye on something for them, or to set up or change a panel.
---

# Your dashboard

Your person sees a dashboard for you in the HermesBot app: tapping your face opens it, before your chat. It holds
**panels** you keep: the voicemails you found, emails to follow up, their reminders, a few numbers. Items that need them
put an amber dot on your face on their home screen and show in their Needs you list. It is the calm, current state of
things; the chat is for conversation.

Tools: `dashboard_read`, `dashboard_set_panel`, `dashboard_update_items`, `dashboard_remove_panel`.

## Setting up a panel

When your person asks you to keep track of something, agree the panel with them before building it (ask, briefly, only what
you can't work out):

1. **The data**: where it comes from (their mailbox, a voicemail inbox, a sheet, Paperless, a web page, the server) and
   which skill or tool reads it. Check you can actually read it now.
2. **What needs them**: what makes an item theirs to act on (a guest asking for a call back, a payout on hold), as opposed
   to things you can just note or handle yourself.
3. **How often**: when to refresh (every morning at 8, every 2 hours on weekdays).

Then, in the same turn:

1. Build the panel now with real data (`dashboard_set_panel`), so they see it at once.
2. Create a routine that refreshes it (`cronjob_manage` with `action: "create"`): a self-contained prompt ("Check the
   voicemail inbox for anything since the last run and rewrite the `voicemails` panel with `dashboard_set_panel`. Mark
   `needs_you` when a caller asks for a call back or a decision."), `skills: ["hermes-dashboard:dashboard"]`, and
   **`deliver: "local"`**: the panel is the routine's output, so nothing goes into the chat. A routine cannot ask
   questions, so put everything it needs in the prompt.
3. Tell them in one or two sentences what the panel holds and when it refreshes.

## Choosing a kind

- `list`: things that arrived or happened (voicemails, emails, alerts, new reviews). Newest first. Items the person marks
  done disappear.
- `checklist`: things the person has to do (reminders, todos, renewals), with `due` dates. They tick them off.
- `metrics`: up to 6 numbers that change (reviews this week, % booked, disk used), each with a short `change`
  ("+3", "-2%").
- `summary`: a short status in Markdown when a list doesn't fit ("All three sites up. The SSL certificate renews in 6 days.").

A dashboard holds at most 12 panels, each at most 50 items. Fewer is better: 3 to 5 panels that matter.

## Writing items

- `title`: a few plain words a person scans ("Guest in Cabin 3"), never a raw subject line or an id.
- `detail`: one line on what it is or why it matters ("AC isn't cooling. Asks for a call back today.").
- `body` (Markdown): your write-up for when they open it: what happened, what they need to decide, your suggestion, the
  facts that matter. Summarise; never paste a whole email, transcript or log.
- `needs_you: true` only when they must act: call, decide, reply, sign. Not for things you can handle, or that are FYI.
- `at`: when it happened (ISO 8601 with offset, or epoch seconds). `due` (`YYYY-MM-DD`) for checklist items.
- `action`: the one next step, `{label, url}`: `{"label": "Call back", "url": "tel:+15555550143"}`, `mailto:`, `sms:`,
  or an `https://` page (the Gmail thread). Only those open.
- `link`: where it lives: a file on this server (`/home/...`), a plugin document route (`/api/plugins/hermes-drive/files/
  <id>/preview`, a Paperless one), or an `https://` page.
- `id`: stable, from the source (the message id, the voicemail's file name), lower-case letters, digits, `-`, `_`. The same
  thing written again keeps its id, so it is never duplicated and the person's done mark sticks.
- `note` on the panel: a few words beside its title ("3 today", "2 emails").

Never put passwords, tokens, keys or card numbers on a panel. It is shown on a phone.

## Keeping it fresh

- A routine usually rewrites the whole panel with `dashboard_set_panel` (same `id`), from the source as it is now.
- To add or change a few items without rewriting, use `dashboard_update_items` (`upsert`, `remove` by id).
- `dashboard_read` shows what is there, with `done_at` on what the person marked done. Respect it: never re-add a done item
  under a new id to bring it back. If it truly needs them again, say so in the chat.
- Drop items that no longer matter, so the panel stays short.
- Remove a panel they no longer want with `dashboard_remove_panel`, and pause or remove its routine.

## Example

```json
{
  "id": "voicemails",
  "title": "Voicemails",
  "kind": "list",
  "note": "3 today",
  "items": [
    {
      "id": "vm-20261013-0914",
      "title": "Guest in Cabin 3",
      "detail": "AC isn't cooling. Asks for a call back today.",
      "body": "The guest says the AC stopped cooling overnight and the unit is at 81°F. They are out until 4 PM and would like a call before then.",
      "needs_you": true,
      "at": "2026-10-13T09:14:00-04:00",
      "action": { "label": "Call back", "url": "tel:+15555550143" }
    },
    { "id": "vm-20261012-1702", "title": "Plow contractor", "detail": "Confirms Thursday, 6 AM. Nothing to do.", "at": "2026-10-12T17:02:00-04:00" }
  ]
}
```
