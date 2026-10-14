---
name: dashboard
description: Track things for your person in collections you design (your own fields, a status, records you add and update), shown as panels on their HermesBot app dashboard and kept fresh by routines. Use when they ask you to track, watch, collect or keep an eye on something, to show or change a panel, or to answer what is open or waiting.
---

# Your dashboard

Your person has a dashboard for you in the HermesBot app: tapping your face opens it, before your chat. It shows **panels**
(views) over **collections** you design and keep. Records that need them light an amber dot on your face and show in their
Needs you list. They can open a record, change its status or a field, mark it done and leave you a note; they never add
records or change panels themselves. You do that, and you reshape panels when they ask.

Tools: `dashboard_collection`, `dashboard_record`, `dashboard_query`, `dashboard_view`.

## Setting something up

When they ask you to keep track of something, work out (asking only what you can't tell):

1. **The source**: where it comes from (mailbox, voicemail, a sheet, Paperless, the web, the server) and which skill or tool
   reads it. Check you can read it now.
2. **What a record is** and its **fields**: what they will want to see and act on (guest, property, received, reply by, quote,
   phone). Pick the right types: `phone` gives them a Call button, `email` a Mail button, `link` is a document (a Google
   Doc, Sheet or Slides file, a Drive, server or Paperless file, a ticket page) shown as a file card that opens where it lives,
   `date`/`datetime` read as "Fri", `money`, `rating`, `checkbox`, `choice`.
3. **A status** with stages, in order, ending in done ones: New → Replied → Waiting on guest → Done. It is what moves
   records along and closes them.
4. **What needs them**: the rule that lights the amber dot (status is New and reply-by is today; a quote over $500).
5. **How often** to look: a schedule for the routine.

Then, in the same turn:

1. `dashboard_collection` `create` with the fields.
2. `dashboard_record` `upsert` the records as they are now, each with a **`source_key`** from the source (the email's message
   id, the voicemail's file name): the same thing written again updates its record instead of adding a second.
3. `dashboard_view` `set` one or more panels (below), with the `needs_you` rule.
4. `cronjob_manage` `create` a routine to keep it fresh: a self-contained prompt ("Check the guest inbox since the last run;
   upsert each message into guest-emails by message id: …; move records along when the guest replies; close ones that are
   resolved."), `skills: ["hermes-dashboard:dashboard"]`, and **`deliver: "local"`**: the collection is its output, so nothing
   goes into the chat. A routine can't ask questions, so give it everything it needs.
5. Tell them in a sentence or two what you track, what will need them, and how often you look.

## Choosing panels

- `list`: records as rows, newest or soonest first. `show: {title, subtitle, meta}` picks the fields.
- `checklist`: things they do; ticking one closes it.
- `board`: columns by a status or choice field, for work that moves through stages.
- `table`: a few columns side by side, when comparing matters (quotes, prices).
- `number`: one figure (count, sum, average), against the last period ("Inquiries this week: 7, +3").
- `chart`: counts or sums per day, week or month, for trends.
- `summary`: a few lines of Markdown when a status is easier said than shown.
- `stat`: figures you set yourself, for data that isn't tracked in a collection.

Three to six panels that matter beat many. Put what needs them first.

## Drafts and buttons: offering to act

When you could do the next step for them (reply to an email with the google-workspace skill, book the vendor, file the
document in Paperless), offer it instead of only telling them:

- A **`draft`** field holds what you wrote for them to send or use: a reply, a message, an order. The record shows it in full
  with an Edit link; set `to` to the field naming who it is for. Write the draft when you add or update the record. If
  they edited it, their version is the one to send (your later writes leave it alone, like any field they changed).
- A **button** (`actions` on the collection, up to 4) is the next step as a verb: `{"id": "send", "label": "Send reply",
  "prompt": "Reply to the guest in the same email thread with the draft reply, exactly as written.", "when": {"field":
  "status", "op": "eq", "value": "New"}, "draft": "reply"}`. The first button whose `when` matches is the record's main
  button. `update` with `actions` replaces the list.
- **Never do the step until they press the button.** Pressing it opens a side chat with you that starts with
  `[Dashboard button "<label>" · <record>]`, the prompt, and the record as it is now (the draft word for word). Do exactly
  that job; Hermes asks them to approve as it always does. Then **finish on the record**: `dashboard_record` `update` (move
  the status along) and a `note` saying what you did, or a note saying why you couldn't. Until you write the record it
  shows "<you> is on it" and needs nothing from them.

## Reviews and approvals: anything that needs their sign-off

Whenever you have something your person should look at or approve before it goes further (a report, a quote, a contract, a
draft post, an invoice, photos), give it a record in a review collection rather than only mentioning it in chat:

- A `link` field to the document itself (where it lives: the Google Doc, the Drive or server file, the Paperless document).
  The record shows it as a file card they open with one tap. A short summary in a longtext field says what changed or what to
  look at.
- A status for the review, such as For review → Changes requested → Approved → Done, and a `needs_you` rule on For review
  (with a review-by date when there is a deadline), so it lights the amber dot and shows in their Needs you list.
- Buttons for what happens next, in your words and your process: "Approve and publish", "Request changes" (your prompt says
  to read their latest note on the record and revise), "Sign and file", "Send to client". The first is their main button,
  the second sits beside it. If you don't know what approving should do, ask them once when you set it up.
- One review collection can hold every kind of document you produce for them; use a `choice` field for the kind if it helps.

## Keeping it right

- Write records with `upsert` by `source_key`; change one with `update`; `close` it when it is resolved.
- **Your person's changes win.** If they changed a field, your later writes leave it alone (the result says which fields were
  kept). Read their changes and notes with `dashboard_query` `include_history: true` and act on them: a note like "offer 1 PM
  if housekeeping runs late" is an instruction. Only pass `override_person` when they ask you to change it back.
- Never reopen something they closed unless it truly needs them again; if so, say why in a note.
- A record they **muted** (`muted_until` ahead of now in `dashboard_query`) is theirs to come back to: keep it up to date,
  but don't raise it with them until the mute lifts.
- Never put passwords, tokens, keys or card numbers in a record. It is shown on a phone.
- Reshape on request: "split it by property" → a board grouped by property or a filter per panel; "add a weekly chart" → a
  `chart` view; "track the callback date too" → `dashboard_collection` `update` `add_fields`, then update the records.
- Share with another bot when the work is shared: `dashboard_collection` `share` (read, or write). That bot builds its own
  panels over it.
- Answer "what's waiting on me?" from `dashboard_query`, not from memory.

## Example

```json
{"action": "create", "id": "guest-emails", "title": "Guest emails", "fields": [
  {"key": "guest", "label": "Guest", "type": "text", "required": true},
  {"key": "status", "label": "Status", "type": "status", "stages": [{"name": "New"}, {"name": "Replied"}, {"name": "Waiting on guest"}, {"name": "Done", "done": true}]},
  {"key": "channel", "label": "Channel", "type": "choice", "options": ["Airbnb", "Vrbo", "Direct"]},
  {"key": "summary", "label": "Summary", "type": "longtext"},
  {"key": "reply_by", "label": "Reply by", "type": "date"},
  {"key": "thread", "label": "Thread", "type": "url"}
]}
```

```json
{"action": "set", "id": "inbox", "title": "Guest emails", "kind": "list", "collection": "guest-emails", "config": {
  "show": {"title": "guest", "subtitle": "channel", "meta": "reply_by"},
  "sort": [{"field": "reply_by", "dir": "asc"}],
  "needs_you": {"all": [{"field": "status", "op": "eq", "value": "New"}, {"field": "reply_by", "op": "within_days", "value": 1}]}
}}
```
