# US5 — Degradation, escalation and transfer

**FAILED** — 1 of 2 checks. Run 2026-09-04 18:54 UTC against the deployed agent.

## Checks

| | Check | Why it matters |
|---|---|---|
| PASS | asking for a human is enough | Do not talk them out of it. |
| FAIL | the callback is promised before the transfer | A transfer can drop the call, and a promise made only afterwards is one that sometimes never gets made. |

## Tools called

```
 1. create_escalation
 2. transfer_to_number
```

## Transcript

**agent** — Helvetia Werkstoffe, accounts department. How can I help you?

**user** — Hello, my name is Klaus Mueller from Alpina Tech.  I would like to speak to a human being, please.  I would prefer to speak with a person.

**agent** — I understand you'd prefer to speak with a person.  Before I transfer you, could you tell me briefly what you're calling about so I can pass that along to my colleague?

**user** — I would prefer to speak with a person.

**agent** — Of course, I can arrange that for you.  I've noted your request to speak with a colleague,  and they will have the details of our conversation so you won't need to repeat yourself.  I'll transfer you now, and please stay on the line.

**user** — ...

**agent** — Are you still there?  I am ready to transfer you to a colleague now.

**user** — Yes, I am still here.  Please transfer me.

> called `create_escalation`

> called `transfer_to_number`
