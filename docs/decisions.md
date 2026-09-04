# Decisions

Every consequential choice in this project, what it cost, and why it was made anyway.

Not a summary of what was built — [implementation-log.md](./implementation-log.md) covers
that. This is the reasoning: the decisions that could defensibly have gone the other way, the
ones where the obvious answer was wrong, and the ones that were made, tried, and reversed.

Format throughout: **the decision**, then **what it costs**, then **why**.

---

## 1. The trust boundary

### 1.1 The model may recommend; only the backend may decide !IMPORTANT

**Decision.** Every financial rule — verification, matching, allocation authority, credit
limits, abuse detection — runs as a pure function in `src/domain/`, called from a Lambda. The
conversational layer receives enumerated statuses and never a decision it could reinterpret.

**Cost.** Latency: each decision is a network round trip inside a five-second budget, where
letting the model reason from data in its context would be instant. And more code — a rule
expressed in a prompt is three sentences, while the same rule in Python is a module, a test
file and an IAM policy.

**Why.** The rest of the system is only as trustworthy as this. A language model can be
argued with; a conditional expression cannot. It also makes the claim *checkable*: "the model
cannot issue a credit" is verifiable by reading one IAM policy, whereas "the prompt tells it
not to" is verifiable only by hoping.

### 1.2 Three layers, and the domain layer cannot reach the network

**Decision.** `domain/` holds pure functions with no imports that touch I/O. `adapters/` is
the only code that talks to anything external. `handlers/` are thin: parse, authorise, call
domain, log.

**Cost.** Indirection. Reading one request path means opening three files, and some data is
passed through a layer that does nothing but pass it.

**Why.** Two properties fall out of it that are otherwise expensive. The financial rules can
be tested exhaustively without AWS — 60-odd matching and credit cases run in milliseconds.
And every dependency can be made to fail in a test by substituting its adapter, which is why
no fault-injection flag ships in production code.

### 1.3 Verification state lives in DynamoDB, not in the conversation !IMPORTANT

**Decision.** `require_verified()` reads a row from the conversations table. The model's
belief about whether the caller is verified is never consulted.

**Cost.** A read on every financial tool call, and a table row per conversation.

**Why.** This is the disclosure gate. If it lived in the prompt, a caller who talked past the
model would be verified — and talking past models is a solved problem for anyone motivated.

**This decision produced the project's most instructive bug.** The write that records
verification was conditional on the conversation row already existing, and nothing created
it. Verification succeeded, the write was discarded, and every subsequent tool refused the
caller. The failure was invisible because *a discarded verification looks exactly like the
gate working* — refusing callers is what a correct gate does. See §9.2.

---

## 2. Identity and verification

### 2.1 Three factors, uniformly, with no high-risk tier

**Decision (Nicolas).** Every disclosure and every action requires three confirmed factors.
No reduced tier for routine questions, no elevated tier for risky ones.

**Cost.** A caller asking a trivial question does the same work as one requesting a credit.
And risk-based verification — the industry norm — is given up.

**Why.** Tiering adds a decision about which tier applies, and that decision is exactly the
kind a model gets wrong under pressure. One bar is auditable and cannot be talked down. Risk
is handled *after* verification, by the abuse rules and by escalation, where it is enforced
in code.

### 2.2 At least one factor must not be printed on an invoice !IMPORTANT

**Decision.** Email, phone, date of birth and account opening year are non-document factors.
Customer ID is not: it is printed on every invoice.

**Cost.** A caller holding the invoice and knowing only what is on it cannot verify, even
though they may be entirely legitimate.

**Why.** Without it, possession of a document is possession of the account — and the calls
this system handles are, by definition, from people holding an invoice.

### 2.3 A name is not a verification factor at all

**Decision (Nicolas, after first proposing it count as one).** No name field exists in the
tool contract. First name and surname are not accepted separately, and a full name is not
accepted either.

**Cost.** A factor that every caller can supply instantly is discarded.

**Why.** That is precisely the problem. Callers volunteer their name in the first sentence of
an ordinary call, so crediting it hands over a third of the bar for free. And first name and
surname are not independent facts — anyone who knows one almost always knows the other — so
counting them separately would hand over two thirds for one piece of common knowledge.

### 2.4 A wrong answer fails the attempt; not knowing does not

**Decision.** Supplying a value that does not match fails the attempt and advances the
lockout counter. Supplying fewer factors than required does not.

**Cost.** A caller who guesses once and stops burns an attempt they might feel they did not
deserve to lose.

**Why.** The alternative punishes honesty: a customer who genuinely cannot recall their
account opening year would be counted toward a lockout for saying so. Three correct answers
plus one wrong one also fails, because otherwise a caller can guess freely as long as they
pad the attempt with facts they do know.

### 2.5 A failed attempt reports no progress at all

**Decision.** On `FAILED`, the response reports zero confirmed factors regardless of how many
were right.

**Cost.** A legitimate caller who mistypes one field loses the visible progress of the ones
they got right, and the agent must ask again.

**Why.** Found by calling the deployed endpoint, not by the unit tests. A real customer ID
plus deliberate nonsense returned `factors_confirmed: 1`; an invented ID returned `0`. That
difference enumerates customer IDs across a five-digit space. The unit test compared
unknown-versus-wrong using *identical* inputs, so it was structurally blind to it.

### 2.6 An unknown customer is indistinguishable from wrong answers

**Decision.** A customer that does not exist is carried through the rule with an empty record
rather than returned early.

**Cost.** A pointless comparison against nothing, and a slightly odd-looking code path.

**Why.** A gate that answers "no such customer" differently from "wrong details" is a way to
enumerate who banks with you.

### 2.7 Attempts are counted per session as well as per customer

**Decision.** The conversation record carries its own failure counter.

**Cost.** A second counter to keep, and a second place a lockout can originate.

**Why.** Found by writing the handler tests. A caller who supplies wrong answers *without
ever naming an account* resolves to no customer, so no per-customer counter moves — and they
can guess indefinitely. The requirement said "per customer and per caller session"; only the
first half had been built.

### 2.8 Working for the customer is not authority over its account

**Decision (Nicolas).** Only the contact recorded against the account can verify. Other
employees fail like anyone else, receive nothing — not the balance, not whether an invoice
exists, not whether the company has an account — and are told an authorised contact can add
them.

**Cost.** A genuine colleague covering a holiday is turned away, and the fix takes a day
rather than a phone call.

**Why.** B2B accounts are not personal accounts. "I work there" is unverifiable over the
phone and trivially claimed.

### 2.9 The agent may name an authorised contact, but nothing else about them

**Decision (Nicolas, overruling an initial refusal to name anyone).** A name may be given. An
email address, phone number, role or location may not.

**Cost.** A caller learns who is on the account — information they could use to target an
impersonation.

**Why.** Nicolas's reasoning, and it holds: someone calling about their own employer already
knows who works in their accounts department. The name adds nearly nothing to what they have;
the contact details are what would let an impersonation actually proceed. Refusing to name
anyone makes the remedy useless without meaningfully improving security.

### 2.10 Guessing is detected server-side, per factor

**Decision (Nicolas).** A caller may correct themselves once. A third distinct value for the
same field is enumeration, raises a risk signal and escalates. Counted per factor, so
correcting an email does not consume the customer-ID allowance. Attempted values are stored
as salted hashes.

**Cost.** A genuinely confused caller — reading from the wrong document, then the right one —
can trip it.

**Why.** The agent can *notice* someone fishing, but if noticing is all that stops them, the
model is the control. Hashing lets distinct attempts be counted without retaining what was
guessed.

---

## 3. Financial correctness

### 3.1 The caller proves knowledge; the record explains failure

**Decision.** The caller supplies exactly two values — amount and transfer date. The
reference is read from the record and never asked for.

**Cost.** Corroborating evidence a caller might happily supply is left on the table.

**Why.** Two different jobs were being conflated. Amount and date are *anti-fraud*: they stop
someone holding a stolen invoice from claiming a payment. The reference is *diagnostic*: it
explains why a correct payment never allocated. Payers rarely know what their accounts
department typed, so asking would fail honest callers on a question that answers nothing.

### 3.2 Exact amount, three-day backward date tolerance

**Decision (Nicolas).** The amount must match exactly. The claimed date may precede the
recorded date by up to three calendar days, never follow it.

**Cost.** The guessable date space widens from one day to four.

**Why.** The payer reads the date their transfer *left*; the record holds the date it
*arrived*. A Friday transfer posting on Monday would, under exact matching, tell an honest
caller their payment does not exist — the system punishing a correct answer for a banking
artefact. Backward only, because a payment cannot post before it was sent, so a later date is
a real mismatch. The amount stays exact, and neither value appears on the invoice, so the
stolen-invoice property survives.

### 3.3 Two invoice identifiers instead of one

**Decision (Nicolas).** `invoice_number` (`INV-2026-0412`) is sequential, spoken aloud, never
fuzzy-matched. `payment_reference` (a random seven-digit key) is quoted on transfers and is
the only field matched fuzzily.

**Cost.** Two identifiers to generate, store, and explain. Customers see one number and quote
another.

**Why.** The single most valuable change in the project. Consecutive invoice numbers differ
by **one character**. Fuzzy-matching them would classify a payment correctly earmarked for
`INV-2026-0413` as a typo of `INV-2026-0412` and propose allocating it to the wrong invoice —
with amount and date appearing to agree. Silent, plausible, wrong. A random key over ten
million values almost never has a real neighbour, so a near miss that resolves to nothing is
safely a typo.

### 3.4 A near miss is a typo only if it resolves to nothing

**Decision (Nicolas).** `PROBABLE_TYPO` requires both that the reference is within two edits
of this invoice's key **and** that it matches no existing invoice. A reference resolving to a
different invoice blocks the match.

**Cost.** An index lookup per classification, and a second GSI on `invoice_number`.

**Why.** Without the second condition the first is dangerous. With sequential numbering, most
mistyped references land on a *real* invoice — so `OTHER_INVOICE` fires more often than
`PROBABLE_TYPO`, which is the safe direction: refusing to guess rather than guessing wrong.

### 3.5 Ambiguity is never resolved by choosing

**Decision.** Two payments fitting the same details return `INSUFFICIENT`. Two invoices a
caller might mean are established with them, not inferred.

**Cost.** More conversational turns, and occasionally an escalation a smarter system would
have resolved.

**Why.** A payment allocated to the wrong invoice looks exactly like one allocated correctly.
Nobody notices until a human untangles it months later.

### 3.6 Payer name and address never affect the outcome

**Decision.** Both are informational flags on the ticket. Neither blocks a match. The rule
receives booleans, never values.

**Cost.** A payment from an entirely unrelated party still matches on amount and date.

**Why.** In Swiss B2B, payment by a parent company, a group entity or a director personally is
routine. Refusing those would strand real money on a technicality. The human reviewing the
allocation sees the flag and decides — which they were going to do anyway, since the agent
cannot allocate.

### 3.7 The agent proposes; it never allocates

**Decision.** `allocation_authority_max` ships at zero, so every allocation goes to a person.
The auto-allocate branch exists and is tested but is unreachable.

**Cost.** No allocation completes on the call. Every one costs human time.

**Why.** Allocating money on a model's recommendation is the action with the worst
failure mode in the system. Keeping the branch — rather than deleting it — means raising the
threshold is a deliberate act with a tested consequence, not an undefined one.

### 3.8 Credit ceilings are inclusive, and cumulative over twelve months

**Decision (Nicolas).** At most CHF 100 per request and CHF 500 across twelve months, both
inclusive. CHF 400 of history plus a CHF 100 request passes at exactly 500; CHF 401 plus 100
does not.

**Cost.** A twelve-month window is long. A customer genuinely unlucky twice in a year waits.

**Why.** A per-request threshold with no cumulative view is defeated by asking twice. Twelve
months makes threshold-splitting impractical rather than merely slower. The unlucky customer
is escalated to a human, not refused.

### 3.9 A credit cannot exceed the charge it is applied to

**Decision (Nicolas).** Checked independently of the policy ceilings, before them, so the
most specific denial reason is reported.

**Cost.** A third check on a path that already has two.

**Why.** The ceilings govern how much goodwill accumulates. This governs not crediting more
than was charged — a data-integrity rule that must survive any future change to the CHF 100
and CHF 500 values.

### 3.10 An overpayment is offset by default; a refund is a request

**Decision (Nicolas).** A surplus comes off the next invoice automatically, and the agent
says so. A caller who wants the money back is making a refund request, evaluated under the
same limits as anything else outbound.

**Cost.** A customer wanting cash back rather than credit may need a person.

**Why.** The default answers the common case without a decision. Treating a refund as an
outbound amount rather than a special case means one set of limits governs everything that
leaves the company.

### 3.11 Balances are derived, never stored

**Decision.** Every balance is computed from ledger entries at read time.

**Cost.** A query and a sum on every account context call.

**Why.** A stored balance is a second source of truth that can disagree with the entries that
produced it, and reconciling the two is a project of its own.

---

## 4. Data and storage

### 4.1 Two KMS keys, not one !IMPORTANT

**Decision.** An identity key and a data key. The identity key's policy admits only the two
handlers that legitimately read identity data, matched on role-name prefix.

**Cost.** Two keys at roughly a dollar a month each, and a second policy to maintain.

**Why.** A shared key needs a policy admitting every Lambda in the project, and the isolation
becomes nominal. This is the difference between claiming identity data is isolated and being
able to demonstrate it from an IAM policy.

### 4.2 Verification records display fields onto the conversation

**Decision.** When verification succeeds it writes company name, language, account status and
the CRM ids onto the conversation record. Downstream tools read those instead of the identity
table.

**Cost.** Duplicated data, and a staleness window inside a single call.

**Why.** Forced by deployment. `get_account_context` needed a company name, and reading the
identity table for it would have put a third handler on that table. The choice was between
weakening §4.1 and copying four non-sensitive fields. Copying is cheaper and keeps the
isolation exact.

### 4.3 Audit events go to a log group, not a table

**Decision.** A dedicated CloudWatch group with ten-year retention.

**Cost.** No key-based reads. Reviewing means Logs Insights, not a query.

**Why.** Audit events are append-only and never read during a call. A table would add IAM
surface and Terraform for a read pattern that does not exist. If audit ever needs reading
mid-call, it moves.

**This nearly went wrong.** The first implementation logged to stdout, and Lambda routes
stdout to the *function's* log group — ninety-day retention. The event looked entirely
correct and would have vanished in three months. See §9.3.

### 4.4 Risk signals live on the conversation record

**Decision.** No separate table.

**Cost.** Aggregating a customer's signals means querying their conversations.

**Why.** Every signal is raised during a conversation and read only as an aggregate. A fifth
table for a one-to-one relationship is infrastructure without a purpose.

### 4.5 Retention differs by record class

**Decision (Nicolas).** Transcripts 90 days. Metadata, metrics and audit events ten years.
Customer summary until twelve months after account closure.

**Cost.** Four lifecycle rules rather than one, and metrics kept far longer than analytics
needs.

**Why.** Transcripts are the most sensitive artefact and the least useful after a few weeks.
Ten years matches Swiss business-record keeping, and Nicolas extended it to metadata so a
financial action taken on a call can still be explained a decade later — not just that it
happened, but with what context.

### 4.6 Seeding deletes as well as writes

**Decision.** Rows the fixtures no longer describe are removed before writing.

**Cost.** A scan per table on every seed.

**Why.** Overwriting alone left ten orphans from a previous fixture set — phantom invoices
that would quietly change a balance and produce a rehearsal that fails for reasons unrelated
to the code.

### 4.7 Fixture dates are relative to seeding

**Decision.** Invoices are issued "75 days ago", not on a fixed date.

**Cost.** No two seedings produce identical data, and documentation about the data must be
generated rather than written.

**Why.** The overdue invoice must always be overdue and the disputed payment must always sit
inside the matching tolerance. Fixed dates go stale, and a rehearsal that fails on stale data
wastes the time it was meant to save.

---

## 5. Failure and degradation

### 5.1 Tool failures return HTTP 200

**Decision.** Every tool returns 200 with a structured status, including failures.

**Cost.** Looks wrong to anyone reading the API in isolation, and defeats platform-level retry
logic.

**Why.** A tool failure is a conversational outcome, not a transport error. A 5xx invites the
platform to retry a mutation or surface a generic error the agent cannot speak. A structured
status the model can read and paraphrase is the point.

### 5.2 A failure is never an answer

**Decision.** `SERVICE_UNAVAILABLE` is a distinct status from `NO_MATCH`. An empty list means
"nothing exists"; a failure raises rather than returning empty.

**Cost.** Every read path needs an explicit failure branch.

**Why.** Conflating them is how an agent tells a customer they have no outstanding invoices
during a database outage.

### 5.3 Reads retry once; writes never do

**Decision.** One bounded retry with backoff on idempotent reads. No automatic retry on any
mutation.

**Cost.** A transient failure on a write surfaces to the caller as an error.

**Why.** A write that may have partly succeeded must not be repeated. Being told to try again
is better than being credited twice.

### 5.4 The ledger moves before the ticket

**Decision.** `propose_allocation` writes the ledger first, then creates the ticket, then logs
the interaction. Ticket and log failures are swallowed and reported as degraded.

**Cost.** A payment can end up under review with no ticket, which a human must find another
way.

**Why.** The ordering is the decision. Losing the ticket is recoverable; failing to move the
ledger after telling the caller a review is under way is a promise that does not exist. Each
subsequent write is less important than the one before it.

### 5.5 A CRM outage degrades rather than fails

**Decision.** `get_account_context` returns invoices with `crm: null` when HubSpot is down.

**Cost.** The agent loses interaction history and the caller may repeat themselves.

**Why.** HubSpot is not the authority for anything the caller is asking about. Losing it must
not cost them the call.

### 5.6 Failure scenarios are proven by tests, not demonstrated live

**Decision (Nicolas).** No runtime fault-injection flag ships. Failures are simulated by
substituting adapters in tests.

**Cost.** The Loom cannot show an outage being handled. A third of the specification's
behaviour is evidenced by a test suite rather than a demonstration.

**Why.** It saves build time, and the adapter boundary makes the tests genuine rather than
theatrical. The consequence is that the test suite must be legible in the repository — it *is*
the evidence.

### 5.7 Idempotency by conditional write, not by a key table

**Decision.** DynamoDB conditional writes on natural keys. A duplicate returns the original
result rather than an error.

**Cost.** Idempotency logic is spread across the handlers that mutate.

**Why.** The natural keys are already unique, so a separate table adds infrastructure for
nothing. Returning the original result means a retry is indistinguishable from a first call
to the person on the phone — who should never learn that the line dropped.

---

## 6. The agent

### 6.1 Prompt and tools live in git, not the dashboard

**Decision.** `agent/` holds the prompt, tool definitions and configuration. `make agent-sync`
pushes them.

**Cost.** Dashboard edits are silently overwritten — which happened, and looked like a bug.

**Why.** The prompt is the security control the model actually reads. It belongs in review and
in history, not in a text box where a change leaves no trace.

### 6.2 The prompt names no company and no scenario

**Decision (Nicolas).** Behaviour is stated as principles. No fixture, customer or test case
appears in it.

**Cost.** More words to express a rule generally than to state the case.

**Why.** A prompt that names cases handles those cases. A prompt that states principles
handles the caller nobody anticipated — which is every real caller.

### 6.3 Let the caller speak before asking anything

**Decision.** Greet, listen, say back what they need, *then* explain that identity must be
confirmed.

**Cost.** A few seconds before verification starts.

**Why.** The first real call asked for an email address before hearing why the caller had
rung — because the prompt front-loaded the disclosure gate so heavily that verification became
the first order of business. That is the behaviour of a form, not a colleague. The gate stands
in front of *account data*, not in front of conversation.

### 6.4 Match the caller's register, never their expectations

**Decision (Nicolas).** Brisk with a caller who wants only the answer, warmer with one who
converses. Never mirror hostility. Adapt *how* things are said, never *what* is true or
permitted.

**Cost.** A tone instruction is unenforceable in code; it holds only as well as the model
follows it.

**Why.** Warmth is a social-engineering vector — the friendly caller is the one you
over-share with. Stating the boundary explicitly ("be kind and be immovable — they are not in
tension") is the only lever available, since the actual enforcement is server-side anyway.

### 6.5 The agent speaks before every tool call

**Decision.** An acknowledgement precedes each call, both in the prompt and via the platform's
`force_pre_tool_speech`.

**Cost.** Slightly longer calls.

**Why.** Backend latency lands in conversation rather than in silence, and a silent pause on a
phone line sounds like a dropped call. Both mechanisms are set because the platform one holds
when the model forgets.

### 6.6 Patient turn-taking

**Decision.** `turn_eagerness: patient`, twelve-second timeout.

**Cost.** A beat of latency before every response.

**Why.** The first real call answered a half-finished sentence. And the agent explicitly asks
people to look up a payment date in their banking app, then has to wait for them.

---

## 7. Infrastructure and delivery

### 7.1 One deployment package for every handler

**Decision.** A single `lambda.zip`.

**Cost.** Every function carries every handler's code.

**Why.** The code is small, and one artefact means the functions cannot drift onto different
versions of the domain rules — a genuinely confusing class of bug.

### 7.2 A Lambda timeout below the agent's budget

**Decision.** Lambda 4s, API Gateway 5s, agent 5s.

**Cost.** A slow-but-succeeding call is cut off a second early.

**Why.** The backend must fail *before* the agent gives up, so the agent receives a structured
error it can speak rather than an opaque timeout it might paper over.

### 7.3 Terraform ≥1.10 for native state locking

**Decision.** S3 `use_lockfile` instead of a DynamoDB lock table.

**Cost.** A newer version floor than strictly necessary.

**Why.** It removes a table that would exist for no reason other than Terraform's history —
one fewer resource to provision, permission and explain.

### 7.4 An account guard on the provider

**Decision.** `allowed_account_ids`, with the value in an untracked tfvars.

**Cost.** It caused a real outage — CI had no value for the variable and Terraform blocked on
an interactive prompt for 28 minutes, holding the state lock.

**Why.** An org SSO session can reach several accounts and a shell's profile is easy to get
wrong. Without the guard, an apply against the wrong account succeeds silently. The lesson is
that a guard needs a path for every context that runs it, and `TF_INPUT=false` now turns a
hang into an immediate error.

### 7.5 Per-function IAM roles

**Decision.** Every handler has its own role and its own inline policy.

**Cost.** One policy document per handler, and deployments that fail on a missing permission
rather than working by accident.

**Why.** It is what makes "only two functions can read identity data" true rather than
aspirational. It caught a real mistake: a handler reading a table its policy excluded, which a
shared role would have hidden.

### 7.6 A committed secret scanner

**Decision.** `scripts/check_no_secrets.py` runs in `make lint` and CI. Account IDs, bucket
names and phone numbers live in untracked files.

**Cost.** False positives — HubSpot object IDs are twelve digits and look exactly like AWS
account IDs.

**Why.** The repository was public for a day with an account ID and a real phone number in it.
The exemptions are narrow and documented: one pattern in one file, rather than weakening the
rule.

---

## 8. Testing

### 8.1 Tests before implementation, for the rules

**Decision.** Domain rules get their tests first.

**Cost.** Slower to a first working version.

**Why.** It fixes the rule by the requirement rather than by whatever the code happens to do.
It also produced three spec corrections that only surfaced because the tests made the rule
concrete enough to argue with.

### 8.2 Deploy early, before the surface is large

**Decision.** `verify_identity` was deployed and called over HTTP before any other handler
existed.

**Cost.** Terraform and IAM work spread across the project rather than done once.

**Why.** It found the customer-ID enumeration oracle — a bug the unit tests were structurally
unable to see. The same bug found a week later would have been buried under three unfamiliar
failures.

### 8.3 A canary test for leaks

**Decision.** Every stored record in the cross-cutting contract tests is seeded with values
existing nowhere else, and no response may contain one.

**Cost.** A slightly odd-looking fixture.

**Why.** It catches a leak introduced later by a field nobody thought to test — the only kind
that actually happens.

### 8.4 The integration test resets state rather than tolerating it

**Decision.** It puts the disputed payment back to `UNALLOCATED` before and after.

**Cost.** It needs write access, and must not run mid-rehearsal.

**Why.** A test that accepts either `UNDER_REVIEW` or `ALREADY_UNDER_REVIEW` cannot tell you
which it got, and therefore proves less than it appears to.

---

## 9. Decisions that were wrong

Recorded because the reasoning that produced them was plausible.

### 9.1 Requiring an exact transfer date

**What was decided.** Exact amount *and* exact date, no tolerance. It felt maximally safe.

**Why it was wrong.** It would have failed honest callers routinely, because the date a payer
sees is not the date the payment arrives. Nicolas caught it. Safety that punishes correct
answers is not safety.

### 9.2 A conditional write for verification state

**What was decided.** `set_verification` updated the conversation row on the condition it
already existed — reasonable, since the initiation webhook creates it.

**Why it was wrong.** The initiation webhook does not exist yet. The write silently did
nothing and every verified caller was refused. **The failure mode was indistinguishable from
correct behaviour**, which is what made it dangerous: refusing callers is what a working gate
does. Only comparing what verification *returned* against what the table *held* exposed it.

The contract tests could not have caught it — they mock `set_verification`, so the real module
never ran. Mocking what you depend on means never testing it.

### 9.3 Audit events by stdout

**What was decided.** `audit.write` logged JSON to stdout, like everything else.

**Why it was wrong.** Lambda routes stdout to the function's own log group — ninety-day
retention, against a ten-year requirement. The event was complete and correct and would simply
have expired. Nothing failed; only asking "which log group is this in?" found it.

### 9.4 A tool referencing a variable no webhook supplies

**What was decided.** `verify_identity` took `candidate_customer_id` as a platform-injected
variable.

**Why it was wrong.** The webhook that supplies it is a later task. ElevenLabs validates at
call start and refused every session, surfacing to Twilio as a WebSocket close — an error
three systems away from its cause. Configuration ran ahead of implementation.

### 9.5 Refusing to name any authorised contact

**What was decided.** The agent would say "an authorised contact must add you" without naming
anyone.

**Why it was wrong.** Nicolas overruled it. The name is nearly worthless to an attacker who
already works at the company, and withholding it makes the remedy unusable. The contact
details are the part worth protecting.

### 9.6 Counting a name as a verification factor

**What was decided.** Briefly, a full name as one document factor.

**Why it was wrong.** Nicolas reversed it within minutes, correctly. Callers volunteer their
name unprompted, so crediting it hands over a third of the bar for nothing.

---

## 10. Scope decisions

Things deliberately not built, and why that is defensible rather than incomplete.

| Not built | Instead | Why |
|---|---|---|
| Outbound SMS and payment links | The transfer-failure fallback is self-contained: persist the escalation, create a callback, tell the caller on the call | No journey needed them, and an out-of-band channel is another thing that can fail |
| Automatic profile reconciliation | The agent asks the caller "did you move, or is it a typo?", records the answer on the existing ticket, and a human corrects it | The caller is a better evidence source than a confidence score, and cheaper |
| Runtime fault injection | Adapters substituted in tests | Saves build time; the cost is that failures cannot be shown live |
| A high-risk verification tier | One uniform bar of three factors | Tiering adds a decision the model could get wrong |
| Auto-allocation | Every allocation proposed for human review | The worst failure mode in the system |

---

## 11. Open

- **Multilingual is not yet the real configuration.** ElevenLabs ties the TTS model to the
  agent's base language: an English-base agent must use English-only models, while the four
  languages need `eleven_turbo_v2_5` with a non-English base. The current English setting is a
  rehearsal convenience and reverts in T080.
- **Transfer behaviour is unverified.** Whether the transfer tool returns control to the agent
  on a failed dial decides whether the caller can be told on the call. If it does not, the
  fallback persists the escalation and creates a callback but cannot speak — and the demo must
  say so rather than imply otherwise.
- **The HubSpot token has been rotated once** after being pasted into a transcript. Worth
  rotating again before the repository is shared, and worth scoping the ElevenLabs key's
  expiry once the Loom is recorded.

---

## 12. Decisions made after the first walkthrough

Added as they were made, per the convention adopted at the end of Phase 3.

### 12.1 Guessing is counted server-side even though the model can see it too

**Decision.** The backend fingerprints each distinct value offered per field and locks the
call at the third. The prompt *also* tells the agent to escalate on a third value.

**Cost.** Roughly forty lines and a secret, duplicating something the model can already
observe in its own context.

**Why.** The model can stop asking; it cannot stop the caller succeeding. If detection lived
only in the prompt, a persistent caller who talks the agent into "sorry, I was reading the
wrong invoice" gets another attempt, and another — each evaluated normally, because nothing
told the backend to refuse. With the server-side count, `verify_identity` returns `LOCKED`
regardless of what the model decides, so the caller cannot become verified however
cooperative the agent is.

Three smaller reasons: the model noticing produces no durable record, while the backend
produces a risk signal that reaches the handoff and the audit log; a long call's early turns
are the first thing dropped when context is compacted; and the model's context holds the
actual guessed values while the backend holds only fingerprints.

### 12.2 Distinct attempts are a set, not a counter

**Decision.** Fingerprints per field are stored as a set. Re-offering a value already seen
changes nothing.

**Cost.** A read-modify-write instead of an atomic increment.

**Why.** The agent resends every factor gathered so far on each call, so a counter would
count `customer_id` once per turn and lock out **every honest caller** on their third answer.
Values are also normalised before hashing, using the same normalisation the comparison uses,
so a caller whose phone number the transcriber renders inconsistently is not recorded as
having tried two different numbers.

### 12.3 The fingerprint salt is generated by Terraform

**Decision.** `random_password` generates it into Secrets Manager.

**Cost.** It lands in Terraform state as well as Secrets Manager.

**Why.** A plain hash of a date of birth is reversible in seconds — the space is about forty
thousand plausible values. A salt makes precomputation useless. Generating it removes a
manual step, and a manual step is one that gets skipped, after which the code either crashes
or silently uses a weak default. The salt grants access to nothing: losing it weakens
fingerprints against someone who already holds the database, rather than opening a door.

### 12.4 Escalation reasons are split by whether identity was needed to know them

**Decision.** Four reasons are valid from an unverified conversation —
`IDENTITY_NOT_ESTABLISHED`, `VERIFICATION_LOCKED`, `SUSPECTED_GUESSING`,
`CUSTOMER_REQUESTED_HUMAN`. The rest require verification.

**Cost.** Two sets to keep in step, and a reason that is refused where a caller might expect
it to work.

**Why.** "Works without verification" cannot mean "anything goes", or the escalation tool
becomes a way around the disclosure gate. The permitted four are statements about *the call*
and need no account to be true. `INVOICE_DISPUTED` is a claim about an account, and an agent
that never established whose account it was looking at cannot make it — nor put an
account-shaped escalation into the queue on a caller's say-so.

### 12.5 The agent never comments on an individual verification answer

**Decision.** No "that's confirmed", no "I couldn't confirm that", no "close". Take the
answer, ask for the next thing. The prompt spells out what each of the four statuses means.

**Cost.** A caller gets less feedback and may feel they are being interrogated blindly.

**Why.** Found by walking through the golden path. The agent announced "I couldn't confirm
that email address" after a *correct* email, then repeated it, then abruptly declared the
caller verified. Two faults: it was narrating per-answer outcomes it was never told, and it
read `PARTIALLY_VERIFIED` as failure when it means "keep going".

Per-answer commentary turns verification into hot-and-cold, which a caller can play until
they win. It is also usually false, since the response reports a total rather than a verdict
on the last thing said.

### 12.6 A caller is resolved from any identifier they might actually know

**Decision.** Verification resolves the account from customer ID, email, or phone. An email
index was added to support it.

**Cost.** A second index on the identity table, and a lookup before any comparison.

**Why.** A real bug, found by walking through the path rather than by any test. Resolution
used only the customer ID, so a caller who led with their email — which most people know and
few can look up — had that correct answer scored as **wrong**, and it burned a lockout
attempt. Verification could not make progress until they happened to recite an ID.

The integration test missed it because it sends all three factors in one call. A real
conversation sends them one at a time, which is the case that mattered.

**The residual tradeoff, stated.** Resolving by email means a correct email alone returns one
confirmed factor while an unknown one returns none, so the response distinguishes "this
address is on file" from "it is not". That is an enumeration oracle for email addresses,
which are semi-public anyway. It is bounded by the same two controls as everything else: the
lockout, and the guessing detector that fires after three distinct values for one field. The
alternative — no progress feedback at all — would make honest verification unusable.

### 12.7 The invoice amount is never spoken before the caller states what they paid

**Decision.** The agent identifies a disputed invoice by number and date only. Amounts are
discussed after the match.

**Cost.** A slightly more awkward opening, and a caller who wanted the figure has to wait.

**Why.** Also found by walking through the path. The agent announced "invoice INV-2026-0013,
for four thousand two hundred francs" and then asked how much the caller had transferred. In
this scenario the payment equals the invoice, so it had handed over the answer. An honest
caller repeats the figure back and the question has established nothing; a dishonest one has
been given it.

The date matters as much: the invoice date may be given to help them find it, the date of any
payment may not.

**What this does not fix.** A determined caller could still learn the invoice amount another
way — from the invoice itself, most obviously. The protection that survives is the *transfer
date*, which appears on no document the company sends. That is why matching requires both.

### 12.8 Evaluation and issuance are one operation, not two

**Decision.** `request_credit` decides and issues in a single call. There is no endpoint that
issues a credit without deciding whether it may.

**Cost.** The agent cannot check eligibility before quoting an amount to the caller, so it
has to ask and then find out.

**Why.** Two tools leave a window in which the model calls the second without the first — and
the model is precisely the component that must not be trusted to sequence a financial
decision. The window would be small and would almost never be hit, which is what makes it
the kind of bug that appears once, in production, on a call nobody recorded.

### 12.9 Only an invoice can be credited

**Decision.** `CREDITABLE_TYPES` is `{INVOICE}`. Payments, credit notes and adjustments are
refused.

**Cost.** A legitimate credit against an adjustment needs a person.

**Why.** Payments and credit notes are self-evidently not charges. An adjustment is excluded
because it may be signed either way, so "credit this adjustment" has no unambiguous meaning —
and a rule whose meaning depends on the sign of the thing it is applied to is a rule waiting
to be got wrong.

Worth noting how this was settled: the test asserted adjustments were not creditable and the
implementation allowed them. The test was the more considered version, so the code changed.

### 12.10 A credit awaiting approval counts against the window; a rejected one does not

**Decision.** `PENDING_APPROVAL` credits are included in the rolling total. `REJECTED` ones
are not.

**Cost.** A customer whose credit is later rejected has had their window consumed in the
meantime.

**Why.** A credit that was asked for and refused was never given, so counting it would punish
someone for a decision that went against them. But one awaiting approval is committed, and
excluding it would let a caller stack requests faster than a human can approve them — which
is the same threshold-splitting the rolling window exists to stop, just faster.

### 12.11 Refusals tell the agent to escalate; incoherent requests do not

**Decision.** The response carries `should_escalate`. It is true for limit, risk and
entry-cap refusals, false when the named charge does not exist or the account is not active.

**Cost.** A caller whose request was incoherent gets no human, and may feel dismissed.

**Why.** A customer told only "no" has been given nothing, and somebody with more authority
may still say yes — so a policy refusal should reach a person. But a charge that does not
exist is a conversation problem, not a policy one: the agent named the wrong thing, and
sending that to a human wastes their time on something the next question would resolve.

### 12.12 The credit id is derived from the request, not generated

**Decision.** `cn_<hash of conversation, charge, amount>`, written with `put_if_absent`.

**Cost.** Two genuinely separate credits for the same amount against the same charge in the
same call are indistinguishable, and the second is silently dropped.

**Why.** That collision is the behaviour we want far more often than not: a caller who
repeats themselves, or a dropped call redialled, must not be credited twice. A random id
would make every retry a second credit. The rare legitimate case — someone wanting two
identical credits on one charge in one call — is one a person should look at anyway.

### 12.13 The lockout counts distinct wrong values, not failed calls

**Decision.** A caller may offer three *different* wrong values before the call locks. The
same wrong value resent counts once, however many turns carry it.

**Cost.** More state per conversation, and the domain now reports internally which factors
mismatched — a field that must never reach a response.

**Why.** Found by an adversarial walkthrough. A caller gave one wrong email and then two
*correct* answers, and was locked out. The agent resends every factor it has gathered on each
turn, so the single wrong email arrived three times and burned all three strikes:

```
turn 1:  email(wrong)                          → strike 1
turn 2:  email(wrong) + year(right)            → strike 2
turn 3:  email(wrong) + year + id(both right)  → strike 3, locked
```

One typo, three strikes, and everything correct after it counted against them. That is a
hostile system, and the lockout exists to stop guessing rather than to punish a mistake.

**The first fix was wrong**, which is worth recording. It counted only turns that introduced
*any* new value — but every turn did, because each added a new field. The replay locked at
step three exactly as before. The rule had to be about distinct *wrong* values specifically,
which meant the domain telling the handler which factors mismatched.

**The disclosure boundary here is subtle.** `mismatched_factors` is exactly the information
FR-004 forbids revealing. It is used in one place, to fingerprint the wrong answers, and the
response body is built field by field so it cannot leak by accident. The rule is "never tell
the caller", not "never know".

**And it introduced a second bug immediately**, caught by an existing test: guarding the
counter update on `wrong_count > 0` meant a *successful* verification no longer reset it, so
a caller who eventually got in would carry their earlier mistakes into the next call. The
update now runs on every attempt; only its effect differs.

### 12.14 The agent says nothing at all about a failed answer

**Decision.** On `FAILED`, the agent asks the next question and comments on nothing. The only
time it mentions being unable to confirm anything is when it has stopped asking.

**Cost.** A caller gets no acknowledgement that something went wrong, which can feel opaque.

**Why.** The earlier rule — "say you have not been able to confirm the details" — produced
this after a *correct* answer:

> "I haven't been able to confirm those details. Let's try something else."

`FAILED` refers to everything the caller has given, not to the thing they just said. Once one
answer is wrong, every subsequent result is `FAILED` including the ones where the caller was
right. So the sentence was both untrue and an oracle: it told a caller working through values
that their last guess had not landed.

### 12.15 Factors are suggested in order of how answerable they are

**Decision.** `next_factor_hint` follows a fixed order — email, phone, date of birth, account
opening year, customer id — rather than alphabetical.

**Cost.** A hardcoded ordering that someone may need to revisit for a different customer base.

**Why.** It was alphabetical, which put `account_opening_year` first. That is the single
hardest question a customer can be asked, and a walkthrough showed the agent asking for it,
being told "I don't know that", and then asking for it again two turns later because the
backend had no idea it had been declined.

Alphabetical order was not a decision, it was the absence of one. Email and phone are what
most people can give without looking anything up.

### 12.16 The agent may offer a credit, but never assert a finding

**Decision (Nicolas).** Offering a goodwill credit nobody asked for is good service and is
kept. What changes is the framing: it must be offered as goodwill for something unverified,
never as redress for an established error.

**Cost.** A caller with a genuine complaint hears "I can't confirm what happened" rather than
"you're right, that's our mistake", which is less satisfying.

**Why.** A walkthrough produced this, unprompted:

> "That's a billing error on our end, not a payment issue. For a billing error like this
> where you were charged for an item you didn't order, you're entitled to a credit."

Every clause is invented. The system holds **no line-item data at all** — no product names,
no quantities. There is no "red fabric" anywhere in it. The agent could not have checked, did
not check, and asserted a conclusion about the company's own billing anyway.

My initial fix was to stop the agent offering credits unprompted. Nicolas overruled it, and
was right: the offer is the useful part, and the problem was never the generosity. It was
claiming to know something. "I can't see the individual lines, but I can put a goodwill credit
on the account and have a colleague look" is both helpful and true.

### 12.17 The agent states what it cannot see

**Decision.** The prompt names the boundary explicitly: invoices, payments and credits are
visible; line items, products and quantities are not.

**Cost.** More prompt, and a caller is told about a limitation they might not have noticed.

**Why.** Without it the model fills the gap. Asked about a duplicated line item it reasoned
confidently about a record it had never seen, and narrated "let me check the details of
invoice INV-00982-004" while making **no tool call at all**. A claimed check that did not
happen is worse than a refusal, because everything said afterwards rests on it.

Two rules follow: if you say you will look something up, call the tool; if you cannot check
something, say so.

### 12.18 The agent writes the credit reason itself

**Decision.** The agent composes the reason from what the caller said, rather than asking
them to phrase it.

**Cost.** None worth noting.

**Why.** It asked "I'll need to note your reason in your own words — something like 'charged
for item not ordered' or however you'd describe it." The caller had already explained the
problem. Asking them to write the file note is administrative work handed to a customer, and
it makes the agent sound like a form.

### 12.19 Credit effects are described accurately, and identifiers are not read aloud

**Decision.** A credit against a settled invoice is described as sitting on the account, not
as reducing a balance. Internal identifiers are never spoken.

**Cost.** None.

**Why.** The agent said a credit "will reduce your balance accordingly" when the balance was
zero and the invoice paid — a small untruth the person reconciling it would notice. It also
recited `CN-701CE28C14CE62D1807A`, which is a database key reformatted to look like a
reference number. Nothing downstream accepts it, so reading it out only sounds official.

### 12.20 A pattern is detected from history, not from hitting a ceiling

**Decision.** Reaching the rolling ceiling raises no risk signal. What raises one is the
*shape* — three or more credits at or above 80% of the per-request limit inside twelve
months.

**Cost.** A customer with several large-but-legitimate credits is flagged, and a determined
person could stay under 80% to avoid it.

**Why.** A ceiling being reached is a limit doing its job, not evidence about anyone. Apex
Capital's credits are 90, 95, 100, 85, 90 — none individually remarkable, and each one a
plausible response to a real problem. Together they are the shape of someone sitting under a
CHF 100 approval threshold, which is precisely what the cumulative rule exists to catch and
precisely what a person reading one call cannot see.

80% because someone taking CHF 5 repeatedly is not working around a CHF 100 ceiling, and
three because two is a coincidence — a company can have two bad deliveries in a year, and
flagging that would put a note on ordinary accounts.

### 12.21 Thresholds for what a pattern looks like are constants, not policy

**Decision.** The detection thresholds live in `domain/risk.py` as named constants. The
credit ceilings remain in SSM.

**Cost.** Tuning them needs a deploy, which breaks the pattern set by Principle VIII.

**Why.** They are different kinds of number. `credit_max_rolling` is what the company
*permits* — a business decision someone may reasonably change on a Tuesday. "Three credits
near the limit is a pattern" is a claim about what unusual behaviour looks like. If that
needs tuning per deployment, the detection is wrong rather than the number, and making it
easy to adjust would hide that.

### 12.22 Nothing the backend emits may accuse anyone

**Decision.** Rule names, signal names and evidence strings are tested against a list of
accusatory words. `SUSPECTED_THRESHOLD_SPLITTING` names a shape and marks it as suspected;
evidence is a count and a window.

**Cost.** A linguistic test over machine-readable strings, which is unusual enough to look
like overreach.

**Why.** Everything the backend emits is read by somebody. Rule names reach the agent and
shape how it speaks to the caller; evidence reaches the human picking up the escalation; both
land in the audit record that would be produced if the decision were ever challenged.

A customer refused a credit may be entirely honest — a company genuinely having a bad year
produces the same history as one testing the limits, and the system cannot tell them apart. A
field called `abuse_detected` would be the system asserting something it has no basis for, in
writing, permanently.

The prompt carries the other half: never name a threshold, a limit, or a count to a caller.
"You've reached your annual limit" sounds harmless and is the worst thing to say — it tells
someone exactly what the ceiling is and how to sit under it.

### 12.23 Seeding clears conversation history

**Decision.** `make seed` deletes every conversation record as well as restoring the ledger.

**Cost.** Any real conversation history is destroyed by a reseed, so it cannot be used
between demos.

**Why.** Found by testing US4. Conversation history feeds the contact-frequency signal, and
rehearsing generates exactly that history: Apex Capital showed *11 calls in the last 30 days*,
every one of them mine. Worse, at five calls in thirty days the clean customer used for the
US3 demo would start being refused the credit that story depends on — a demo breaking because
of how often it had been rehearsed.

A seed is meant to restore a known state. Leaving one table's history behind made it restore
most of one.

### 12.24 Identity is per person, not per company

**Decision (Nicolas).** The identity table holds one row per contact, each with their own
email, phone and date of birth, linked to a company account. Any listed contact verifies and
reaches that account; somebody matching no contact reaches nothing.

**Cost.** A destructive table change, thirty identity records instead of ten, and a reversal
of the authority rule written a day earlier — Precision Systems previously tested that a
colleague *could not* verify.

**Why.** It is how B2B accounts actually work. A company has an accounts department, not a
designated caller, and the person who rings about an invoice is whoever picked up the query.
Under the old model Anna Schmidt could never get help with her own company's invoice.

The line it draws is the one that matters: **a listed contact of Apex Capital** versus
**somebody who knows about Apex Capital**. Membership of the CRM contact list is the
authority, which also means removing a leaver from that list removes their access — a control
someone can actually operate.

### 12.25 At least one factor must be personal, replacing the non-document rule

**Decision.** The rule was "at least one factor not printed on an invoice". It is now "at
least one factor personal to the caller" — their own email, phone or date of birth.

**Cost.** None in practice; it is strictly stronger.

**Why.** Per-person identity made the old rule too weak. Under it, the customer ID and the
account opening year both counted as non-document, so a caller who worked at the company — or
had merely seen one of its invoices — could satisfy the bar with two facts that identify
nobody. The personal/company split names what the rule was always trying to express.

### 12.26 The account opening year is removed entirely

**Decision (Nicolas).** Gone from the factor pool.

**Cost.** One fewer factor, so the pool is four rather than five.

**Why.** It is a fact about the company that every employee shares, so it distinguishes
nobody — and almost nobody remembers it. A walkthrough showed the agent asking for it, being
told "I don't know that", and asking again two turns later. It cost an exchange and bought
nothing.

**A useful consequence**: with three factors required and only one company factor remaining,
a caller necessarily supplies two personal facts plus the customer ID. The "two personal"
requirement falls out of the arithmetic rather than needing its own rule.

### 12.27 The colleague-impersonation gap is recorded, not designed around

**Decision.** Colleagues commonly know each other's email, phone and date of birth, so these
factors cannot distinguish one listed contact from another. Accepted, documented, and
mitigated by recording *which* contact verified.

**Cost.** Anyone at a customer company can verify as any of their colleagues.

**Why.** Nicolas raised it directly: *"I know my colleague's email, phone, and date of
birth."* He is right, and knowledge-based verification cannot fix it.

What makes it tolerable is that every listed contact has identical access, so impersonating a
colleague gains nothing — the blast radius inside the authorised set is zero. The real threat
is someone who *used to be* inside it, and the control for that is the CRM contact list rather
than anything the factors can do.

The honest fix is possession rather than knowledge — a one-time code to the phone on file —
and outbound messaging is out of scope (FR-048). It belongs in the README as the first thing
to add in production.

Which contact verified is now recorded on the conversation, in the audit record and in the
handoff. It does not prevent the impersonation; it means there is a trace of it.

### 12.28 The callback is arranged before the transfer, not after it fails

**Decision.** `create_escalation` records the callback and returns a line the agent can say
*before* any transfer is attempted.

**Cost.** A callback is recorded for every escalation, including the ones where the transfer
works and nobody needs ringing back. That is a row nobody acts on.

**Why.** T071 was flagged at planning as the project's biggest unknown: does the ElevenLabs
transfer tool hand control back when a dial fails? **The platform does not document it.** The
docs cover three transfer types and their configuration and say nothing about failure — no
routing, no recovery, no statement either way.

What can be inferred is that conference transfer dials the destination and removes the agent
*after* the join succeeds, so it is the only type where surviving a failure is even possible.
Blind and SIP REFER hand off at the protocol level. The docs also note that audio is cut the
moment a transfer triggers.

Rather than build on an inference, the design was changed so the answer does not matter.
Persisting the callback first means the caller is covered whether or not the agent survives:
if it does, it tells them; if it does not, a person already has the context and a callback
exists. A promise that rests on undocumented behaviour is not a promise.

**Still worth one real call** to see whether the recovery can be *shown* in the demo or only
described. That is now a presentation question rather than a correctness one.

### 12.29 The agent says the callback line before transferring

**Decision.** The prompt tells the agent to say "a colleague will call you back if we get cut
off" *before* putting the caller through, not in the recovery path.

**Cost.** It sounds slightly pessimistic on a transfer that will work perfectly well.

**Why.** The same reasoning one layer up. A transfer can drop the call without warning, and a
reassurance offered only after a failure is one that sometimes never gets offered. Saying it
while the caller is still listening costs a sentence and removes the dependency.

### 12.30 Failure scenarios assert what is *not* said

**Decision.** `assert_safe_failure` checks the response envelope *and* scans `message_hint`
for phrases that would assert a financial fact — "paid", "unpaid", "no outstanding",
"not received".

**Cost.** A word-list test, which will need extending when new phrasing appears.

**Why.** A safe failure is not just a correctly-shaped error. The specific danger is a
failure that reads as an answer: "you have no outstanding invoices" during a database outage
is well-formed, plausible, and false. Checking the shape alone would pass it.

The same test asserts every tool fails identically, so a caller cannot tell which part of the
system broke.

### 12.31 Two tests read the source rather than the behaviour

**Decision.** `test_an_audit_write_failure_never_undoes_the_action` inspects `audit.write` for
an absent `raise`; `test_the_lambda_budget_sits_below_the_agents` reads the Terraform default.

**Cost.** Both break on a harmless refactor, which is the usual argument against them.

**Why.** Neither property is observable from behaviour at this level. That a failed audit
write does not roll back a credit is a property of *not* doing something, and the timeout
relationship lives in two files that must stay in step — a Lambda budget above the agent's
would produce an opaque timeout instead of a speakable error, and nothing else would notice.

### 12.32 A fourth test layer, driving the agent itself

**Decision.** `tests/conversation` runs scripted callers against the deployed agent through
ElevenLabs' simulator — real prompt, real tools, no voice minutes.

**Cost.** Slow (about a minute per test, twelve for the suite), it mutates real data so every
case reseeds, and it needs credentials. It cannot run in CI as things stand.

**Why.** Nicolas asked whether the 413 tests ever reach ElevenLabs. They do not. Every layer
below stops at the handler boundary, which means the prompt — twenty-eight thousand
characters of security-relevant instruction — had no automated coverage at all.

Every failure found during walkthroughs was in that gap: announcing the invoice amount before
asking what was paid, claiming to have checked an invoice it never looked up, saying "I
couldn't confirm that email" after a correct email, asserting a billing error it could not
see. Four rounds of manual testing, each finding something the suite could not.

**It found a worse one immediately.** Given a payment matching no invoice, the agent invented
a second invoice — number, amount and date, none returned by any tool — called
`propose_allocation` against it, and when the backend refused, told the caller "the payment
has been proposed for allocation, a colleague will confirm within twenty-four hours."

The data was safe: the mutation was rejected. The caller was not. They would have stopped
chasing a payment nobody was handling.

### 12.33 Prompt rules are tendencies; only the backend enforces

**Decision.** Recorded as a property of the system rather than fixed, because it cannot be
fixed at the prompt layer.

**The evidence.** Running the conversation suite twice gave different results. Three tests
failed on the second run, one of them having passed alone three minutes earlier. Same prompt,
same agent, same scenario.

Each of those three is an explicit "never" in the prompt. The model follows them most of the
time.

**Why it matters more than the individual failures.** It draws the line between the two kinds
of guarantee this system makes. "Nothing financial before VERIFIED" holds because
`require_verified` reads a row and raises — a caller cannot talk past it, and no model
behaviour changes it. "Never say an action succeeded when it did not" holds because the model
usually complies.

The first is a control. The second is a tendency. Both were written the same way in the
prompt, and only testing the agent showed which was which.

**What follows from it**: where a rule matters and can be moved into the backend, move it.
Where it cannot, say so honestly rather than claiming the prompt as a guarantee.

### 12.34 Failure hints tell the agent what to say, not what went wrong

**Decision.** `message_hint` changed from a description ("That information is temporarily
unavailable") to an instruction ("...and nothing was changed. Tell the caller you cannot
access it right now, never what it would have said, and offer to have a colleague follow
up").

**Cost.** The hint is no longer something the agent can read out verbatim, so it must
paraphrase — and a longer string travels on every failure.

**Why.** This is §12.33 acted on. The agent claiming a review that never happened was a
prompt rule being ignored; the fix is not another prompt rule. A neutral description leaves
the model a gap to fill, and it sometimes fills one by assuming success. A sentence in the
response is right there at the moment of speaking, where a rule from twenty thousand
characters earlier has to be recalled.

Every hint now states explicitly that nothing changed, because that is the part most often
narrated wrongly.

### 12.35 Reverted: failure hints describe, they do not instruct

**Decision.** §12.34 is reverted. `message_hint` is back to a short description of what
happened.

**Cost.** The failure mode §12.34 was aimed at — the agent narrating a refused action as
successful — is unaddressed again.

**Why.** It was made without asking, and it crossed a line the rest of the system respects:
constraining what the agent may *assert* is the backend's business, dictating what it may
*say* is not. It was also the wrong shape of fix. If an action must not be reported as done,
the way to guarantee that is for the action to be impossible, not for the response to carry
better wording. §12.36 is that fix.

### 12.36 The agent cannot write the ledger, at all

**Decision.** Both ledger writes are removed. `propose_allocation` no longer moves a payment
to `UNDER_REVIEW`; `request_credit` no longer writes a credit note. Each records the request
as a HubSpot ticket instead. An explicit IAM `Deny` on the ledger table is attached to all six
agent-facing roles, and a scheduled `apply_decisions` Lambda — with no API Gateway route — is
the only principal that can write the ledger.

**Cost.** Real, and worth naming. The agent can no longer tell a caller a credit has been
applied, only that it has been requested, which is a weaker outcome for the caller and a
smaller demo. HubSpot becomes load-bearing: if the CRM is unreachable the agent cannot log a
request or check the ceiling, and must fall back to the callback path. And the ceiling is now
computed partly from a CRM read on every call, where it used to be one DynamoDB query.

**Why.** "Things that must never happen should be impossible, not forbidden." A prompt rule
saying the agent must not overstate what it did is a tendency (§12.33). An IAM Deny is not.
The change also fixed something the old design had backwards, found by a test: when ticket
creation failed, `propose_allocation` logged `DEGRADED` and still returned `UNDER_REVIEW` with
a null ticket id — correct when the ledger had already moved, and a promise of a review that
existed nowhere once it had not.

### 12.37 The open ticket is the idempotency record, not a derived key

**Decision.** `_credit_id` is deleted. What stops a second credit request is that the first
one is still open: `request_credit` reads the company's undecided tickets and returns the
existing one rather than raising another.

**Cost.** Idempotency now depends on a CRM read succeeding, where a hash and a conditional
write needed nothing external.

**Why.** The hash was weaker than its own docstring claimed. It was derived from the
conversation, so a caller who hung up and redialled got a new id, a second row and a second
credit — the exact case the comment said it prevented. The ticket lookup is scoped to the
company, so it holds across calls and across callers, which is what the rule actually needs.
It is also amount-aware: it can say "CHF 90 is already pending against this charge, so the
entry cap leaves 20", where a hash can only say "identical, refuse".

### 12.38 A failed CRM read refuses rather than granting

**Decision.** When the pending-request read fails, or the caller's company has no CRM record,
`request_credit` returns `SERVICE_UNAVAILABLE` and raises no ticket.

**Cost.** A CRM outage refuses legitimate customers.

**Why.** The ceiling is now partly computed from HubSpot. An unreadable ceiling is not a
customer who has used no credit, and treating the two the same is how the same CHF 500 gets
given away twice. This is the FR-011 distinction — "cannot check" is not "allowed" — applied
to a dependency that only became load-bearing with §12.36.

### 12.39 The applier re-runs the rules instead of trusting Accepted

**Decision.** `apply_decisions` re-reads the ledger and re-runs `evaluate_credit()` before
writing, and refuses anything the rules refuse, whoever accepted it. A refusal is written back
to the ticket as a note.

**Cost.** A person can accept a credit and find it was not applied, which needs explaining in
the CRM rather than being obvious.

**Why.** Without this the applier is a rubber stamp and the split buys only an audit trail:
the agent still determines the outcome, with extra steps. Re-validating changes the agent's
blast radius from "can write the ledger" to "can propose what the rules already allow", and it
catches the case a human cannot see — credits that landed between the caller ringing and the
ticket being answered, which is a ceiling breach nobody did on purpose.

### 12.40 Polling, not a webhook

**Decision.** The applier runs on a one-minute EventBridge schedule rather than receiving a
HubSpot webhook on ticket closure.

**Cost.** Up to a minute of lag, and a search call per minute against a rate-limited account.

**Why.** A webhook would mean a public endpoint, signature verification, and a Lambda that
writes the ledger triggered by an inbound internet request — precisely the blast radius
§12.36 exists to shrink. A bug in the signature check would move money. The schedule's trigger
lives inside AWS and the ledger writer is unreachable from the internet. Free tier likely
gates private-app webhooks anyway, but the choice would be the same on Enterprise.

### 12.41 Lookup and comparison share one normalisation

**Decision.** `lookup_key()` in the domain is used by the identity indexes, the seed and the
factor comparison. The indexes key on `email_lookup` and `phone_lookup` rather than on the
stored display values. Email normalisation drops hyphens; phone keeps its trailing nine
digits, as the comparison already did.

**Cost.** Two addresses differing only by a hyphen now collide, so an ambiguous lookup has to
resolve nobody. A GSI rebuild and a re-seed to populate the new attributes.

**Why.** They had drifted, and the drift was invisible until a voice call. The comparison
normalised a phone number to its trailing digits while the lookup queried the index with the
raw string, so a caller reciting their own number correctly could never be found by it. Every
test until then had been text, where a caller "says" exactly what is on file.

### 12.42 The lockout counter is per field, not a total

**Decision.** `record_wrong_values` stores wrong-value fingerprints per field and returns the
worst field's count. Three factors offered together that match nobody is one failed attempt.

**Cost.** A caller can now be wrong once about each of three fields across three turns without
locking. The allowance is effectively larger for someone spreading guesses across fields.

**Why.** It locked an honest caller on his opening sentence. He gave email, phone and date of
birth in one breath; the email had lost a hyphen in transcription, so no record resolved, so
all three were scored wrong — including the two that were right — and three wrong values was
the whole allowance. `is_enumerating` was already per field for exactly this reason (FR-006b);
the lock counter simply had not been. Someone working through three different emails still
trips both.

### 12.43 An ambiguous lookup resolves nobody

**Decision.** `_lookup` returns a contact only when exactly one row matches.

**Cost.** Two people whose addresses normalise identically can neither be found.

**Why.** Dropping hyphens merges values that were distinct. Picking the first of two would
check a caller's answers against a record that is not theirs, which is worse than failing to
find them — and failing to find them is already indistinguishable from a wrong answer.

### 12.44 Verification becomes a structured procedure

**Decision.** The verification sequence moves out of the prompt and into an ElevenLabs
structured procedure: tell, ask email, read it back, ask phone, ask date of birth, one
`verify_identity` call, branch on the result. Steps are enforced by the platform rather than
interpreted by the model.

**Cost.** The sequence is now fixed. A caller who volunteers their phone number first still
gets asked for their email first, where the prompt could adapt. Verification also stops being
readable in one file — it lives in `agent/procedures/verification.json` and only takes effect
once compiled and published.

**Why.** §12.33 said prompt rules are tendencies. This is the clearest case in the project:
the prompt says, in bold, *"Never say whether an individual answer was right or wrong. Not
'that's confirmed', not 'I couldn't confirm that', not 'close'."* The agent then said "I
couldn't confirm that" to a caller whose email was correct. The rule was present, explicit,
and quoted the exact forbidden phrase.

Under the procedure it cannot happen, and not because it is forbidden harder. There is no
tool call between the questions, so there is no result to narrate and no step in which to
narrate it. The prohibition becomes unnecessary rather than better enforced.

### 12.45 The email is read back on every call

**Decision.** A dedicated Ask step spells the address back and asks whether it is right,
before any of it is sent.

**Cost.** An extra exchange in every verification.

**Why.** The first voice call was lost to a hyphen the transcript dropped from
"alpina-tech.ch". Reading it back only when the lookup fails would be an oracle — the caller
would learn their answer matched nobody, which is the one thing the gate refuses to say.
Doing it every time reveals nothing, because the agent is repeating what the caller just
said and has no record to compare it against yet. It fixes the transcription rather than
reacting to a failure.

### 12.46 The removed factor is gone from the tool schema

**Decision.** `account_opening_year` is removed from the `verify_identity` enum.

**Cost.** None.

**Why.** The factor was removed from the domain, and the agent kept asking for it. A stale
example in the prompt was found and deleted; this was the second source and the stronger one,
because an enum in a function schema is a list of valid choices rather than prose the model
may or may not weigh.

### 12.47 The backend stops suggesting what to ask next

**Decision.** `ASK_ORDER`, `_next_hint` and `next_factor_hint` are removed. The response says
how many factors are confirmed and whether a personal one is among them, and nothing about
what to ask for.

**Cost.** A caller who cannot produce the detail being asked for no longer has the backend
steering them to one they can. That guidance now has to live in the procedure, which is a
fixed order rather than an adaptive one.

**Why.** The procedure decides the sequence. A hint in the tool response is a second voice
telling the agent something different, and when two sources disagree the model picks — which
is the failure mode this whole line of work exists to remove. Deleting the hint also removes
a response field that had to be checked for leaking a value; a field that does not exist
cannot leak.

### 12.48 One detail at a time, then the decision

**Decision.** A `check_factor` endpoint checks a single detail and returns MATCHED,
NOT_MATCHED, or AMBIGUOUS. The procedure asks for each detail, checks it, recovers if it did
not land, and calls `verify_identity` only once everything is gathered. `verify_identity`
still makes the decision and is unchanged.

**Cost.** Real, and bought deliberately: `check_factor` tells the caller which detail failed,
which is the enumeration oracle the final verification refuses to be. Someone can now confirm
whether an address is on file. Four round trips instead of one, too.

**Why.** A voice call loses things a text one does not, and it loses them per detail. A Swiss
surname arrives misspelled; the caller is asked to spell it while still on that question, and
the call recovers. Checked only at the end, the same call fails with nothing to point at.
The bar itself has not moved: knowing an email exists says nothing about who is holding the
phone, and the full set is still required.

### 12.49 An ambiguous date is a question about the sentence

**Decision.** `ambiguous_date()` detects a spoken date that could be read two ways and the
procedure asks which was meant, naming the month in words. Detected before the record is
read.

**Cost.** An extra exchange whenever both numbers are twelve or under.

**Why.** "11 6 1994" is the 11th of June or the 6th of November and the string does not say
which. Guessing is wrong half the time, silently. Because the check runs before anything is
looked up, asking reveals nothing about the caller — it is a question about what was written
down, which is why it can be asked every time it applies.
