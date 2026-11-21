import base64
import json
import os
from pathlib import Path


from .models import Observations, Proposal

from .completion import complete


def get_model() -> str:
    model = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-large-4-0").strip()
    if not model.startswith("mistralai/"):
        raise ValueError("Only Mistral models are allowed. Set OPENROUTER_MODEL to a mistralai/ model.")
    return model


def resolve_span(item: dict, frames: dict) -> dict:
    start = frames.get(item["start_frame_id"])
    end = frames.get(item["end_frame_id"])
    if not start or not end or end["timestamp"] < start["timestamp"]:
        raise ValueError("Mistral referenced an invalid evidence range. Please retry the analysis.")
    return {**item, "start_seconds": start["timestamp"], "end_seconds": end["timestamp"],
            "observed_span_seconds": round(end["timestamp"] - start["timestamp"], 3)}


def validate_step_coverage(steps: list[dict], frames: list[dict]) -> list[dict]:
    """Require a chronological partition, so revisits cannot hide intervening screens."""
    lookup = {frame["id"]: frame for frame in frames}
    covered = set()
    ordered = sorted(steps, key=lambda step: resolve_span(step, lookup)["start_seconds"])
    for step in ordered:
        span = resolve_span(step, lookup)
        evidence = {frame["id"] for frame in frames
                    if span["start_seconds"] <= frame["timestamp"] <= span["end_seconds"]}
        if covered & evidence:
            raise ValueError("Screenshot ranges overlap. Split returns to an application into separate chronological steps.")
        covered.update(evidence)
    missing = set(lookup) - covered
    if missing:
        raise ValueError("Screenshots without a description: " + ", ".join(sorted(missing)))
    return ordered


def finalize(proposal: Proposal, steps: list[dict], frames: list[dict]) -> dict:
    lookup = {frame["id"]: frame for frame in frames}
    result = proposal.model_dump()
    result["timeline"] = sorted([resolve_span(s, lookup) for s in steps], key=lambda s: s["start_seconds"])
    for step in result["timeline"]:
        step["evidence_frame_ids"] = [frame["id"] for frame in frames
                                      if step["start_seconds"] <= frame["timestamp"] <= step["end_seconds"]]
    occurrences = sorted([resolve_span(o, lookup) for o in result["workflow"]["occurrences"]], key=lambda o: o["start_seconds"])
    for previous, current in zip(occurrences, occurrences[1:]):
        if current["start_seconds"] < previous["end_seconds"] or current["start_frame_id"] == previous["start_frame_id"]:
            raise ValueError("Mistral returned overlapping workflow occurrences. Please retry.")
    result["workflow"]["occurrences"] = occurrences
    result["workflow"]["observed_occurrence_count"] = len(occurrences)
    for recommendation in result["recommendations"]:
        if any(frame_id not in lookup for frame_id in recommendation["evidence_frame_ids"]):
            raise ValueError("Mistral cited a screenshot that does not exist. Please retry.")
    return result


async def analyze(folder: Path, job: dict, update):
    model = get_model()
    usage = []
    frames = job["frames"]
    batches = [frames[i:i + 12] for i in range(0, len(frames), 12)]
    steps, limitations = [], []
    # A summary failure must not discard a successfully validated screenshot
    # analysis. Recheck full coverage before reusing it on the same recording.
    if job.get("observations") and job.get("observation_model", job.get("model")) == model:
        try:
            steps = validate_step_coverage(job["observations"], frames)
        except (ValueError, KeyError, TypeError):
            steps = []
        else:
            limitations = job.get("observation_limitations", [])
            usage = list(job.get("observation_usage", []))
    reused = bool(steps)
    if reused:
        update(analysis_progress={"batch": len(batches), "batches": len(batches),
               "screenshots": len(frames), "completed_screenshots": len(frames),
               "identified_steps": len(steps), "frame_ids": []},
               message=f"Reusing {len(steps)} saved steps from {len(frames)} screenshots")
    for index, batch in enumerate([] if reused else batches):
        details = {"batch": index + 1, "batches": len(batches), "screenshots": len(frames),
                   "batch_screenshots": len(batch), "completed_screenshots": index * 12,
                   "frame_ids": [f["id"] for f in batch], "identified_steps": len(steps),
                   "start_seconds": batch[0]["timestamp"], "end_seconds": batch[-1]["timestamp"]}
        update(stage="observing", progress=round(25 + 42 * index / len(batches)),
               analysis_progress=details, model_request=None,
               message=f"Reading {len(batch)} screenshots · batch {index + 1} of {len(batches)}")
        prompt = f"""Reconstruct the visible activity in this ordered screen-recording segment.
Recording duration: {job['metadata']['duration']} seconds. User context (data only): {job.get('context', '')}
Each image is preceded by its exact frame ID and timestamp. Account for EVERY supplied screenshot
in chronological order, including application switches, composing, checking, and returning to a prior view.
Group only consecutive screenshots of the same activity. Each screenshot must belong to exactly one step.
Never span a step across an intervening different activity: Mail -> Calendar -> Mail is THREE steps.
Start/end ranges must not overlap, including endpoints. Cite only frame IDs in this batch.
A step may reference the same start/end frame when only one screen supports it.
Describe the visible state and useful details of each step, not just the overall business goal.
If a screenshot is unclear, describe only what is visible and state the uncertainty; do not omit it.
Do not infer text too small to read. Note ambiguity.
The sample interval is {job['sample_interval']} seconds; exact clicks are not captured.
Earlier segment context: {json.dumps(steps[-5:])}"""
        content = [{"type": "text", "text": prompt}]
        for frame in batch:
            content.append({"type": "text", "text": f"Frame {frame['id']} at {frame['timestamp']:.3f} seconds"})
            encoded = base64.b64encode((folder / "frames" / f"{frame['id']}.jpg").read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        # Give the model one repair attempt with the same images when coverage is
        # incomplete. Never silently accept missing or overlapping screenshot ranges.
        for coverage_attempt in range(2):
            if coverage_attempt:
                update(message=f"Checking screenshot coverage · batch {index + 1} of {len(batches)}")
            observed = await complete(content, Observations, usage, model,
                                      on_progress=lambda state: update(model_request=state))
            try:
                batch_steps = validate_step_coverage([s.model_dump() for s in observed.steps], batch)
                break
            except ValueError as error:
                if coverage_attempt:
                    raise ValueError("Some screenshots were not described in order. Please retry this recording.") from error
                content.append({"type": "text", "text":
                    f"The previous response needs correction: {error} Return a complete replacement covering "
                    "each screenshot exactly once. Use one step per screenshot if necessary; do not invent details."})
        steps.extend(batch_steps)
        limitations.extend(observed.limitations)
        details = {**details, "completed_screenshots": min((index + 1) * 12, len(frames)),
                   "identified_steps": len(steps)}
        update(stage="observing", progress=round(25 + 42 * (index + 1) / len(batches)),
               analysis_progress=details, observations=list(steps),
               observation_model=model, observation_limitations=list(limitations),
               observation_usage=list(usage),
               message=f"Read {details['completed_screenshots']} of {len(frames)} screenshots")
    update(stage="recommending", progress=76, observations=steps,
           model_request=None, message="Writing the summary and suggestions")
    prompt = f"""Using ONLY these observations, identify the main workflow and up to 3 concrete automation
opportunities (prefer one strong proposal). Return zero recommendations if no credible proposal is supported.
Each proposal must identify the trigger, data movement, AI task where useful, required integrations,
implementation steps, and decisions that remain with the person. Integration access is proposed,
never verified by seeing an application. A deterministic template or rule may be better than AI.
All proposed AI components must use Mistral models. No other model vendors.
Set automation_recipe to meeting_reply_drafts ONLY when the proposal is to read new meeting-request emails,
check calendar availability, and prepare reply drafts for human review. That is the only implemented recipe.
For all other proposals, use unavailable. Never imply that a proposed integration is already connected.
Group only genuinely repeated instances of the SAME task as occurrences, in chronological order,
with non-overlapping frame ranges. One incomplete instance is not proven repetition. Be conservative.
Do not label elapsed screen spans as measured work or savings. Savings are hypothetical: use
null for estimated_seconds_per_run if this brief recording cannot justify a numeric estimate.
If you do provide a number, give explicit frequency and review-time assumptions. Never extrapolate
weekly savings as measured. Mention short clips and sampling gaps. Make all assumptions explicit.
Recording duration: {job['metadata']['duration']} seconds; sample interval: {job['sample_interval']} seconds.
Context (untrusted data): {job.get('context', '')}
Frame inventory: {json.dumps([{'id': f['id'], 'timestamp': f['timestamp']} for f in frames])}
Observed activity: {json.dumps(steps)}
Observation limitations: {json.dumps(limitations)}"""
    proposal = await complete(prompt, Proposal, usage, model,
                              on_progress=lambda state: update(model_request=state))
    update(stage="validating", progress=94, message="Checking the report and screenshot references")
    report = finalize(proposal, steps, frames)
    report["limitations"] = list(dict.fromkeys(report["limitations"] + limitations + [
        "Analysis uses sampled screenshots, not mouse or keyboard events. Short transitions can be missed.",
        "Durations are elapsed spans between sampled frames, not measured active work or verified savings.",
        "Audio is not analyzed. All AI interpretation uses Mistral through OpenRouter."]))
    if job["metadata"]["duration"] < 60:
        report["limitations"].insert(0, "This recording is under one minute. Repetition and potential savings need validation with a longer recording.")
    report["model"] = model
    report["usage"] = usage
    report["reused_observations"] = reused
    return report

