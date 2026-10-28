"""Human review via `interrupt()` and the case lifecycle around it: accept, correct,
reject-with-hint resume (same thread), reviews of escalations, Investigate,
Re-investigate (superseded), retries and failures."""

import uuid

import pytest

from core.banking import BankingClient
from core.enums import CaseStatus, InvestigationStatus, InvestigationTrigger, IssueType, RootCause, Verdict
from core.jobs import ClaimedJob
from investigation.schemas import FeedbackRequest
from investigation.store import Conflict, NotFound
from tests.banking_mock import ALICE
from tests.fakes import FakeLLMProvider, text, tool_call
from tests.investigation.harness import Harness, diagnosis, enough, make_harness, not_enough, triaged


@pytest.fixture
def llm() -> FakeLLMProvider:
    return FakeLLMProvider()


def _script_card_decline(llm: FakeLLMProvider, root_cause: RootCause = RootCause.CARD_FROZEN_BY_CUSTOMER) -> None:
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_cards", {}),
        text("Card frozen (ev-1)."),
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, root_cause, "ev-1"),
        text("The card is frozen (ev-1)."),
    )


async def _diagnosed(h: Harness) -> tuple[uuid.UUID, uuid.UUID]:
    case_id = h.store.add_case(customer_id=ALICE, complaint="my card keeps getting declined")
    _script_card_decline(h.llm)
    await h.service.run_pending()
    inv = h.only_investigation(case_id)
    assert inv.status == InvestigationStatus.AWAITING_REVIEW
    return case_id, inv.id


async def _escalated(h: Harness) -> tuple[uuid.UUID, uuid.UUID]:
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    h.llm.script("triage", triaged(IssueType.CARD_DECLINE))
    h.llm.script("reasoning", tool_call("get_cards", {}), text("Unclear."), not_enough())
    await h.service.run_pending()
    assert h.store.cases[case_id].status == CaseStatus.ESCALATED
    return case_id, h.only_investigation(case_id).id


async def _state(h: Harness, investigation_id: uuid.UUID) -> dict:
    snapshot = await h.service.graph.aget_state({"configurable": {"thread_id": str(investigation_id)}})
    return {"values": snapshot.values, "next": snapshot.next, "interrupts": snapshot.interrupts}


async def test_accept_completes_resolves_and_finalizes(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id, inv_id = await _diagnosed(h)
    state = await _state(h, inv_id)
    assert state["next"] == ("review",) and state["interrupts"], "paused at interrupt()"

    result = await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.ACCEPTED, notes="spot on"))
    # The response already shows the new statuses.
    assert result.investigation.status == InvestigationStatus.COMPLETED
    assert result.investigation.verdict == Verdict.ACCEPTED
    assert result.investigation.review is not None and result.investigation.review.notes == "spot on"
    assert h.store.cases[case_id].status == CaseStatus.RESOLVED
    assert not result.requeued

    await h.service.finalize(inv_id, result.resume_payload)
    state = await _state(h, inv_id)
    assert state["next"] == () and state["values"]["outcome"] == "finalized"
    assert state["values"]["final"]["root_cause"] == "card_frozen_by_customer"
    assert h.nodes(inv_id)[-1] == "finalize"
    types = h.event_types(inv_id)
    assert types[types.index("review_recorded") + 1] == "status_changed"
    assert (str(inv_id), "diagnosis_accepted", 1.0, "accepted") in h.scores

    # Finalizing twice is harmless.
    await h.service.finalize(inv_id, result.resume_payload)
    assert h.nodes(inv_id).count("finalize") == 1


async def test_correct_records_the_root_cause_and_resolves(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id, inv_id = await _diagnosed(h)
    feedback = FeedbackRequest(
        verdict=Verdict.CORRECTED,
        corrected_issue_type=IssueType.CARD_DECLINE,
        corrected_root_cause=RootCause.CARD_EXPIRED,
    )
    result = await h.service.submit_feedback(inv_id, feedback)
    assert result.investigation.status == InvestigationStatus.COMPLETED
    assert result.investigation.review is not None and result.investigation.diagnosis is not None
    assert result.investigation.review.corrected_root_cause == RootCause.CARD_EXPIRED
    assert result.investigation.diagnosis.root_cause == RootCause.CARD_FROZEN_BY_CUSTOMER, "the diagnosis is kept"
    assert h.store.cases[case_id].status == CaseStatus.RESOLVED

    await h.service.finalize(inv_id, result.resume_payload)
    final = (await _state(h, inv_id))["values"]["final"]
    assert final["root_cause"] == "card_expired" and final["verdict"] == "corrected"
    assert (str(inv_id), "diagnosis_accepted", 0.0, "corrected") in h.scores


async def test_reject_with_hint_resumes_the_same_thread(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking, investigation_max_steps=2)
    case_id, inv_id = await _diagnosed(h)  # uses 2 of the 2 steps
    first_diagnosis = h.store.investigations[inv_id].diagnosis

    result = await h.service.submit_feedback(
        inv_id, FeedbackRequest(verdict=Verdict.REJECTED, hint="Check the fraud flags, not the freeze.")
    )
    assert result.requeued
    assert result.investigation.status == InvestigationStatus.QUEUED
    assert result.investigation.verdict == Verdict.REJECTED
    assert h.store.cases[case_id].status == CaseStatus.INVESTIGATING

    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_fraud_flags", {}),
        text("No fraud flags (ev-2); the freeze is the customer's (ev-1)."),
        enough("ev-1", "ev-2"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-1", "ev-2", confidence=0.95),
        text("Checked the fraud flags (ev-2)."),
    )
    assert await h.service.run_pending() == 1

    inv = h.store.investigations[inv_id]
    assert len([i for i in h.store.investigations.values() if i.case_id == case_id]) == 1, "same investigation"
    assert inv.status == InvestigationStatus.AWAITING_REVIEW and inv.attempts == 2
    assert inv.diagnosis["id"] != first_diagnosis["id"]
    assert inv.diagnosis["evidence_ids"] == ["ev-1", "ev-2"]
    assert inv.review is None, "the rejected review survives in the event log only"
    assert inv.steps_used == 4, "a rejection gives the run a fresh step budget"
    assert h.store.cases[case_id].status == CaseStatus.AWAITING_REVIEW

    # Same thread: intake did not run again, the hint reached triage and the investigator.
    assert h.nodes(inv_id).count("intake") == 1
    assert "Check the fraud flags" in llm.fake("triage").calls[1].text
    investigator_prompt = llm.fake("reasoning").calls[5].text
    assert "rejected the previous conclusion" in investigator_prompt and "Check the fraud flags" in investigator_prompt
    assert "ev-1" in investigator_prompt, "earlier evidence is kept in the thread"
    review_events = [e for e in h.events(inv_id) if e.type == "review_recorded"]
    assert review_events[0].payload["review"]["hint"] == "Check the fraud flags, not the freeze."


async def test_rejected_twice_then_accepted(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id, inv_id = await _diagnosed(h)
    for hint in ("look again", "and again"):
        await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.REJECTED, hint=hint))
        _script_card_decline(llm)
        await h.service.run_pending()
        assert h.store.investigations[inv_id].status == InvestigationStatus.AWAITING_REVIEW
    result = await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.ACCEPTED))
    await h.service.finalize(inv_id, result.resume_payload)
    assert h.store.cases[case_id].status == CaseStatus.RESOLVED
    assert (await _state(h, inv_id))["values"]["outcome"] == "finalized"


async def test_rejecting_an_escalation_goes_back_to_investigating(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking, investigation_max_passes=1)
    case_id, inv_id = await _escalated(h)
    result = await h.service.submit_feedback(
        inv_id, FeedbackRequest(verdict=Verdict.REJECTED, hint="Check the controls")
    )
    assert h.store.cases[case_id].status == CaseStatus.INVESTIGATING
    assert result.investigation.escalation is not None

    _script_card_decline(llm)
    await h.service.run_pending()
    inv = h.store.investigations[inv_id]
    assert inv.diagnosis is not None and inv.escalation is None
    assert h.store.cases[case_id].status == CaseStatus.AWAITING_REVIEW


async def test_accepting_an_escalation_means_handled_manually(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking, investigation_max_passes=1)
    case_id, inv_id = await _escalated(h)
    result = await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.ACCEPTED, notes="unfroze it"))
    assert result.investigation.status == InvestigationStatus.COMPLETED
    assert h.store.cases[case_id].status == CaseStatus.RESOLVED
    await h.service.finalize(inv_id, result.resume_payload)
    values = (await _state(h, inv_id))["values"]
    assert values["outcome"] == "finalized" and values["final"]["had_diagnosis"] is False
    assert not any(name == "diagnosis_accepted" for _, name, _, _ in h.scores)


async def test_feedback_only_while_awaiting_review(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    _, inv_id = await _diagnosed(h)
    await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.ACCEPTED))
    with pytest.raises(Conflict):
        await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.ACCEPTED))
    with pytest.raises(NotFound):
        await h.service.submit_feedback(uuid.uuid4(), FeedbackRequest(verdict=Verdict.ACCEPTED))


def test_feedback_validation() -> None:
    with pytest.raises(ValueError, match="corrected_root_cause"):
        FeedbackRequest(verdict=Verdict.CORRECTED)
    with pytest.raises(ValueError, match="hint"):
        FeedbackRequest(verdict=Verdict.REJECTED)
    with pytest.raises(ValueError, match="hint"):
        FeedbackRequest(verdict=Verdict.REJECTED, hint="   ")


# --- Investigate and Re-investigate --------------------------------------------------------


async def test_investigate_a_parked_case_runs_manually_and_is_not_parked(
    llm: FakeLLMProvider, banking: BankingClient
) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=ALICE, complaint="something odd")
    llm.script("triage", triaged(IssueType.OTHER))
    await h.service.run_pending()
    assert h.store.cases[case_id].status == CaseStatus.NEEDS_MANUAL

    started = await h.service.start_investigation(case_id, reinvestigate=False)
    assert started.created and started.investigation.trigger == InvestigationTrigger.MANUAL
    assert h.store.cases[case_id].status == CaseStatus.INVESTIGATING
    again = await h.service.start_investigation(case_id, reinvestigate=False)
    assert not again.created and again.investigation.id == started.investigation.id, "one active investigation"

    llm.script("triage", triaged(IssueType.OTHER))
    llm.script(
        "reasoning",
        tool_call("get_account_balance", {}),
        text("Nothing failed (ev-1)."),
        enough("ev-1"),
        diagnosis(IssueType.OTHER, RootCause.UNKNOWN, "ev-1", confidence=0.2),
        text("Nothing failed."),
    )
    await h.service.run_pending()
    assert "generic_investigator" in h.nodes(started.investigation.id)
    assert h.store.cases[case_id].status == CaseStatus.AWAITING_REVIEW


async def test_reinvestigate_supersedes_one_awaiting_review(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id, old_id = await _diagnosed(h)
    started = await h.service.start_investigation(case_id, reinvestigate=True)
    assert started.created and started.superseded_id == old_id
    assert h.store.investigations[old_id].status == InvestigationStatus.SUPERSEDED
    assert h.events(old_id)[-1].payload["status"] == "superseded"
    new_id = started.investigation.id
    assert new_id != old_id and started.investigation.status == InvestigationStatus.QUEUED

    with pytest.raises(Conflict):  # queued
        await h.service.start_investigation(case_id, reinvestigate=True)
    with pytest.raises(Conflict):  # the superseded one can no longer be reviewed
        await h.service.submit_feedback(old_id, FeedbackRequest(verdict=Verdict.ACCEPTED))

    _script_card_decline(llm)
    await h.service.run_pending()
    assert h.store.investigations[new_id].status == InvestigationStatus.AWAITING_REVIEW
    assert h.nodes(new_id)[0] == "intake", "a new thread starts from scratch"
    assert h.store.investigations[new_id].evidence[0]["id"] == "ev-1"


async def test_reinvestigate_conflicts(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id, inv_id = await _diagnosed(h)
    await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.ACCEPTED))
    with pytest.raises(Conflict, match="resolved"):
        await h.service.start_investigation(case_id, reinvestigate=True)
    with pytest.raises(Conflict, match="resolved"):
        await h.service.start_investigation(case_id, reinvestigate=False)
    with pytest.raises(NotFound):
        await h.service.start_investigation(uuid.uuid4(), reinvestigate=True)

    running_case = h.store.add_case(customer_id=ALICE, complaint="x")
    job = await h.store.claim_next("w1")
    assert job is not None and job.case_id == running_case
    with pytest.raises(Conflict, match="running"):
        await h.service.start_investigation(running_case, reinvestigate=True)


# --- retries and failures ---------------------------------------------------------------------


async def test_a_failing_run_is_retried_then_fails_to_needs_manual(
    llm: FakeLLMProvider, banking: BankingClient
) -> None:
    h = make_harness(llm, banking, investigation_max_attempts=2)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    # Nothing scripted: the fake LLM raises, as an unreachable model would.
    assert await h.service.run_pending() == 2
    inv = h.only_investigation(case_id)
    assert inv.status == InvestigationStatus.FAILED and inv.attempts == 2
    assert "script exhausted" in inv.error
    assert h.store.cases[case_id].status == CaseStatus.NEEDS_MANUAL
    assert h.events(inv.id)[-1].payload["status"] == "failed"


async def test_a_retry_resumes_from_the_last_checkpoint(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script("reasoning", tool_call("get_cards", {}), text("Frozen (ev-1)."))  # then the evidence check fails

    job = await h.store.claim_next("w1")
    assert job is not None
    await h.service.handle_job(job)
    inv = h.only_investigation(case_id)
    assert inv.status == InvestigationStatus.QUEUED and inv.error

    llm.script(
        "reasoning",
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-1"),
        text("Frozen."),
    )
    await h.service.run_pending()
    assert inv.status == InvestigationStatus.AWAITING_REVIEW
    assert len(llm.fake("triage").calls) == 1, "triage and the investigator were not repeated"
    assert h.nodes(inv.id).count("card_decline_investigator") == 1


async def test_a_retry_after_reaching_review_does_not_reapply_the_review(
    llm: FakeLLMProvider, banking: BankingClient
) -> None:
    """The run resumed with a rejection, reached the next review, then the worker died
    before storing it: the retry stores the pause without rejecting the new diagnosis."""
    h = make_harness(llm, banking)
    case_id, inv_id = await _diagnosed(h)
    await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.REJECTED, hint="again"))
    job = await h.store.claim_next("w1")
    assert job is not None and job.resume_payload is not None
    _script_card_decline(llm)
    config = h.service._config(inv_id, h.service._emitter(job, inv_id))
    from langgraph.types import Command

    await h.service.graph.ainvoke(Command(resume=job.resume_payload), config)  # ...then the worker dies
    await h.store.release_for_retry(job, "worker died", max_attempts=3)

    await h.service.run_pending()  # no more model output scripted: nothing may run again
    inv = h.store.investigations[inv_id]
    assert inv.status == InvestigationStatus.AWAITING_REVIEW and inv.diagnosis is not None
    assert h.store.cases[case_id].status == CaseStatus.AWAITING_REVIEW


async def test_a_worker_that_lost_its_job_changes_nothing(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    job = await h.store.claim_next("w1")
    assert job is not None
    h.store.investigations[job.investigation_id].worker_id = "w2"  # requeued and claimed by another worker
    _script_card_decline(llm)
    await h.service.handle_job(job)
    inv = h.only_investigation(case_id)
    assert inv.status == InvestigationStatus.RUNNING and inv.worker_id == "w2"
    assert inv.diagnosis is None


async def test_stale_jobs_are_recovered(llm: FakeLLMProvider, banking: BankingClient) -> None:
    from datetime import timedelta

    h = make_harness(llm, banking, investigation_max_attempts=1)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    job = await h.store.claim_next("dead")
    assert isinstance(job, ClaimedJob)
    row = h.store.investigations[job.investigation_id]
    assert row.heartbeat_at is not None
    row.heartbeat_at -= timedelta(hours=1)
    await h.service.recover_stale()
    assert row.status == InvestigationStatus.FAILED
    assert h.store.cases[case_id].status == CaseStatus.NEEDS_MANUAL
