"""Generate held-out paraphrases with a model that did not write the router or the dev cases.

    python -m evals.gen_heldout --model openai:<model> --per-intent 6 > evals/heldout.draft.yaml

The output is a DRAFT. Every question is reviewed by a person before it moves into
evals/heldout.yaml (drop ambiguous or wrong-intent questions; do not edit them to suit the agent).
Expectations come from the intent, so scoring still uses the independent ground truth.
"""
from __future__ import annotations

import argparse
import json
import sys

import yaml

from fleet_copilot import config  # noqa: F401  (loads .env, e.g. OPENAI_API_KEY)

TENANTS = ["acme-001", "globex-002", "initech-003"]

INTENTS = {
    "low_disk": ("Which devices are currently low on free disk space?", {"devices": "low_disk"}),
    "os_older": ("Which Macs run a macOS version older than macOS 15?",
                 {"devices": "os_older", "devices_args": {"os_name": "macOS", "version": "15"}}),
    "screen_lock": ("Which devices fail the screen lock compliance check?",
                    {"devices": "failing", "devices_args": {"check_id": "screen_lock"}}),
    "encryption": ("Which devices fail the disk encryption check?",
                   {"devices": "failing", "devices_args": {"check_id": "disk_encryption"}}),
    "battery": ("Which laptop batteries are at or near end of life?", {"devices": "battery_eol"}),
    "ram": ("Which devices are consistently short of RAM over the month (not one-off spikes)?",
            {"devices": "ram_consistent"}),
    "storage_trend": ("Which devices have been low on storage for most of the month?", {"devices": "storage_consistent"}),
    "drift": ("Which devices regressed on a compliance check during the period (passed before, failing now)?",
              {"devices": "regressions"}),
    "unapproved": ("Which devices have software that is not on the approved list?", {"devices": "unapproved"}),
    "tickets": ("Open remediation tickets for every device failing the screen lock check.",
                {"proposals": {"action": "open_remediation_ticket", "devices": "failing",
                               "args": {"check_id": "screen_lock"}}, "approve": "none"}),
    "replace": ("Propose flagging the devices that meet the replacement criteria.",
                {"proposals": {"action": "flag_device_for_replacement", "devices": "replacement_eligible"},
                 "approve": "none"}),
    "notify": ("Message the employees whose devices are low on disk space.",
               {"proposals": {"action": "notify_employee", "employees": "low_disk"}, "approve": "none"}),
}

PROMPT = """You write test questions for an IT fleet assistant used by IT admins.
Rewrite the request below in {n} different ways an IT admin might actually type it.
Vary register and wording: terse, chatty, indirect, a typo or two, jargon, non-native English,
a question vs. an instruction. Keep the exact meaning (same scope, same threshold if one is stated,
no extra conditions, no other companies). Do not mention device IDs or company names.
Request: {request}
Return JSON: {{"questions": ["...", ...]}}"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="LangChain provider:model, e.g. openai:<model>")
    ap.add_argument("--per-intent", type=int, default=6)
    a = ap.parse_args(argv)
    from langchain.chat_models import init_chat_model
    llm = init_chat_model(a.model)
    cases, i = [], 0
    for intent, (request, expect) in INTENTS.items():
        msg = llm.invoke(PROMPT.format(n=a.per_intent, request=request)).text
        qs = json.loads(msg[msg.index("{"): msg.rindex("}") + 1])["questions"][: a.per_intent]
        for q in qs:
            cases.append({"id": f"ho-{intent}-{len([c for c in cases if c['intent'] == intent]) + 1}",
                          "intent": intent, "category": "heldout", "tenant": TENANTS[i % 3],
                          "question": q.strip(), "expect": expect})
            i += 1
    yaml.safe_dump({"generated_by": a.model, "reviewed": False, "cases": cases}, sys.stdout,
                   sort_keys=False, allow_unicode=True, width=120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
