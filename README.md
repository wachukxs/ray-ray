# Fleet Copilot

An agentic copilot for IT admins over device telemetry. It answers fleet health and compliance
questions with cited evidence, surfaces trends, and proposes operational actions that run only
after a human approves them.

Built with **LangGraph** (explicit agent graph and human-in-the-loop interrupt). The model that
drives it is pluggable: a **Claude brain** on the Anthropic SDK, a **LangChain brain** for any
provider LangChain supports (OpenAI, Google Gemini, Anthropic, ...), and a **deterministic offline
brain**, so the full system and its eval suite run without an API key.

```
admin > Which devices at Acme are low on disk space?
Devices low on disk space - 2 device(s) [query:q1] (definition: latest snapshot free disk < 10.0%):
- MT7PJB7N5LRE (MacBook Pro, emp-acme-1003): 2.0% free (10.2 GB of 512 GB) on latest snapshot. [snap:MT7PJB7N5LRE@2026-06-12T08:39:00Z]
- 8NM23J95R5I6 (ThinkPad X1 Carbon, emp-acme-1005): 2.6% free (26.5 GB of 1024 GB) on latest snapshot. [snap:8NM23J95R5I6@2026-06-12T09:02:00Z]
```

---

## 1. Install and set up

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ".[langchain]"   # optional: only for the LangChain brain (OpenAI, Gemini, ...)
pip install -e ".[durable]"     # optional: SQLite checkpoints, so pending approvals survive a restart
```

The dataset is already in `data/raw/device-telemetry-dataset.ndjson`.
Runtime state (audit log, executed actions) goes to `state/`.

**Configuration (`.env`)**

Settings are read from environment variables. The copilot also loads a `.env` file at the repo root
(gitignored, so keys stay out of version control). Variables already set in your shell take precedence over the file.

```bash
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
# then edit .env and set the key for the provider you use
```

| variable | default | purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | enables the Claude brain |
| `FLEET_COPILOT_MODE` | `claude` if a key is set, else `offline` | which brain to use (`--mode` on the CLI overrides it) |
| `FLEET_COPILOT_MODEL` | `claude-opus-5-5` | Claude model ID |
| `FLEET_COPILOT_EFFORT` | `medium` | Claude effort: `low` · `medium` · `high` · `xhigh` · `max` |
| `FLEET_COPILOT_LC_MODEL` | unset | LangChain brain model as `provider:model`, e.g. `openai:<model>`, `google_genai:<model>`, `anthropic:<model>` |
| `FLEET_COPILOT_LC_TEMPERATURE` | unset | optional temperature for the LangChain brain (left unset by default because some current models reject it) |
| `OPENAI_API_KEY` / `GOOGLE_API_KEY` | unset | provider key read by LangChain for `openai:` / `google_genai:` models |
| `FLEET_DATA_PATH` | `data/raw/device-telemetry-dataset.ndjson` | telemetry file |
| `FLEET_STATE_DIR` | `state/` | audit log and executed actions |
| `FLEET_APPROVERS_FILE` | unset (dev identities) | approver directory: identities, tenants, roles (see `approvers.example.json`) |
| `FLEET_APPROVAL_SECRET` | unset | when set, approval decisions must be HMAC-signed by the approval channel |
| `FLEET_PROPOSAL_TTL_HOURS` | `24` | approval window per proposal |
| `FLEET_CHECKPOINT_DB` | unset (in-memory) | SQLite file for durable graph checkpoints (needs the `durable` extra) |

**Brain selection**

| mode | how | when |
|---|---|---|
| `offline` (default without a key) | rule-based planner + recipes | reproducible demo and evals, no network |
| `claude` | set `ANTHROPIC_API_KEY` in `.env` or your shell (or pass `--mode claude`) | natural-language robustness, with Claude-specific features (effort, refusal fallbacks) |
| `langchain` | `FLEET_COPILOT_MODE=langchain` (or `--mode langchain`) + `FLEET_COPILOT_LC_MODEL` + that provider's key | any LangChain-supported provider; compare models on the same eval suite |

Example `.env` for OpenAI or Gemini:

```bash
FLEET_COPILOT_MODE=langchain
FLEET_COPILOT_LC_MODEL=openai:<model-name>         # or google_genai:<model-name>
OPENAI_API_KEY=sk-...                               # or GOOGLE_API_KEY=...
```

## 2. Run the agent

```bash
python -m fleet_copilot tenants
python -m fleet_copilot ask --tenant acme "Which devices are low on disk space?"
python -m fleet_copilot ask --tenant acme -v "Show me compliance drift over time"          # -v shows plan, tools, verifier
python -m fleet_copilot ask --tenant initech "Flag devices that should be replaced" --approve P1 --approver alice
python -m fleet_copilot decide --tenant initech --thread <thread-id> --approve P1 --approver alice   # resume after restart (FLEET_CHECKPOINT_DB)
python -m fleet_copilot chat --tenant globex                                                # interactive; asks for approval inline
python -m fleet_copilot audit --tail 20 --verify                                            # audit log + hash-chain check
```

Things to try: "Give me a fleet health summary", "Which batteries are approaching end-of-life?",
"Which devices are consistently constrained by RAM?", "Upgrade RAM on constrained devices"
(Initech: refused, because Mac minis have soldered RAM), "Notify employees whose devices are low on disk",
"Which devices at Globex are low on disk?" (asked as Acme: blocked).

## 3. Run the evaluations

```bash
python -m evals.run                                   # offline brain: deterministic, a few seconds
python -m evals.run --mode claude                     # Claude brain (needs ANTHROPIC_API_KEY)
python -m evals.run --mode langchain --workers 8      # FLEET_COPILOT_LC_MODEL (any provider)
python -m evals.run --mode langchain --repeats 3 --judge openai:<model>   # variance + LLM-judge quality score
python -m evals.run --only act- -v                    # subset, with traces for failures
pytest                                                # 51 unit tests (incl. Claude and LangChain loops against scripted models)
```

The runner writes `evals/results/report-<mode>.md` and `.json` (LangChain runs include the model in the
name, e.g. `report-langchain-openai-<model>.md`, so providers can be compared side by side). It exits non-zero if any safety
invariant is violated or the dev pass rate drops below 90%.

**Splits.** The suite has a **dev** split (`cases.yaml`: 58 agent, policy and verifier cases, written alongside the
offline router, so a high score there is partly self-grading; gated) and a held-out **test** split (92 questions:
8 hand-written paraphrases plus 84 in `evals/heldout.yaml`, generated by a different model than the one that wrote
the agent, reviewed for meaning, never tuned against; reported, not gated). `evals/gen_heldout.py` regenerates the draft.

**Scoring.** Device answers are scored on what the answer *claims* (a device in a negated statement such as "X is
not low on disk" does not count). Counts are compared structurally (the number attached to "devices", "laptops",
...; number words included), not "any matching number in the text". An agent that declines an unjustified action on
its own counts as correct, the same as a policy refusal; the report says which. `--repeats N` reports mean and
standard deviation of pass rates plus the cases that flip between runs. `--judge` adds a 1-5 rubric score for
usefulness, prioritisation and caveats (uncalibrated until it is checked against human labels; never gated).

**Current results**

Same suite, same prompts, same controls; one report per brain in `evals/results/`. LLM runs use the
LangChain brain against OpenAI (`gpt-5.4-mini` three times, the others once). The LLM judge is `gpt-5.5` for all runs
(so its score for `gpt-5.5` may carry self-preference).

| metric | offline | gpt-5.4-nano | gpt-5.4-mini (3 runs) | gpt-5.5 |
|---|---|---|---|---|
| **dev split** (58 gated cases) | 58/58 | 0.83 | **0.91 ± 0.03** (0.88 / 0.95 / 0.90) | 0.93 |
| **held-out test split** (92, not tuned against) | **61/92** | 85/92 | **86/92** in all three runs | **90/92** |
| original 8 hand-written paraphrases | 2/8 | 7/8 | 8/8 | 8/8 |
| test-split device F1 | 0.68 | 0.95 | 0.97 | 0.97 |
| grounded (verifier passed) | 1.00 | 0.94 | 1.00 | 1.00 |
| action-target F1 | 1.00 | 0.53 | 0.77 | 0.83 |
| plan adherence (planned tools actually used) | 1.00 | 0.68 | 0.85 | 0.99 |
| re-plans triggered | 0 | 11 | 6 | 8 |
| LLM-judge quality, 1-5 (uncalibrated) | n/a | 4.36 | 4.45 | 4.49 |
| mean latency per turn | 0.2 s | 6.4 s | 3.9 s | 10.1 s |
| unsafe executions · cross-tenant leaks · policy bypasses · verifier misses | **0 · 0 · 0 · 0** | **0 · 0 · 0 · 0** | **0 · 0 · 0 · 0** | **0 · 0 · 0 · 0** |

How to read this:

- **The model-driven path is now measured.** On questions nobody tuned against, the LLM brains get 85-90 of 92
  against the keyword router's 61, and 7-8 of 8 against 2 on the original hand-written paraphrases. The safety
  invariants hold for every model. Nothing executed without approval, and nothing leaked across tenants.
- **The offline router is still the CI baseline.** It scores 58/58 on dev because the two were written together.
  Treat that number as a regression check, not as evidence of language understanding.
- **What the LLMs still get wrong.** In replacement requests, models often propose only the strongest candidates
  instead of every device that meets the criteria (action-target F1 0.53-0.83). In drift questions they also list
  devices that never passed, so they appear in the answer even though the expected set covers only regressions
  (partly a scoring limitation). On the summary question, some devices are left out. None of these are unsafe:
  the policy still decides every proposal and nothing runs without approval.
- **Variance is real but small** for `gpt-5.4-mini`: dev 0.91 ± 0.03 across three runs, test split identical across
  runs. The report lists the cases that flip between runs.
- **The first live pass found a verifier false positive.** Models quoted a real figure (a 30-day mean, a threshold)
  but cited the snapshot instead of the query that returned it, and answers were withheld. Figures are now required
  to exist in the turn's evidence; a mis-attributed citation is recorded but not failed. A fabricated number still fails.

## 4. Agent architecture

```mermaid
flowchart LR
    Q([admin question]) --> G[guard<br/>tenant + injection checks]
    G -- blocked --> R1[refuse] --> E([end])
    G --> P[planner<br/>explicit plan]
    P --> A[agent<br/>brain picks next tool calls]
    A -- tool calls --> T[tools<br/>validate · tenant-scope · ledger · audit]
    T --> A
    T -- error / empty result breaks plan --> RP[replan] --> A
    A -- draft answer --> V[verifier<br/>citations, coverage, leaks]
    V -- fail, retry left --> A
    V -- pending proposals --> H{{approval<br/>LangGraph interrupt}}
    H --> X[execute<br/>policy re-check, then act] --> S[respond] --> E
    V -- no proposals --> S
```

- **Explicit loop.** `agent ⇄ tools` is a graph cycle, capped at 8 steps. The planner's plan is stored in
  state, shown with `-v`, written to the audit log, and injected into the LLM brains' system prompt.
- **Plans are tracked, not just pasted.** Each planned tool is marked done / failed / pending / skipped as the turn
  runs (`planning.py`). The LLM sees that progress in its system prompt, and the final adherence (plus any
  unplanned calls) goes to the audit log and the eval report. A **replan** node runs when a tool errors, or when a
  read returns nothing while the plan still has action steps that would need its evidence. It runs at most once per turn.
  A planner failure (e.g. a provider's structured output breaking) is logged as `planner_failed` with the reason.
  Provider errors are logged as `model_error` with type, message, HTTP status and request ID.
- **Brain vs. graph.** The brain (offline, Claude or LangChain) only decides *what to do next* and writes text. Every
  control (tenant scope, argument validation, evidence policy, approval, verification, audit) lives in
  graph nodes and the tool layer, so it holds whichever brain is plugged in.
- **Human in the loop.** `approval` calls LangGraph `interrupt()`. The graph checkpoints and stops, and
  `FleetCopilot.decide()` resumes it with `{proposal_id: approve|reject}` plus the approver's name. A missing
  decision counts as a rejection.
- **Claude brain** (`brains/claude.py`): planning uses structured output (`messages.parse` → `Plan`). Acting is a
  manual Messages loop with the tool catalog. Assistant content is appended unchanged; tool results go back as
  `tool_result` blocks. Verifier feedback is a follow-up user turn. Refusal fallback is enabled
  (`fallbacks: "default"`).
- **LangChain brain** (`brains/lc.py`): `init_chat_model("provider:model")`, `with_structured_output(Plan)` for
  planning, `bind_tools` for acting. It uses the same prompts as the Claude brain (`brains/prompts.py`), so an
  eval comparison measures the model, not prompt differences. Messages are stored in graph state as plain
  dicts so the checkpointer can serialise them for any provider. Tool-call IDs are assigned if a provider omits them.
- **Offline brain** (`brains/offline.py`): an intent router chooses a recipe. Recipes are multi-step: e.g.
  *find failing devices → one ticket proposal per device*. Answers are composed only from tool output.

```
fleet_copilot/
  config.py      thresholds and policy constants (single source of truth)
  data.py        NDJSON → typed snapshots; TenantScope (the only data access path)
  analytics.py   deterministic detectors (disk, RAM, battery, drift, software)
  tools.py       tool catalog, evidence ledger, run_tool (validate + audit)
  actions.py     proposals, evidence policy, mock executor
  guardrails.py  input guard, grounding verifier
  graph.py       LangGraph agent
  planning.py    plan adherence tracking, replan triggers
  approvals.py   approver directory and roles, signed decisions, idempotency keys
  brains/        offline.py, claude.py, lc.py (LangChain), prompts.py (shared)
  audit.py       hash-chained JSONL audit log
  copilot.py     session facade · cli.py
evals/           cases.yaml, independent ground truth, runner, reports
tests/           pytest
```

## 5. Tool catalog

No tool has a tenant argument. The tenant comes from the session, and passing `company_id` fails validation.

| tool | kind | purpose |
|---|---|---|
| `fleet_overview()` | read | counts, models, OS mix, device list |
| `list_devices(model?, os_name?, employee_id?, laptops_only?)` | read | filtered inventory (latest snapshot) |
| `get_device(device_id)` | read | latest state, 30-day stats, all active findings |
| `get_device_history(device_id, metric)` | read | per-snapshot series (RAM, disk, battery, compliance) |
| `find_low_disk_devices(free_pct_below=10)` | read | latest free disk below threshold |
| `find_compliance_failures(severity?, check_id?, laptops_only?)` | read | failing checks on latest snapshot |
| `find_os_older_than(os_name, version)` | read | numeric version compare |
| `search_installed_software(name)` / `find_unapproved_software()` | read | inventory search / diff against approved list |
| `detect_storage_pressure()` | insight | share of low-disk snapshots, fill-rate trend, days-to-full |
| `detect_ram_pressure()` | insight | sustained (≥50% of snapshots >90%) vs. watchlist (≥20%) |
| `detect_battery_eol(include_ok?)` | insight | service condition / ≥1000 cycles / capacity drop |
| `detect_compliance_drift()` | insight | pass→fail transitions with the exact transition snapshots; fleet failing-count series |
| `create_upgrade_order(device_id, component, spec, justification, evidence_ids)` | action | propose RAM/storage/battery upgrade |
| `open_remediation_ticket(device_id, check_id, note, evidence_ids)` | action | propose ticket for a failing check (or `unapproved_software`) |
| `flag_device_for_replacement(device_id, reason, evidence_ids)` | action | propose replacement |
| `notify_employee(employee_id, message, evidence_ids)` | action | propose a message (≤500 chars) |

Read and insight tools return `{query_id, as_of, definition, count, results[finding, explanation, metrics, evidence]}`.
Every insight therefore includes **the finding, the supporting evidence, and a short explanation**, plus
the threshold definition used.

## 6. Grounding strategy

1. **All numbers come from deterministic code.** Detectors in `analytics.py` compute every judgement
   ("low", "sustained", "end of life") from thresholds in `config.py`. The model selects and phrases; it never computes.
2. **Evidence IDs.** Each snapshot has a stable ID `snap:<device>@<timestamp>`. Each tool call gets a
   `query:qN` ID that records its args, definition, count and result devices. Device facts cite snapshots;
   counts and *negative* answers ("no laptops fail a high-severity check") cite the query that established them.
3. **Evidence ledger.** Everything a tool returns in a turn is recorded. Only ledger IDs are valid citations.
4. **Verifier node.** Before anything reaches the admin it checks that every citation is in the ledger,
   that every device and employee mentioned was returned by a tool *and* is covered by a citation (a snapshot
   of that device, or a query that returned it), that factual answers cite something, and that no identifier
   from another tenant appears. It also checks **values**: every quantity stated with a unit (`%`, `GB`, `TB`,
   `cycles`) must match a number held by the evidence the answer cites (snapshot fields, or the figures a cited
   query returned, including its threshold definition), or a figure from a proposal made this turn. "2.0% free"
   citing a snapshot that says 12.4% fails. The LLM brains get one retry with the issues listed. If the answer
   still fails, it is withheld ("I'm not going to guess") and its proposals are blocked.
5. **Chosen evidence, not dumped evidence.** Trend findings cite the window start, the first bad reading,
   the worst reading and the latest. Drift findings cite the last passing and first failing snapshot.

## 7. Guardrails

| requirement | mechanism |
|---|---|
| Tenant isolation | `TenantScope` physically holds one tenant's data. Tools have no tenant parameter. A foreign ID fails exactly like a nonexistent one (no existence oracle). |
| Prevent cross-company access | Input guard blocks other tenants' names, IDs, serials, employee IDs and hostnames before any model runs. The verifier and the eval scan outputs and tool logs for foreign identifiers. |
| Explicit human approval | Action tools only create proposals. Execution exists only in the `execute` node, reachable only through the `interrupt()` approval. Requests to skip or pre-grant approval are refused at input. |
| Approver identity and roles | The approver must be in the approver directory, scoped to the tenant, and hold a role for that action type (e.g. `procurement` for upgrade orders, `it_ops` for tickets). With `FLEET_APPROVAL_SECRET` set, a decision must carry an HMAC signature from the approval channel (admin UI or Slack app, which authenticates the person), so nothing typed into chat can approve. |
| Stale approvals | A proposal expires after `FLEET_PROPOSAL_TTL_HOURS`, or as soon as newer telemetry exists than the data it was proposed on. Expired proposals are not executed; the admin re-asks. |
| Duplicate execution | Each approved proposal gets an idempotency key that is stored with the action and is meant for the downstream system. A retried execution returns the original record instead of creating a second ticket or order. |
| Audit logs | Append-only JSONL with a SHA-256 hash chain (`audit --verify`). Logs question, guard result, plan, every tool call, proposals, refusals, approvals and rejections (with approver), executions, verification and the final answer. |
| Refuse actions without sufficient evidence | Server-side evidence policy: cited snapshots must have been retrieved this session and be about the target. The data criterion is then **recomputed** (e.g. RAM upgrade needs sustained pressure, no soldered RAM, and a larger spec; a ticket needs the check to be failing now and no open duplicate). It is re-checked again at execution (time-of-check vs. time-of-use). |
| Prompt injection | Telemetry is treated as data. Override, role-escalation and audit-tampering phrasing is blocked at input. Controls are structural, so a compromised prompt still can't execute anything. |

The input guard is **defence in depth, not a primary control**. A regex can be paraphrased past, so nothing depends
on it: tenant isolation comes from `TenantScope` and tool signatures, and execution from the policy and the approval
interrupt. Its patterns are kept narrow to limit false refusals ("list devices across all companies" is blocked;
"how do you keep other customers' data separate?" is not). A trained classifier is the next step if the guard needs
to catch more.

**Tenant isolation in production.** `TenantScope` enforces isolation in-process. With a real database the same rule
belongs in storage: Postgres row-level security on every telemetry and action table (`USING (tenant_id =
current_setting('app.tenant_id'))`), the session's tenant set with `SET LOCAL app.tenant_id` at the start of each
transaction from the authenticated session (never from model output), and the copilot connecting as a role without
`BYPASSRLS`. A bug in a tool then returns no rows instead of another tenant's rows. Checkpoints, proposals and the
audit log carry the tenant ID and get the same policy.

## 8. Key design decisions and trade-offs

- **Deterministic detectors + LLM orchestration**, not LLM-computed analytics. This gives reproducible,
  testable numbers and leaves the model to do what it's good at (intent, composition, choosing among
  tools). Trade-off: adding a new kind of question needs a tool, not just a prompt.
- **Offline brain behind the same interface.** It gives a free, deterministic baseline and CI-friendly
  evals, and it proves the guardrails don't depend on model behaviour. Trade-off: it is brittle with
  language (61/92 on the held-out test split, against 85-90 for the LLM brains) and is not meant for production conversations.
- **Policy recomputes; citations are necessary but not sufficient.** An agent that can choose its own
  evidence can also choose misleading evidence, so the policy decides from data.
- **Answers withheld when unverifiable.** I chose a useful refusal over a fluent hallucination. The
  verifier checks IDs, coverage, leaks and stated quantities. It does not judge meaning (a correctly cited
  figure used in a misleading sentence still passes); the LLM judge in the evals covers answer quality, not truth.
- **Hardware realism in policy** (soldered Apple-silicon RAM and storage → propose replacement, not upgrade). Small, but it is the kind
  of thing that makes proposed actions actually executable.
- **Mock executors.** Actions are written to `state/actions.jsonl` with idempotency keys. Real integrations
  (ITSM, procurement, Slack) would plug into `ActionStore.execute` and pass the key on.
- **Two LLM brains instead of one abstraction.** The Claude brain calls the Anthropic SDK directly to use
  Claude-specific features (effort, refusal fallbacks, passing thinking blocks back unchanged). The LangChain
  brain trades those for one config switch across providers. Provider choice is then an eval result, not an
  assumption. Note: Gemini's schema conversion drops `additionalProperties`; unknown arguments (e.g. a
  `company_id`) are still rejected by our own Pydantic validation when the tool runs.
- **Determinism.** Current Claude models don't accept `temperature`, so Claude mode can't be made
  bit-for-bit reproducible. Variance is bounded by deterministic tools, the verifier and the policy, and is
  measured with `--repeats`. The offline suite is bit-for-bit reproducible.
- **Python over TypeScript.** LangGraph's Python package is the reference implementation (interrupts,
  checkpointers and the Postgres/SQLite savers land there first). The analytics are numeric code over NDJSON,
  which is simpler and better tested in Python. The eval tooling (YAML cases, pytest, LangChain's provider
  packages) is also Python-first. A TypeScript front end (admin UI, Slack app) would call this service over HTTP and
  own the authenticated approval channel; nothing in the agent needs to run in the browser.
- **Bounded tool output.** Tool results sent to an LLM are capped at 25 list items and about 24k characters, with an
  explicit `truncated` note telling the model it saw a page. Counts stay complete and the evidence ledger keeps
  everything, so citations and the policy are unaffected.
- **Value checks before structured claims.** The verifier extracts quantities from prose and checks them against
  cited evidence. Having the model emit structured claims `{statement, value, evidence_ids}` and rendering prose
  from them would be stricter (it would also cover unit-less numbers and comparative statements). It is the next
  step if prose extraction misses things in practice.
- **Not built yet:** Postgres checkpointer and proposal store (SQLite is wired in through the same interface), a real
  approval UI or Slack app (the signing side is `approvals.sign_decision`), a trained input classifier, human-labelled
  calibration of the LLM judge, human review and growth of the held-out set to ~200, and multi-turn coreference in
  the offline brain.

See [`docs/DATA.md`](docs/DATA.md) for dataset quirks.
