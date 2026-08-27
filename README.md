# TheList

A list of macro-tasks, owned by one person and annotated by the people they work
with. One board per user; the owner decides what is on it, editors add notes,
reorder, mark things done and propose new items.

Live at [thelist.borant.eu](https://thelist.borant.eu).

**What it is not:** a task manager. No sub-tasks, no generated instances, no
scheduler, no burndown. A row is a thing worth remembering exists, not a unit of
work to be broken down.

## The five ideas

**One order, and it is manual.** The list is in the order the owner dragged it
into. Due dates are shown and coloured, never sorted on — an automatic
reordering would make the dragging mean nothing. There is a *by date* view, and
it is a reading of the same table, not a rewrite of it.

**Recurring is a flag, not a cadence.** Some things just need doing every so
often. Marking one done records *when it was last done* and leaves it exactly
where it is; marking a one-off done archives it. No child rows, no schedule, no
notion of "skipped".

**Proposals, not edits.** An editor proposes; the owner accepts or declines with
a reason. Declined proposals stay, with the reason — in a two-person
arrangement, "you proposed this in May and said no because…" is working memory.

**Every row says who it is for.** Anyone, with or without an account: the person
asking for the work is usually outside the system. That is what makes the
quarter countable.

**Notes are append-only.** They are the asynchronous channel between two people,
and a note that can be edited afterwards is a conversation nobody can rely on.
The author has fifteen minutes to fix a typo; after that, the correction is
another note.

## The report

`/app/{id}/report` cuts the period by person and by tag:

| column | what it counts |
|---|---|
| on the list | active tasks right now |
| opened | tasks created in the period |
| closed | one-off tasks archived in the period |
| touches | completions in the period, recurring included |
| weighted | the same touches times their size (S/M/L = 1/3/8) |

**These are counts, not hours**, and the page says so. *Touches* is the column
that matters: a recurring task done eleven times in a quarter produces eleven
events, where counting tasks would say one and say something false. The raw and
weighted columns sit side by side deliberately — while nobody touches the size
menu, the weighted column is just the raw one times three, and that is visible.

CSV export is on the same page.

## Running it

```bash
cp .env.example .env      # JWT_SECRET is required
python seed.py            # first account, local mode only
python dev-run.py         # http://localhost:8020
python smoke.py           # 41 checks, its own database
```

`AUTH_MODE=local` (the default) uses email and password. `AUTH_MODE=gateway`
puts it behind [Borant ID](https://id.borant.eu) — see `DEPLOY.md`. The app works
either way: the gate is a mode, never a dependency.

## The MCP surface

Read-only. `list_boards`, `list_tasks`, `get_task`, `upcoming`,
`list_proposals`, `workload`, `vocabularies`. Keys are created per user in
Settings and carry an identity, not a capability: a call reaches exactly what its
owner reaches, and what it cannot see answers "not found" rather than
"forbidden".

```
https://thelist.borant.eu/mcp            header: X-API-Key
https://thelist.borant.eu/mcp/k/{key}    for clients that cannot set headers
```

Writing is deliberately absent for now.

## Mail

A degradable dependency: with `SMTP_ENABLED=false` the app behaves identically
and invitation links are shown on screen. All messages are in English and all of
them live in `mailer.py`, which is what makes that a claim you can check by
reading one file.
