#!/usr/bin/env python3
"""One-off verification spike for Phase 3's flagged risk: does a full
~3-hour meeting transcript (the plan's advertised max meeting duration)
actually fit through the real Gemini pipeline -- facts extraction + all 3
document generators, including the refine pass -- without truncation or
failure? Not a pytest test: this makes real Gemini API calls (real quota
cost), so it's a script run deliberately, not part of the automated suite.

Usage:
    python scripts/spike_long_transcript.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.docgen import engine as docgen_engine  # noqa: E402
from app.docgen import registry  # noqa: E402

_SPEAKERS = ["Priya Shah", "Rahul Mehta"]

_TEMPLATES = [
    "Let's walk through how a new customer request actually moves through our system today.",
    "So the request comes in through the web form, and it lands in the intake queue first.",
    "Right, and someone on the sales team has to manually check whether the customer is already in our CRM.",
    "That manual check is actually one of our biggest bottlenecks right now, it takes almost a day sometimes.",
    "Once it's validated, it goes to the account manager for approval before we even start scoping the work.",
    "And if the account manager rejects it, what happens then? Does it go back to the customer directly?",
    "No, it goes back to sales first, and they have to explain the rejection reason to the customer themselves.",
    "That seems like it could be automated with a simple email template instead of a manual conversation.",
    "Agreed, but there's also the pricing approval step which still needs a human because of custom discounts.",
    "Let's talk about the discount approval process specifically, since that seems to be where things slow down most.",
    "Discounts above fifteen percent need director sign-off, and the director is often traveling or in meetings.",
    "Could we set up a delegate approver for when the director is unavailable for more than a day?",
    "That's a good idea, we should add that as an action item for the process redesign.",
    "Moving on, once pricing is approved, the request goes to the delivery team for actual implementation planning.",
    "The delivery team then breaks it into tasks and assigns them across the engineering pod for that quarter.",
    "How long does that planning step usually take from approval to a committed delivery date?",
    "Usually about three to five business days, depending on how busy the pod is with existing commitments.",
    "And what about when a customer wants to change scope mid-way through delivery, how is that handled?",
    "Right now it's pretty ad hoc, someone just raises it in a shared channel and we figure it out reactively.",
    "That's a risk area, we should probably have a formal change request process for that instead.",
    "Let's also discuss the handoff between delivery and support once the work is actually completed.",
    "Support gets a summary document, but it's often incomplete because delivery is already moving to the next project.",
    "We should standardize what fields that handoff document needs to include before it's considered complete.",
    "Good point, I'll note that as a requirement for the new process documentation we're drafting.",
    "What about customer communication throughout all of this, how often do they hear from us?",
    "Only at the start and the end right now, which customers have told us feels like a black box.",
    "We could add a weekly automated status update once a request is in the delivery phase.",
    "That would definitely help with customer satisfaction scores, which have been slipping a bit lately.",
    "Let's also touch on the escalation path when something goes wrong during delivery.",
    "Escalations currently go straight to the delivery lead, bypassing the account manager entirely.",
    "That seems like a gap, the account manager should probably be looped in immediately as well.",
    "Agreed, I'll add that to the list of process gaps we're documenting for this review.",
    "Let's summarize the key pain points so far: manual CRM checks, slow discount approvals, and unclear handoffs.",
    "And the lack of proactive customer communication during the delivery phase itself.",
    "Right, those four things together probably account for most of the friction customers experience.",
    "What would be a realistic first step to start fixing the discount approval bottleneck specifically?",
    "I think a delegate-approver rule is the fastest win, we could implement that within a couple of weeks.",
    "Let's make that the first action item, owned by you, with a target date of two weeks from today.",
    "Sounds good, I'll draft the delegate-approval policy and circulate it for feedback by Thursday.",
    "For the CRM check automation, who would own building that since it touches both sales and engineering?",
    "I think engineering should own the technical build, but sales should define the validation rules.",
    "Let's assign that as a joint action item between the two teams, with a rough estimate next week.",
    "And for the customer status updates, is that something marketing or delivery should own?",
    "Delivery should own the content since they know the project status, but marketing can help with the template.",
    "Okay, let's capture that as well and move on to any other open questions before we wrap up.",
    "One more thing, do we have any data on how often change requests actually happen mid-delivery?",
    "Not formally tracked yet, but anecdotally it feels like at least a third of projects have some scope change.",
    "We should start tracking that properly so we can size the change-request process work correctly.",
    "Agreed, I'll ask the PMO to add a simple tracking field for that starting next sprint.",
    "I think that covers everything we wanted to get through today, thank you both for the detailed walkthrough.",
]


def _build_transcript_text(target_seconds: float) -> str:
    lines = ["Client Requirement Discussion", ""]
    t = 0.0
    i = 0
    while t < target_seconds:
        speaker = _SPEAKERS[i % 2]
        text = _TEMPLATES[i % len(_TEMPLATES)]
        hh = int(t // 3600)
        mm = int((t % 3600) // 60)
        ss = int(t % 60)
        lines.append(f"[{hh:02d}:{mm:02d}:{ss:02d}] {speaker}: {text}")
        t += 4.0  # ~4s per utterance, a reasonable conversational pace
        i += 1
    return "\n".join(lines)


def main() -> None:
    target_seconds = 3 * 60 * 60  # 3 hours, the plan's advertised max
    transcript_text = _build_transcript_text(target_seconds)
    word_count = len(transcript_text.split())
    print(f"Synthetic transcript: {word_count} words, ~{len(transcript_text)} characters")

    print("\n--- extract_meeting_facts ---")
    start = time.monotonic()
    try:
        facts = docgen_engine.extract_meeting_facts(transcript_text)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the spike
        print(f"FAILED after {time.monotonic() - start:.1f}s: {exc!r}")
        return
    print(f"OK in {time.monotonic() - start:.1f}s")

    for group_key, group in registry.GROUPS.items():
        print(f"\n--- generate group: {group_key} ---")
        start = time.monotonic()
        try:
            result = group.generator(
                "Client Requirement Discussion", "5 September 2026", list(_SPEAKERS), facts, transcript_text
            )
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED after {time.monotonic() - start:.1f}s: {exc!r}")
            continue
        elapsed = time.monotonic() - start
        if result is None:
            print(f"OK in {elapsed:.1f}s -- nothing to generate (no supporting material)")
            continue
        for produced_key, content in result.items():
            print(f"OK in {elapsed:.1f}s -- {produced_key}: {len(content)} characters")


if __name__ == "__main__":
    main()
