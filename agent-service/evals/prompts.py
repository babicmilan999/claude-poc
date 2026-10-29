"""The groundedness judge prompt (alias `judge`). Versioned with the code; the version
goes into the eval run's metadata."""

GROUNDEDNESS_VERSION = "groundedness-judge-v1"

GROUNDEDNESS_SYSTEM = """\
You grade an answer a bank's in-app assistant sent to a customer.

Sources are the only facts the assistant had: tool results about the customer's account
and help-article passages. Split the answer into its factual claims (about the account,
about what happened, about how bank features or policies work). Politeness, empathy and
offers to help are not claims.

For each claim decide whether a source backs it. Return:
- claims: how many factual claims the answer makes
- supported: how many of them a source backs
- unsupported_claims: each claim no source backs, quoted briefly
- reason: one short sentence
"""

GROUNDEDNESS_USER = """\
<sources>
{sources}
</sources>

<answer>
{answer}
</answer>
"""
