# US3 — Goodwill credit requested within policy

**PASSED** — 3 of 3 checks. Run 2026-09-04 18:55 UTC against the deployed agent.

## Checks

| | Check | Why it matters |
|---|---|---|
| PASS | a credit is requested | The rules are evaluated in full on the call. |
| PASS | it is described as requested, never as applied | The agent cannot apply a credit; a person accepts the ticket. |
| PASS | no charge is called an error | It cannot see line items, so it cannot know. |

## Tools called

```
 1. check_factor
 2. check_factor
 3. check_factor
 4. verify_identity
 5. get_account_context
 6. request_credit
```

## Transcript

**agent** — Helvetia Werkstoffe, accounts department. How can I help you?

**user** — Hello, this is Marco Rossi from Ticino Industries.  I'm calling about my most recent invoice. I believe I was charged twice for red fabric, and it was about a hundred francs.  I'd like to get a credit for that.

**agent** — I can help you with a credit request,  but first I need to confirm your identity.  Could you please provide your email address?

**user** — My email is marco.rossi@ticino-ind.ch.

> called `check_factor`

**agent** — Thank you.  Could you please provide your phone number?

**user** — My phone is 091 604 77 31.

> called `check_factor`

**agent** — Thank you.   Could you please provide your date of birth?

**user** — I was born on the 2nd of November 1981.Is there anything else you need to confirm my identity,  or can we proceed with the credit request?

> called `check_factor`

> called `verify_identity`

**agent** — Thank you for providing those details.  Let me now pull up your account information to look into your recent invoice and the credit request.

> called `get_account_context`

**agent** — I can see your most recent invoice is INV-2026-0020 from the twenty-sixth of July.   You mentioned you were charged twice for red fabric and would like a credit of about a hundred francs.   

To process this credit request,  I need to know which specific charge this relates to.   Could you confirm if this is regarding invoice INV-2026-0020,  or a different one?

**user** — Yes, that's the one.  It's regarding invoice INV-2026-0020.

> called `request_credit`

**agent** — I've requested a credit of one hundred francs against invoice INV-2026-0020.   A colleague will review this request and follow up with you.   

Is there anything else I can help you with today?
