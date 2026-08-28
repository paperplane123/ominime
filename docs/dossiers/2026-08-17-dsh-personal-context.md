# DSH Personal Context Dossier

## Current status

- Current stage: S5 on-demand Kim vertical slice verification.
- Current decision: **on-demand Kim Skill PASS; overall still BLOCKED at G2**.
- Delivery guard: Task 7 and all downstream implementation are prohibited until
  G2 passes for both required chat sources and durable ingestion.
- Runtime guard: the existing legacy keyboard/OCR capture remains active and
  unchanged. No real service or deployment was modified.
- Resume point: read this dossier, then the
  [source gate](../verification/chat-source-gate.md) and
  [Kim Chat Skill design](../plans/2026-08-18-kim-chat-skill-design.md), then the
  [source-owned interface reframe](../plans/2026-08-18-source-owned-chat-interface-reframe-design.md), then the
  [source-owned interface investigation plan](../plans/2026-08-18-source-owned-chat-interface-investigation.md), then the
  [implementation plan](../plans/2026-08-17-dsh-personal-context-hub-implementation.md).

## S0 Intake

### User request, verbatim

> “可以定时拉取kim和微信的聊天记录，这样应该是最好的，获取kim和微信，有发言的上下文，组织起来，区分哪些是我的，哪些是我回复的，形成针对kim和微信以及其他app的聊天记录，这样沉淀到自己的知识库，是一个比较靠谱的做法，结合自己的录音之后的asr，如果这样做，那应该重新设计，比如基于DSH，引入不同的上下文的来源，将自己在不同的app下的输入，作为一个上下文，存入到个人知识库。或者给出更好的设计。”

### User clarifications, verbatim

> “思考其他的解决方案 不走OCR”

> “要业务无感 对我无感”

> “DeepSeek Harness”

> “前者”

> “B是不是更好？”

> “可以 另外也需要一个GUI页面”

The intended user is the local device owner. The requested outcome is a
source-aware personal knowledge base combining qualifying Kim and WeChat
history, recording ASR, retrieval, and a GUI without visible interaction with
the chat applications. The current workaround is the legacy capture path; it is
preserved because the replacement has not passed feasibility.

## S1 Current-system survey

- The [historical baseline report](../verification/dsh-personal-context-baseline.md)
  records the initial OmniMe state, test baseline, then-pinned DSH release, and the
  requirement to preserve the legacy database and capture path.
- [DSH compatibility](../../dsh-plugin/DSH_COMPATIBILITY.md) records the local
  pinned-runtime evidence and the independent portable distribution constraint.
- The [WeChat report](../verification/wechat-source-feasibility.md) and
  [Kim report](../verification/kim-source-feasibility.md) contain redacted source
  evidence only.
- No generated system map exists for this repository, so this dossier does not
  restate architecture totals.

## S2 Requirement definition

The approved [design](../plans/2026-08-17-dsh-personal-context-hub-design.md)
defines the DSH-native B+ architecture, local data boundary, source-neutral
connector contract, participation semantics, ASR integration, retrieval, GUI,
and privacy constraints. It explicitly excludes OCR fallback, UI automation,
event interception, clipboard modification, network capture, process injection,
and guessed source facts.

The approved
[G2 reframe design](../plans/2026-08-18-source-owned-chat-interface-reframe-design.md)
adds a feasibility-only investigation of the Kim internal open platform and
WeChat-owned loopback listeners. It does not weaken the original exclusions or
authorize downstream implementation.

The user subsequently approved the
[Kim Chat Skill design](../plans/2026-08-18-kim-chat-skill-design.md), which adds
a user-authorized local-database adapter as a Kim-only evidence path. It permits
an on-demand restricted Skill implementation and redacted proof, but it does not
enable the production Kim connector, storage, background synchronization,
WeChat ingestion, or legacy cutover.

## S3 Plan and feasibility evidence

The [implementation plan](../plans/2026-08-17-dsh-personal-context-hub-implementation.md)
binds the work to test-first delivery and named gates. Evidence completed before
this decision includes:

- `docs: record personal context baseline`
- `feat: scaffold DSH personal context bundle`
- `feat: define personal context connector contract`
- `spike: verify structured WeChat source`
- `spike: verify structured Kim source`
- `docs: decide chat source feasibility gate`

These are commit subjects in repository history, avoiding environment-specific
references in the dossier. The authoritative feasibility decision is the
[source gate](../verification/chat-source-gate.md).

The superseded internal-platform branch remains explicit evidence rather than
being erased: [Kim source-owned catalog gate](../verification/kim-source-owned-catalog.md) `BLOCK`.

## S4 Requirement breakdown

The implementation plan decomposes the approved design into an isolated bundle,
source-neutral contract, source probes, gate decision, storage, normalization,
retrieval, GUI, ASR, shadow synchronization, service cutover, and acceptance.
The baseline, bundle scaffold, neutral contract, read-only feasibility spikes,
G2 decision, and the separately approved on-demand Kim Skill slice have been
executed. No production connector, storage, migration, cutover, or deployment
work was executed.

## S5 Implementation state

The branch now includes the packaged `kim-chat-history` Skill, four restricted
DSH tools, executable provenance verification, a bounded lifecycle-owned MCP
client, private multi-account selection, and a redacted live proof. The live
proof established the six capabilities required for explicit on-demand reads.

The production WeChat and Kim connectors remain disabled. Durable incremental
sync, storage, scheduling, automatic knowledge ingestion, and legacy cutover
remain separately gated.

## Gate record

| Gate or constraint | Decision | Evidence and consequence |
|---|---|---|
| G1 local pinned DSH compatibility | `PASS` | [Compatibility report](../../dsh-plugin/DSH_COMPATIBILITY.md); limited to the pinned local runtime. |
| Portable npm `0.1.1-rc.2` distribution | `BLOCK` | Independent distribution constraint in the compatibility report; it neither changes nor resolves G2. |
| G2 source feasibility | **`BLOCKED at G2`** | [Source gate](../verification/chat-source-gate.md); Kim on-demand reads pass, but WeChat and durable Kim ingestion remain blocked. Stop before the original hub Task 7. |
| G3 data and privacy | `NOT STARTED` | Storage, participation filtering, normalization, and privacy implementation were not started. |
| G4 feature review | `NOT STARTED` | No downstream feature implementation exists to review. |
| G5 deployment health | `NOT STARTED` | No deployment was attempted. |
| G6 live acceptance | `NOT STARTED` | No production or live acceptance path was attempted. |

## G2 risk decision

The legacy source adapters fail closed because they do not have an approved
atomic directory-descriptor/`openat` path helper or a source-owned read-only
structured interface. The separately approved Kim local reader proves bounded
on-demand reads, but not durable synchronization. This does not prove that the
remaining business capabilities are absent.

Continuing would require unsafe or guessed source behavior and would violate the
design. Therefore no work may pass G2 with partial or synthetic-only evidence.

## Approved G2 reframe

The user approved the following boundaries:

- one-time enterprise SSO, application registration, and administrator approval
  are acceptable if later operation is unattended;
- dedicated test identities and an isolated macOS user are permitted;
- an undocumented WeChat-owned loopback interface may receive bounded standard
  protocol classification in the isolated environment;
- protocol guessing, authentication bypass, process injection, and private
  reverse engineering remain prohibited;
- the selected approach is Kim internal-open-platform validation plus isolated
  WeChat loopback classification.

The reframe is documented in the
[approved design](../plans/2026-08-18-source-owned-chat-interface-reframe-design.md).
It creates a new evidence path but does not change the current G2 decision.

Read-only research found no public personal-chat-history API for WeChat and no
public local chat interface for Kim. The existing third-party Chatlog process is
excluded because it is not source-owned, its upstream is unsupported after a
compliance notice, and its observed launch model exposes sensitive arguments.
No change to that process is authorized by this feature.

## Exact unblock conditions

1. For WeChat, provide a reviewed atomic directory-FD/`openat` helper or a
   source-owned read-only structured interface, then re-run its redacted probe.
2. For Kim background ingestion, prove restart-safe change detection, late
   arrivals, edits, retractions, and source mutation behavior.
3. Re-evaluate overall G2 only after both remaining evidence sets pass. Partial
   evidence remains `BLOCK`.

## Pending decision after reframe evidence

No further product decision is required for the bounded on-demand Kim Skill.
Continuing the original hub Task 7 or disabling the legacy capture path remains
prohibited unless WeChat and durable Kim ingestion both pass G2.

## Deployment and acceptance

No deployment occurred. G5 and G6 were not entered, no rollback point was
needed, and no production acceptance claim is made.
