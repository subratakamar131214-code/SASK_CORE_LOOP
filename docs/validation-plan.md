# Zero-cost demand validation plan

Before adding features or channels, validate that the free artifact solves a
real, repeated problem. This plan costs nothing and uses only public or
explicitly shared feedback.

## For each niche

1. **Target user** — name one role and context, e.g. "platform engineer debugging
   GitHub Actions failures for a small team."
2. **Pain hypothesis** — one sentence, one observable failure mode.
3. **Free artifact** — the generated CLI plus tests and docs; no signup.
4. **Install command** — one copy-paste path (download the artifact, run the
   command). No gate.
5. **One feedback question** — "Did this identify the cause or the breaking
   change? What was missing?" One question only.
6. **Stop rule** — if no external signal after two releases and four weeks,
   pause the niche and reallocate offline effort to the other seed.

## Signals to count

Use the funnel counters already recorded locally (aggregate, no tracking):

| Stage | Meaning | Source |
| --- | --- | --- |
| `impression` | A human saw the landing page or post | Manual count entered locally |
| `artifact_download` | The free archive was downloaded | Manual count or host stats |
| `feedback_signal` | A real user replied or filed an issue | Sanitized feedback ingestion |
| `paid_intent` | A human asked how to pay or sponsor | Human-recorded, never inferred |

Do not scrape, automate outreach, mass-file issues, or buy traffic. Manual
distribution only, through channels that permit it.

## Two-week loop

1. Publish one release per niche with sanitized notes and checksums.
2. Share once in a relevant, opt-in community where self-promotion is allowed.
3. Answer feedback personally; record only sanitized, aggregate counts.
4. At day 14, compare `feedback_signal` and `artifact_download` per niche.
5. Keep, revise, or stop. Write the decision in a reviewed doc, not in state.

## Success thresholds (initial, adjustable)

- **Continue**: at least 3 independent feedback signals for one niche and at
  least 1 concrete missing-capability request.
- **Revise**: downloads occur but no feedback; improve the one feedback question
  and the first-run experience.
- **Stop**: no downloads or signals after two releases and four weeks.

## Guardrails

- No paid ads, paid APIs, paid runners, or paid tooling.
- No PII in public notes; raw feedback stays local and gitignored.
- Simulated revenue is never presented as real revenue.
- A human confirms any real payment or sponsorship before it is recorded.

This plan exists so the loop earns the right to build more, rather than building
on speculation.
