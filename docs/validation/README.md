# Validation

One directory per user story, one JSON per run, named for when it happened. The history
accumulates rather than being overwritten, so a rule that holds today and not tomorrow is
visible.

```bash
AWS_PROFILE=voice-agent-admin uv run python -m scripts.validation.run
AWS_PROFILE=voice-agent-admin uv run python -m scripts.validation.run --story US1 --runs 4
```

Each file carries the verdict, the behaviours that were right and wrong with why each matters,
the metadata needed to tie the result to the code that produced it, and the whole transcript.

## What these prove, and what they do not

They exercise **the prompt**. Nothing else does — unit tests exercise rules, contract tests
exercise handlers with the world stubbed out, integration tests call endpoints directly.

**The simulator does not call the webhooks.** A recorded tool call is the agent deciding to
call it; the result it then reacts to is fabricated by ElevenLabs, and nothing reaches AWS.
Verified by CloudWatch: zero Lambda invocations across a completed run. So `tools_called` is
a record of intent, and every file says so in its metadata.

That makes these good for what the agent says and in what order, and useless for whether the
backend works. The 60 integration tests cover that.

**They are not deterministic.** The same scenario can pass and then fail. A failure is
information about the prompt rather than a regression, and the question it raises is whether
the rule can be moved somewhere it would be enforced.

**They mutate real data.** The fixtures are reseeded before each run.
