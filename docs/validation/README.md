# Validation

Each user story run against the deployed agent, as a simulated caller with a persona rather
than a script. Regenerate with:

```bash
AWS_PROFILE=voice-agent-admin uv run python -m scripts.validation.run
AWS_PROFILE=voice-agent-admin uv run python -m scripts.validation.run --story US1
```

Every file holds the whole transcript, the tools the agent actually called, and each
acceptance check with a verdict and why it matters. The transcript goes in whole because a
summary of a conversation is an opinion about it.

## What these can and cannot tell you

They exercise the prompt, which nothing else does. The unit tests exercise rules, the contract
tests exercise handlers with the world stubbed out, and the integration tests call endpoints
directly — none of them has ever seen a sentence the agent says.

**They are not deterministic, and that is a finding rather than a defect.** The same scenario
can pass and then fail: a rule the model followed at 14:02 it ignores at 14:05. So a failure
here is information about the prompt, not necessarily a regression, and the question it raises
is whether the rule can be moved somewhere it would be enforced.

That distinction is the whole design. "Nothing financial before VERIFIED" holds because
`require_verified` reads a row and raises; no amount of talking changes it. "Never promise a
review that does not exist" holds because the model usually complies. The first is a control.
The second is a tendency, and this is where you find out which is which.

**They mutate real data.** The agent calls the same endpoints a phone call does, so the
fixtures are reseeded before each scenario.

## Results

| Story | | Checks | |
|---|---|---|---|
| [US1](us1.md) | Disputed overdue invoice resolved to a reviewed allocation | 6/6 | PASSED |
| [US2](us2.md) | Identity verification gate | 4/4 | PASSED |
| [US3](us3.md) | Goodwill credit requested within policy | 3/3 | PASSED |
| [US4](us4.md) | Threshold splitting and abuse detection | 2/2 | PASSED |
| [US5](us5.md) | Degradation, escalation and transfer | 1/2 | FAILED |
| [US8](us8.md) | Address discrepancy raised with the caller | 3/3 | PASSED |
