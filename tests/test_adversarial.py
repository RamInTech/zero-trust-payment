"""Phase 6 completion test — Adversarial Test Suite.

The suite in `zerotrust/adversary.py` produces a results table. These tests are
what make that table trustworthy: they fail the build if any attack succeeds,
if any attack silently disappears from the runner, or if the suite stops
covering one of RAZORPAY.md's six required cases.
"""

from __future__ import annotations

import random

import pytest

from zerotrust.adversary import AdversarialSuite, AttackOutcome, run_suite

#: RAZORPAY.md Phase 6's completion test, mapped to the attack that covers it.
#: If an attack is renamed or dropped, this fails rather than passing quietly.
REQUIRED_COVERAGE = {
    "replay with original payload -> replayed, not re-executed":
        "replay_completed_transaction",
    "replay with a tampered amount -> rejected":
        "replay_with_tampered_amount",
    "over the per-transaction cap -> denied":
        "exceed_per_transaction_cap",
    "disallowed item -> denied":
        "purchase_disallowed_item",
    "burst over the velocity limit -> only the allowed count succeeds":
        "velocity_burst",
    "two valid purchases racing -> both succeed independently":
        "concurrent_distinct_purchases",
    # Beyond Phase 6's required six: added with the Section 8 stretch goals.
    "repeated denials -> the agent is throttled":
        "grind_against_the_policy_engine",
}


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """One run for the whole module.

    The attacks are stateful -- several of them charge -- so re-running per
    test would be slow and would change what the velocity rows mean.
    """
    return run_suite(str(tmp_path_factory.mktemp("adversarial")))


def test_every_attack_is_defended(report):
    breached = [o for o in report.outcomes if not o.defended]
    assert not breached, "\n".join(
        f"{o.name}: {o.attack}\n  expected: {o.expected}\n  got: {o.evidence}"
        for o in breached
    )


@pytest.mark.parametrize("index", range(len(AdversarialSuite.ATTACK_METHODS)))
def test_attack_holds(report, index):
    """One test per attack, so a failure names the attack that broke."""
    outcome = report.outcomes[index]
    assert outcome.defended, (
        f"{outcome.name} BREACHED\n"
        f"  attack:   {outcome.attack}\n"
        f"  expected: {outcome.expected}\n"
        f"  evidence: {outcome.evidence}\n"
        f"  charges:  {outcome.money_actions} (intended {outcome.intended_actions})"
    )


def test_no_unintended_charges(report):
    """The single number that matters most: money moved that shouldn't have."""
    assert report.unintended_charges == 0, (
        f"{report.unintended_charges} unintended charge(s): "
        + ", ".join(f"{o.name} charged {o.money_actions}, "
                    f"intended {o.intended_actions}"
                    for o in report.outcomes
                    if o.money_actions != o.intended_actions)
    )


def test_nothing_breached(report):
    assert report.breached == 0
    assert report.defended == len(report.outcomes)


def test_every_registered_attack_ran(report):
    """An attack cannot be quietly dropped from the runner."""
    ran = {o.name for o in report.outcomes}
    assert len(report.outcomes) == len(AdversarialSuite.ATTACK_METHODS)
    assert len(ran) == len(report.outcomes), "two attacks share a name"


@pytest.mark.parametrize("requirement,attack_name", REQUIRED_COVERAGE.items())
def test_required_completion_case_is_covered(report, requirement, attack_name):
    """Each of Phase 6's six required cases has a defending attack."""
    matching = [o for o in report.outcomes if o.name == attack_name]
    assert matching, (
        f"no attack named '{attack_name}' -- RAZORPAY.md Phase 6 requires "
        f"coverage of: {requirement}"
    )
    assert matching[0].defended


def test_the_suite_discriminates_rather_than_denying_everything(report):
    """A system that refuses everything is broken, not secure.

    At least one attack must be a legitimate action that SUCCEEDS, or the
    other eleven rows prove nothing about the system's judgement.
    """
    legitimate = [o for o in report.outcomes if o.intended_actions > 0]
    assert legitimate, (
        "every attack expects zero charges, so nothing in this suite proves "
        "the system can still say yes"
    )
    assert sum(o.money_actions for o in legitimate) == sum(
        o.intended_actions for o in legitimate)


def test_outcomes_carry_usable_evidence(report):
    """Every row must explain itself, for the generated results table."""
    for o in report.outcomes:
        assert isinstance(o, AttackOutcome)
        assert o.attack and o.expected and o.defence and o.evidence, (
            f"{o.name} has an empty field; the results table would be blank"
        )
        assert o.status in ("DEFENDED", "BREACHED")


def test_attacks_are_order_independent(tmp_path):
    """Each attack must stand alone.

    Attacks that charge consume velocity budget. When they shared one agent, a
    later attack inherited a spent budget and reported a breach that did not
    exist (JOURNAL.md Entry 7). Shuffling the order proves that is fixed --
    and would catch any future attack that reintroduces the coupling.
    """
    for seed in range(3):
        order = AdversarialSuite.ATTACK_METHODS[:]
        random.Random(seed).shuffle(order)

        workdir = tmp_path / f"shuffle_{seed}"
        workdir.mkdir()
        suite = AdversarialSuite(str(workdir))

        outcomes = [getattr(suite, name)() for name in order]
        breached = [o.name for o in outcomes if not o.defended]
        assert not breached, (
            f"order-dependent attack(s) in shuffle {seed}: {breached}"
        )


def test_report_renders_markdown_and_json(report):
    markdown = report.to_markdown()
    assert "GENERATED FILE" in markdown
    assert "do not edit by hand" in markdown
    for outcome in report.outcomes:
        assert outcome.name in markdown

    import json
    data = json.loads(report.to_json())
    assert data["totals"]["attacks"] == len(report.outcomes)
    assert data["totals"]["breached"] == 0
    assert data["totals"]["unintended_charges"] == 0
    assert len(data["attacks"]) == len(report.outcomes)
