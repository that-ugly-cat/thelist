# TheList

A list of macro-tasks, owned by one person and annotated by the people they work
with. One board per user; the owner decides what is on it, editors add notes,
reorder, mark things done and propose new items.

**Live at [thelist.borant.eu](https://thelist.borant.eu)**, behind Borant ID.

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
python smoke.py           # 79 checks, its own database
```

`AUTH_MODE=local` (the default) uses email and password. `AUTH_MODE=gateway`
puts it behind [Borant ID](https://id.borant.eu) — see `DEPLOY.md`. The app works
either way: the gate is a mode, never a dependency.

## The MCP surface

Seventeen tools, reading and writing, so the list can be kept from inside the
conversation where the work is actually being discussed.

**Reading** — `list_boards`, `list_tasks`, `get_task`, `upcoming`,
`list_proposals`, `workload`, `vocabularies`.

**Writing** — `add_task`, `update_task`, `complete_task`, `reopen_task`,
`add_note`, `accept_proposal`, `decline_proposal`, `move_task`, `delete_task`,
`restore_task`.

Keys are created per user in Settings and carry an **identity, not a
capability**: a call reaches exactly what its owner reaches. A board the caller
is not a member of answers "not found" rather than "forbidden", or the surface
becomes a way to probe for what it hides — but a board they *can* see with an
owner-only action says so plainly, because nothing is concealed by that answer.

The write tools call the same functions the web routes call rather than
reimplementing the rules beside them, so there is **no path through here that the
interface would not allow**: an editor proposes where the owner creates,
declining needs a reason, deletion is soft, completing a recurring task keeps it
on the list. `add_task` also refuses a title already on the board unless you pass
`allow_duplicate` — a retried call should not quietly leave two identical rows.

```
https://thelist.borant.eu/mcp            header: X-API-Key
https://thelist.borant.eu/mcp/k/{key}    for clients that cannot set headers
```

Renaming and archiving people, invitations, mail preferences and keys stay out of
the surface on purpose: renaming a person rewrites how the whole past reads, and
that is a thing to do while looking at the page.

## Administration

One level, and it is **a different axis from the boards rather than a level above
them**. An admin configures the mail relay everybody sends through and can
deactivate an account; an admin reaches **nobody's list**, because reaching a
board is a membership row and `/admin` issues none. Without that line,
"administrator" would quietly mean "reads everyone's coordination with their
colleagues".

The relay is set once, from `/admin`, and the password is **write-only**: it is
Fernet-encrypted at rest and never sent back to the page. Everyone's mail goes
through it, nobody can read it. There is a *send a test* button, because
otherwise "does the relay work" gets answered by waiting for somebody else's
invitation to silently not arrive — and the result is the mailer's own code, so
`auth_refused 535` is a string you can search for.

**Who is admin**: the first account to exist, because somebody has to be able to
configure the relay and behind the gate the first arrival is whoever set the
thing up. After that it is given deliberately. The last administrator cannot be
demoted or deactivated from the interface — and `admin.py --promote` is the way
back in when the account is gone for some other reason.

## Mail

A degradable dependency: with the relay off the app behaves identically and
invitation links are shown on screen. All messages are in English and all of them
live in `mailer.py`, which is what makes that a claim you can check by reading
one file.

Configuration lives in the database, not the environment, and there is **no
fallback between the two**: the day mail stops working, "which of the two is
winning" has to be answerable from the screen.
