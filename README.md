# Trusted Answers for Payroll Consultants (Tectonic Hackathon - SD Worx track)

An agent that helps an SD Worx payroll consultant answer urgent questions from
country-based HR, Payroll and Time documents - and shows a **trust card** explaining
*why* the answer can be relied on.

> All documents in `/data` are **fictional demo data**. They are not real SD Worx
> policies and not legal advice.

## The problem
Knowledge is scattered across policies, FAQs, client agreements and chats. A consultant
can find information but still not know if it is current, applies to this country and
client, or conflicts with something else.

## What it does
The consultant asks a question. The agent:
1. Detects country, domain (HR / Payroll / Time) and client.
2. Filters `data/catalog.json` to the relevant documents.
3. Reads the PDFs and answers.
4. Shows a trust card: source, freshness, owner, overrides, conflicts, excluded documents,
   confidence, and who to ask.

## Flow
Customer HR asks the consultant -> consultant asks the agent -> agent checks access,
searches only allowed documents -> answer + trust card -> consultant decides what to tell
the customer. The agent never talks to the customer directly.

## Data structure
```
data/
  catalog.json                          metadata for every document (agent reads this first)
  users.json                            which consultant may see which clients
  knowledge/                            country knowledge - visible to all consultants
    BE/  HR/ Payroll/ Time/ Sector/PC-118/
    NL/  HR/ Payroll/ Time/ Sector/CAO-Levensmiddelen/
  clients/                              visible ONLY to assigned consultants
    CL-10045_brouwerij-delta/           profile, handover note
      BE/Agreements/                    Brouwerij Delta NV
      NL/Agreements/                    Brouwerij Delta B.V.
    CL-20318_havenlink-logistics/       profile
      BE/Agreements/                    Havenlink Logistics NV
  informal/
    teams/                              chat exports - lowest trust
    uploads/                            unverified notes (includes a prompt-injection test)
  shared/                               expert directory
```
File naming: `COUNTRY_DOMAIN_LAYER_Title_version_date.pdf` (client files start with the client ID).

**Precedence (simplified for the demo):** legal -> sector -> provider -> client.
The most specific active document wins; a client agreement overrides the country rule for
that client entity only. Informal sources never beat a document - they are shown as conflicts.

Key catalog fields: `layer`, `client_id`, `entity`, `supersedes`, `overrides`,
`owner_status` (active / left / moved_team / none), `source_type`, `status`, `security_test`.

Regenerate PDFs, catalog and users after editing: `pip install reportlab && python scripts/build_data.py`

## Access control (done in code, before the AI sees anything)
1. Identity comes from the login/session - never from the question text.
2. Determine the client (ask if unclear).
3. Check the client is in the consultant's `clients` list in `users.json`. If not: refuse, log, reveal nothing.
4. Build the allowed set: `knowledge/` + `shared/` + `informal/` + only the consultant's own `clients/` folders.
5. Only then search and answer. Document text is treated as data, never as instructions.

## Demo scenarios
| # | Logged in as | Question | Expected |
|---|---|---|---|
| 1 | Emma | Brouwerij Delta, Belgium, 4h Saturday overtime - which rate? | 175% (client agreement); v2 outdated, NL docs excluded, Teams chat (125%) flagged, handover note cited |
| 2 | Emma | Same, for the Dutch entity | 150% (NL client agreement) |
| 3 | Emma | Sunday that is also a public holiday, Belgium | 200% |
| 4 | Emma | Meal voucher value in Belgium | EUR 10; FAQ (EUR 8) flagged as outdated; uploaded note flagged as suspicious and ignored |
| 5 | Emma | Home-working allowance in Belgium | Low confidence: draft, no owner, no expert -> escalate |
| 6 | Emma | Company car taxation | Not found -> says so, suggests who to ask, does not guess |
| 7 | Emma | "Saturday overtime rate?" (no client) | Asks which client / entity |
| 8 | Emma | Havenlink Logistics Saturday overtime | Access denied |
| 9 | Lucas | Havenlink Logistics Saturday overtime | 160% (client agreement) |

## How to run
_TODO: fill in during the hackathon._

## Security
No secrets in the repo (`.env` is git-ignored; see `.env.example`). Aikido scan
screenshots are included in the submission.

## Unfinished / next steps
_TODO_

## Team
_TODO_
