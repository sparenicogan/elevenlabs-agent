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
