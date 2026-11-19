# Recorded employer evidence

`routing_audit.json.gz` contains the frozen 120-listing audit sample and the
119 public employer responses recorded on October 30, 2026. One Tesla response
failed to download; replay preserves that failure instead of fetching it live.

Each case includes the normalized feed listing, employer endpoint, response
text and SHA-256, reference label, and the evidence supporting that label.
The original random sample used seed `20261030` from 4,543 filtered listings.
`bach` means **no mandatory graduate degree**, not a guarantee that any
bachelor's holder meets every requirement. One ambiguous reference (`unclear`)
is explicitly excluded from scoring.

Labels came from a single assistant review of employer evidence, without
independent human adjudication. The sample was subsequently used to develop
the parser. It is now a development regression benchmark, not an independent
accuracy estimate. Do not change reference labels just to make tests pass.
Any label correction needs the employer evidence and an explanation in review.

Run `python scripts/replay_routing_audit.py` from the repository root.
Replay performs no network requests and exits unsuccessfully for any mismatch.
The regular unit test suite runs the same replay automatically.

The other JSON fixtures preserve the previously reported Meta and Toyota
examples. The classifier has no company-specific degree exceptions.
