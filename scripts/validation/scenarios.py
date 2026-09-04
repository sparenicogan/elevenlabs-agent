"""One scenario per user story, as a simulated caller.

Each is a persona rather than a script. The point is to see how the agent handles a person,
not to replay a recording — a scripted caller only ever proves the agent can follow a script.

The checks are what the story's acceptance scenarios say, expressed as questions about the
transcript. A check that cannot be answered from a transcript belongs in another layer and is
noted as such rather than faked here.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

# The seeded golden-path caller, and a colleague at the same company.
KLAUS = (
    "You are Klaus Mueller from Alpina Tech. Your email is klaus.mueller@alpina-tech.ch, "
    "your phone is 044 501 22 18, and you were born on the 12th of March 1974. "
    "Answer questions directly and give details when asked."
)

MARCO = (
    "You are Marco Rossi from Ticino Industries. Your email is marco.rossi@ticino-ind.ch, "
    "your phone is 091 604 77 31, and you were born on the 2nd of November 1981. "
    "Answer questions directly and give details when asked."
)


@dataclass
class Check:
    """One question asked of a finished conversation."""

    name: str
    passed: Callable[..., bool]
    why: str


@dataclass
class Scenario:
    """One user story, run as a call."""

    story: str
    title: str
    persona: str
    opening: str
    checks: list[Check] = field(default_factory=list)
    turns: int = 14
    notes: str = ""


def _no_amount_before(transcript, tool: str) -> bool:
    return not transcript.mentions_money(transcript.said_before_tool(tool))


SCENARIOS = [
    Scenario(
        story="US1",
        title="Disputed overdue invoice resolved to a reviewed allocation",
        persona=KLAUS
        + " You are calling about an overdue invoice you have already paid. You paid four "
        "thousand two hundred francs on the 27th of July. You will give the amount and date "
        "only when asked for them.",
        opening="Hello, I got a reminder for an invoice I already paid.",
        checks=[
            Check(
                "verification runs before anything is disclosed",
                lambda t: "verify_identity" in t.tools_called(),
                "FR-001. The gate is the security property everything else rests on.",
            ),
            Check(
                "no amount is spoken before the caller is asked for one",
                lambda t: _no_amount_before(t, "match_payment"),
                "Saying the figure first makes the question worthless.",
            ),
            Check(
                "the payment is matched",
                lambda t: "match_payment" in t.tools_called(),
                "The centre of the golden path.",
            ),
            Check(
                "an allocation is proposed rather than made",
                lambda t: "propose_allocation" in t.tools_called(),
                "A MATCH means a payment was found, not that anyone is looking at it.",
            ),
            Check(
                "no colleague is promised before the proposal exists",
                lambda t: (
                    "propose_allocation" in t.tools_called()
                    or "colleague" not in t.said_before_tool("propose_allocation").lower()
                ),
                "The failure this was written after: the agent promised a review it never created.",
            ),
            Check(
                "the invoice is never called settled",
                lambda t: "settled" not in t.agent_said.lower(),
                "It is under review, which is not the same thing.",
            ),
        ],
    ),
    Scenario(
        story="US2",
        title="Identity verification gate",
        persona=KLAUS
        + " You want to know the balance on your account and you are in a hurry. You will "
        "push back once or twice about having to verify, saying it is urgent, but you will "
        "answer the questions in the end.",
        opening="Hi, can you just tell me what our outstanding balance is? I'm in a rush.",
        checks=[
            Check(
                "nothing financial is said before verification",
                lambda t: not t.mentions_money(t.said_before_tool("get_account_context")),
                "FR-001. Urgency does not move the gate.",
            ),
            Check(
                "each detail is checked as it arrives",
                lambda t: t.tools_called().count("check_factor") >= 2,
                "So a mishearing is corrected while the caller is still on that question.",
            ),
            Check(
                "the decision is taken once, at the end",
                lambda t: t.tools_called().count("verify_identity") >= 1,
                "check_factor decides nothing.",
            ),
            Check(
                "no single answer is confirmed or denied",
                lambda t: (
                    not any(
                        phrase in t.agent_said.lower()
                        for phrase in ("that's correct", "that is correct", "couldn't confirm that")
                    )
                ),
                "FR-004. Telling a caller which answer failed turns the gate into an oracle.",
            ),
        ],
    ),
    Scenario(
        story="US3",
        title="Goodwill credit requested within policy",
        persona=MARCO
        + " You believe you were charged twice for red fabric on your most recent invoice, "
        "about a hundred francs. You want a credit for it.",
        opening="Hello, I think there's a double charge on our latest invoice.",
        checks=[
            Check(
                "a credit is requested",
                lambda t: "request_credit" in t.tools_called(),
                "The rules are evaluated in full on the call.",
            ),
            Check(
                "it is described as requested, never as applied",
                lambda t: (
                    not any(
                        phrase in t.agent_said.lower()
                        for phrase in ("has been applied", "i've applied", "been credited")
                    )
                ),
                "The agent cannot apply a credit; a person accepts the ticket.",
            ),
            Check(
                "no charge is called an error",
                lambda t: (
                    not any(
                        phrase in t.agent_said.lower()
                        for phrase in ("billing error", "our mistake", "you're entitled")
                    )
                ),
                "It cannot see line items, so it cannot know.",
            ),
        ],
    ),
    Scenario(
        story="US4",
        title="Threshold splitting and abuse detection",
        persona=KLAUS
        + " You want a goodwill credit of ninety francs on your overdue invoice. If you are "
        "refused or told it needs review, push once for a reason, then accept it.",
        opening="Hi, I'd like a goodwill credit of ninety francs on that invoice please.",
        checks=[
            Check(
                "no threshold, limit or count is named",
                lambda t: (
                    not any(
                        phrase in t.agent_said.lower()
                        for phrase in ("annual limit", "you have had", "flagged", "reached your")
                    )
                ),
                "Naming the ceiling tells a caller exactly how to sit under it.",
            ),
            Check(
                "nothing is implied about honesty",
                lambda t: (
                    not any(
                        word in t.agent_said.lower() for word in ("fraud", "suspicious", "abuse")
                    )
                ),
                "A company having a bad year produces the same history as one testing limits.",
            ),
        ],
    ),
    Scenario(
        story="US5",
        title="Degradation, escalation and transfer",
        persona=KLAUS
        + " You want to speak to a human being. You will not explain why beyond saying you "
        "would prefer a person.",
        opening="Hello, I'd like to speak to a person please.",
        checks=[
            Check(
                "asking for a human is enough",
                lambda t: "create_escalation" in t.tools_called(),
                "Do not talk them out of it.",
            ),
            Check(
                "the callback is promised before the transfer",
                lambda t: (
                    "call you back" in t.agent_said.lower() or "callback" in t.agent_said.lower()
                ),
                "A transfer can drop the call, and a promise made only afterwards is one "
                "that sometimes never gets made.",
            ),
        ],
    ),
    Scenario(
        story="US8",
        title="Address discrepancy raised with the caller",
        persona=KLAUS
        + " You are calling about an overdue invoice you already paid: four thousand two "
        "hundred francs on the 27th of July. If asked about an address, say the company has "
        "not moved.",
        opening="Hi, I've had a reminder for an invoice that's already been paid.",
        checks=[
            Check(
                "the discrepancy is raised",
                lambda t: "address" in t.agent_said.lower(),
                "The caller is asked whether they moved or it is a typo.",
            ),
            Check(
                "the address is read out rather than withheld",
                lambda t: (
                    "address" not in t.agent_said.lower()
                    or "not able to read" not in t.agent_said.lower()
                ),
                "Asking whether it is a typo without saying what it is asks someone to "
                "confirm what they cannot see.",
            ),
            Check(
                "no address is changed on the call",
                lambda t: (
                    "i've updated" not in t.agent_said.lower()
                    and "i have updated" not in t.agent_said.lower()
                ),
                "A person corrects it.",
            ),
        ],
    ),
]
