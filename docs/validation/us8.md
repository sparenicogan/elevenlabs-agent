# US8 — Address discrepancy raised with the caller

**PASSED** — 3 of 3 checks. Run 2026-09-04 18:50 UTC against the deployed agent.

## Checks

| | Check | Why it matters |
|---|---|---|
| PASS | the discrepancy is raised | The caller is asked whether they moved or it is a typo. |
| PASS | the address is read out rather than withheld | Asking whether it is a typo without saying what it is asks someone to confirm what they cannot see. |
| PASS | no address is changed on the call | A person corrects it. |

## Tools called

```
 1. check_factor
 2. check_factor
 3. check_factor
 4. verify_identity
 5. get_account_context
 6. match_payment
 7. propose_allocation
 8. create_escalation
```

## Transcript

**agent** — Helvetia Werkstoffe, accounts department. How can I help you?

**user** — Hello, my name is Klaus Mueller from Alpina Tech. I am calling about an overdue invoice.  We received a reminder, but we already paid four thousand two hundred francs on the 27th of July.

**agent** — An overdue invoice you've already paid — I can look into that.  First I need to confirm your identity.
 
What is your email address?

**user** — My email address is klaus.mueller@alpina-tech.ch.

> called `check_factor`

**agent** — Thank you.  What is your phone number?

**user** — My phone number is 044 501 22 18.

> called `check_factor`

**agent** — Thank you.   What is your date of birth?

**user** — My date of birth is the 12th of March 1974.

> called `check_factor`

> called `verify_identity`

> called `get_account_context`

**agent** — I can see the invoice from the twentieth of June,  INV twenty twenty-six, zero zero one three,  showing overdue with no payment against it.
 
You mentioned you paid four thousand two hundred francs on the twenty-seventh of July.   Is that the exact amount and date you transferred?   It is fine to check your banking app — I will wait.

**user** — Yes, that is correct.  Four thousand two hundred francs on the 27th of July.

> called `match_payment`

> called `propose_allocation`

**agent** — A payment matching those details was found and appears to cover the invoice.   A person will confirm it within twenty-four hours,  and you need do nothing else.
 
The payer address on the payment is recorded as Hauptstrasse 45,  8001 Zurich.   Did you move, or is that a typo?

**user** — The company has not moved.

> called `create_escalation`

**agent** — I have noted that the address needs correction.   A colleague will review the payment allocation and correct the address details.   They will call you back if we get cut off.
 
Is there anything else I can help you with today?

**user** — No, that will be all.  Thank you for your help.

**agent** — You are welcome.   A colleague will confirm the payment and correct the address within twenty-four hours.   Have a good evening.
