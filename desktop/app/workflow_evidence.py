"""Locally matched source/result pairs for supported visible workflows."""
import json
import math
import os
from pathlib import Path
import subprocess


HELPER = Path.home() / "Applications/chonkytasks.app/Contents/Helpers/FindWorkflowEvidence"
KINDS = {"mailToCalendar", "mailToTask", "mailToNote", "invoiceFiling", "spreadsheetReport", "orderDraft"}


def scan(source: Path, preview: Path, duration: float) -> list[dict]:
    helper = Path(os.environ.get("CHONKYTASKS_EVIDENCE_HELPER", str(HELPER)))
    if not helper.is_file():
        raise ValueError("The local workflow evidence helper is unavailable.")
    paths = (source, preview) if source.suffix.lower() in {".mp4", ".mov", ".m4v"} else (preview,)
    for path in paths:
        try:
            result = subprocess.run([str(helper), str(path)], check=True, timeout=240,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            return checked_findings(json.loads(result.stdout), duration)
        except subprocess.TimeoutExpired:
            break
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError):
            continue
    raise ValueError("Local workflow evidence could not be checked for this recording.")


def checked_findings(items, duration: float) -> list[dict]:
    if not isinstance(items, list) or len(items) > len(KINDS):
        raise ValueError("Invalid local evidence.")
    seen = set()
    for finding in items:
        kind = finding["kind"]
        if kind not in KINDS or kind in seen or not 1 <= len(finding["examples"]) <= 4:
            raise ValueError("Invalid local workflow.")
        seen.add(kind)
        pairs = set()
        for example in finding["examples"]:
            source, result = example["source"], example["result"]
            if (not math.isfinite(source) or not math.isfinite(result)
                    or not 0 <= source < result <= duration + .003 or (source, result) in pairs):
                raise ValueError("Invalid local evidence timestamp.")
            pairs.add((source, result))
    return items


def meeting_recommendation(findings: list[dict]) -> dict | None:
    meeting = next((item for item in findings if item["kind"] == "mailToCalendar" and len(item["examples"]) >= 2), None)
    if meeting is None:
        return None
    return {
        "automation_recipe": "meeting_reply_drafts", "activation_ready": True,
        "title": "Handle new meeting requests", "summary": "The recording shows repeated email-to-calendar work. The meeting recipe can check new requests and prepare a reply for approval.",
        "confidence": "high", "evidence_frame_ids": [], "local_example_kind": "mailToCalendar",
        "automated_steps": ["Find meeting requests in new Mail messages.", "Check Calendar and prepare the appropriate reply."],
        "human_steps": ["Review the exact reply and any calendar event before applying them."],
        "implementation_steps": ["Connect Mail, Calendar, and Mistral Workflows.", "Activate the recurring meeting check."],
        "integrations": [{"name": "Mail and Calendar", "purpose": "Read requests, check availability, and apply approvals.",
                          "requirement": "A connected Mail account and permission to use Calendar."}],
        "effort": "small", "savings": {"estimated_seconds_per_run": None, "assumptions": []}, "assumptions": [],
    }



def recipe_recommendations(findings: list[dict]) -> list[dict]:
    from .office_recipes import RECIPES

    descriptions = {
        "mailToTask": ("new emails matching a subject prefix you choose", "a follow-up task with its due date"),
        "mailToNote": ("new emails matching a subject prefix you choose", "a note containing the email details"),
        "invoiceFiling": ("invoices arriving in its Inbox", "a copy organized by vendor and invoice month"),
        "spreadsheetReport": ("CSV files arriving in its Inbox", "a report for the status you choose"),
        "orderDraft": ("order requests arriving in its Inbox", "a structured order draft"),
    }
    recommendations = []
    for finding in findings:
        kind = finding["kind"]
        if kind not in RECIPES or len(finding["examples"]) < 2:
            continue
        recipe = RECIPES[kind]
        inputs, output = descriptions[kind]
        mail = recipe["source"] == "mail"
        recommendations.append({
            "automation_recipe": kind, "activation_ready": True, "workflow_source": recipe["source"],
            "title": recipe["title"],
            "summary": f"The recording shows this task more than once. ChonkyTasks can turn {inputs} into {output} for approval.",
            "confidence": "high", "evidence_frame_ids": [], "local_example_kind": kind,
            "automated_steps": [f"Check for {inputs} every 30 seconds.", f"Prepare {output} on this Mac."],
            "human_steps": ["Review the source and proposed result in Activity. Approve it to save it."],
            "implementation_steps": ["Connect Mistral Workflows and set up the source.", "Activate the recurring check."],
            "integrations": [{"name": "Mail and Mistral Workflows" if mail else "Inbox and Mistral Workflows",
                              "purpose": "Check for new inputs on this Mac.",
                              "requirement": "A connected Mail account." if mail else "An app-managed Inbox folder."}],
            "effort": "small", "savings": {"estimated_seconds_per_run": None, "assumptions": []}, "assumptions": [],
        })
    return recommendations


def attach(report: dict, job: dict) -> dict:
    local = job.get("local_findings", [])
    report["local_findings"] = local
    report["local_evidence_status"] = job.get("local_evidence_status", "unavailable")
    meeting = meeting_recommendation(local)
    proven = ([meeting] if meeting else []) + recipe_recommendations(local)
    enabled_recipes = {item["automation_recipe"] for item in proven}
    report["recommendations"] = proven + [
        item for item in report["recommendations"] if item.get("automation_recipe") not in enabled_recipes
    ]
    return report


def local_report(job: dict) -> dict:
    local = job.get("local_findings", [])
    examples = sum(len(item["examples"]) for item in local)
    return attach({
        "title": "Local workflow findings", "model": "On-device evidence checks",
        "summary": (f"Matched {examples} source/result examples across {len(local)} workflow types." if local else
                    "No supported source/result pairs were confirmed on this Mac."), "timeline": [],
        "workflow": {"name": "Local evidence", "description": "Matched examples appear under Automations.",
                     "occurrences": [], "observed_occurrence_count": 0,
                     "repetition_assessment": "The checks cover six supported workflow types."},
        "recommendations": [], "limitations": ["Only the local matching pass ran. No recording evidence was shared."],
    }, job)
