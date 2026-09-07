# Loom script

Target 4 minutes. Times are cumulative. Anything in _italics_ is a stage direction, not a line
to read.

Two corrections to the outline this came from: the CRM is **HubSpot**, not ClickUp. And the
agent **requests** credits, never grants them — worth saying precisely, because it is the whole
architecture in one word.

---

## 0:00 — What and why (35s)

> I built a voice agent that answers billing calls for a fictional Swiss materials company.
> Invoices, payments, credits, disputes.
>
> The reason it is this and not something else: while I was thinking about what to build, my
> old estate agency emailed me asking for my new bank details so they could refund what I had
> overpaid on heating. An email. Asking me to send bank details.
>
> That is the call this agent takes. Somebody rings about money, and the entire problem is
> proving who they are before you tell them anything.

_Beat. This is the frame for everything after it — do not rush it._

---

## 0:35 — The golden path (90s)

_Play `conv_0501m1vrah1ser79ta2gb8b6xx0v`. Have the tool calls visible._

> Thomas Weber rings about an overdue invoice he says he already paid.
>
> First: identity. Three details, one at a time — email, phone, date of birth. Each one checked
> as it arrives, by a Lambda, against the record in DynamoDB.

_Point at the four `check_factor` calls._

> Four calls, not three. Look at what the transcript heard: `thomas.veber@alpinatech.ch`. The
> W came through as a V and the hyphen vanished. That is a normal Swiss surname on a normal
> phone line.
>
> Checking each detail separately is what lets him correct it while he is still on that
> question, instead of the whole call failing at the end for a reason nobody can name.

_Point at `verify_identity`._

> Then the decision. `check_factor` decides nothing — it never moves anyone past the gate.
> `verify_identity` writes VERIFIED onto the conversation record.
>
> The agent can still call any tool it likes. Five of them refuse: account context, payment
> matching, allocation, credits, escalation. Each one opens by reading that record, and returns
> NOT AUTHORIZED unless it says VERIFIED.
>
> So the model is never asked whether it verified someone. It cannot be — saying so is not a
> write. And those tools hold no permission on the identity table at all; the customer id comes
> from the gate, so a payment tool cannot read a contact even if it tried.

_Point at `get_account_context`, then `match_payment`._

> Now it can see the account: invoices and payments from DynamoDB, open and closed tickets from
> HubSpot.
>
> He gives the amount and the date. `match_payment` compares them against the payment on
> record — the agent never reads the stored figures out first, because then the question is
> worthless.

_Point at `propose_allocation` → `UNDER_REVIEW`, and at the ticket id it returns._

> Match. And here is the important part: it raises a ticket. It does not allocate the payment.

---

## 2:05 — The decision that shapes everything (45s)

> The agent has read access to the financial ledger and no write access at all.
>
> Not "we told it not to". There is an explicit IAM Deny on every agent-facing role, covering
> PutItem, UpdateItem, the PartiQL variants — a Deny beats any Allow. And the code path to
> write simply does not exist in that Lambda.
>
> Money moves one way: a person opens the ticket, accepts it, and a scheduled applier re-reads
> the ledger, re-runs the rules against the account as it stands now, and writes only if they
> still pass. Someone accepting a ticket says they are content for it to happen — not that the
> numbers still add up.
>
> Same idea on credits. Under a hundred francs the agent can request one. Above five hundred in
> a rolling year it is refused — and the caller is never told the ceiling, because naming it
> tells them exactly how to sit under it.

---

## 2:50 — The second caller (40s)

_Play `conv_3001m1vrgmhaea6vgp6fgj3tkqca`._

> Klaus Mueller, same company, same invoice, ringing separately.
>
> He verifies with his own details — being a colleague of somebody verified is worth nothing at
> the gate.

_Point at `propose_allocation` → `ALREADY_UNDER_REVIEW`, **the same ticket id**._

> Same ticket id as the first call. The agent finds the review his colleague already raised and tells
> him it is in hand, rather than opening a second one.
>
> Two tickets for one problem means two people working it and two different answers reaching
> the same company.
>
> And when I close that ticket, the applier allocates the payment against the invoice, and the
> invoice stops reading overdue.

---

## 3:30 — How it is built (35s)

_Show the repo, then the Actions tab._

> Spec-driven, with Claude Code, in git.
>
> On a pull request: format, lint, a scanner that fails the build if a credential or an account
> id is in a tracked file, the unit and contract suites, and Terraform format and validate.
>
> On merge, the pipeline deploys the infrastructure **and** pushes the prompt, the tool schemas
> and the knowledge base to ElevenLabs. The prompt is the security boundary the model actually
> reads, so it belongs in review, not in a dashboard where a change leaves no trace.
>
> ElevenLabs holds the phone number, the prompt, the tools and the knowledge base. Behind every
> tool is a Lambda with its own IAM role. Qwen for the model — the latency is good, around half
> a second to first token.

---

## 4:05 — Honest assessment (30s)

> What works: I have not managed to get a single financial fact out of it without verifying
> first, and I tried.
>
> What does not: French and Italian carry an accent and some artefacts. That one is instructive
> — ElevenLabs picks the voice model from the conversation's language, and a browser session
> has no phone number to infer a language from, so my web tests were an English voice reading
> Italian. On a real call from an Italian number it switches by itself.
>
> Next: ElevenLabs' evaluation and data-collection features, and a proper price-performance
> comparison across models. Every call already records its own cost, tokens, latency and
> outcome, so that comparison is a query rather than a project.

---

## Closing line

> The thing I would want you to take from this: the prompt is where behaviour is shaped, and
> the IAM policy is where it is guaranteed. Anything that matters is on the second list.

---

## Numbers, if asked

| | |
|---|---|
| Both demo calls | ~2 minutes, ~$0.27 each |
| LLM time to first token | p50 0.5s |
| Caller-perceived silence | p50 1.9s, p95 5.3s |
| Slowest tool | `propose_allocation`, p50 3.9s |
| Credit ceilings | 100 CHF per request, 500 CHF rolling 12 months |
| Languages | EN, DE, FR, IT — prompts held in agreement by tests |

## Things not to claim

- Do not say the agent grants credits. It requests them.
- Do not say the payment is settled on the call. It is proposed; a person accepts it.
- Do not demo multilingual through the browser widget. It has no calling number, so the
  conversation stays English and the voice will sound wrong.
