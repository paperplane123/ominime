# Chat Source Feasibility Gate

Pipeline entry: [DSH Personal Context dossier](../dossiers/2026-08-17-dsh-personal-context.md).

## Decision

G2 is **BLOCKED** overall. WeChat remains fully blocked. The user-authorized Kim
local reader now passes the narrower on-demand Skill gate, but durable
incremental synchronization is still unproven, so the Kim production connector
also remains blocked.

## User-authorized Kim local adapter evidence path

The user has approved a separately installed, audited local-database reader as
a new Kim-only evidence path. The [Kim Chat Skill
design](../plans/2026-08-18-kim-chat-skill-design.md) restricts that reader behind
four bounded DSH tools and a packaged Skill; it does not authorize a production
connector, background synchronization, persistence, or legacy cutover.

- On-demand Kim Skill: `PASS`
- Kim production connector: `DISABLED`
- Durable Kim incremental synchronization: `NOT PROVEN / BLOCK`
- WeChat production connector: `DISABLED`

The live redacted proof established the six bounded read capabilities used by
the Skill. It did not establish a durable cursor, late-arrival behavior,
mutation behavior, or automatic synchronization. G2 remains blocked overall
while WeChat is blocked and durable Kim ingestion is unproven.

This is a safety and evidence decision, not a claim that either application's
business data is incapable of providing the remaining capabilities. The legacy
snapshot adapters still require an atomic directory-descriptor/`openat` path
helper or a source-owned read-only structured interface.

## Evidence and reproduction

- WeChat: [redacted feasibility report](./wechat-source-feasibility.md), proof
  commit `3b1ccec65889157a08cb0ab19a509b5d755044c6`.
- Kim: [redacted feasibility report](./kim-source-feasibility.md), including the
  earlier fail-closed snapshot result and the user-authorized on-demand proof.
- Source-neutral contract: commit
  `a9b82b319d9fd62550542dfff4b597d6fbf0f892`.

From the repository root, with an arm64 Node version accepted by the plugin's
`engines` field and the locked dependencies installed, reproduce the synthetic
contract and safe-failure checks with:

```sh
pnpm --dir dsh-plugin exec vitest run tests/connector-contract.spec.ts tests/wechat-paths.spec.ts tests/wechat-probe.spec.ts tests/kim-paths.spec.ts tests/kim-probe.spec.ts
```

From a clean checkout, reproduce the live redacted result with the fail-closed
script below. It verifies the locked package-manager version, installs only the
locked dependency graph, checks Node compatibility, and builds the ignored
probe artifacts before execution.

```sh
(
  set -eu
  cd dsh-plugin

  test "$(pnpm --version)" = "11.7.0"
  pnpm install --frozen-lockfile
  pnpm run check:node
  pnpm build

  # Run both probes before evaluating either expected BLOCK status.
  set +e
  wechat_report="$(node lib/probe-wechat.js --redact)"
  wechat_status=$?
  kim_report="$(node lib/probe-kim.js --redact)"
  kim_status=$?
  set -e

  test "$wechat_status" -eq 2
  test "$kim_status" -eq 2

  WECHAT_REPORT="$wechat_report" KIM_REPORT="$kim_report" \
    node --input-type=module <<'NODE'
const expectedShape = [
  'adapterVersion',
  'capabilities',
  'failureCodes',
  'fieldMappings',
]
const expectedCapabilities = [
  'authoritativeDirectionOrSender',
  'conversationAndParticipantIdentity',
  'finalMessageText',
  'incrementalChangeDetection',
  'sourceAccountIdentity',
  'stableMessageIdentity',
  'timestampOrOrdering',
]
const expectedMappings = [
  'authoritativeDirectionOrSender',
  'conversationIdentity',
  'finalMessageText',
  'incrementalChangeDetection',
  'participantIdentity',
  'sourceAccountIdentity',
  'stableMessageIdentity',
  'timestampOrOrdering',
]

for (const [environmentName, failureCode, adapterVersion] of [
  ['WECHAT_REPORT', 'WECHAT_ATOMIC_OPEN_UNAVAILABLE', 'wechat-macos-xwechat-v4-v1'],
  ['KIM_REPORT', 'KIM_ATOMIC_OPEN_UNAVAILABLE', 'kim-macos-structured-v1'],
]) {
  const report = JSON.parse(process.env[environmentName])
  const shape = Object.keys(report).sort()
  if (JSON.stringify(shape) !== JSON.stringify(expectedShape)) process.exit(1)
  if (typeof report.adapterVersion !== 'string') process.exit(1)
  if (report.adapterVersion !== adapterVersion) process.exit(1)
  if (JSON.stringify(report.failureCodes) !== JSON.stringify([failureCode])) process.exit(1)

  const capabilities = Object.entries(report.capabilities)
  const mappings = Object.entries(report.fieldMappings)
  if (JSON.stringify(capabilities.map(([name]) => name).sort()) !==
      JSON.stringify(expectedCapabilities)) process.exit(1)
  if (JSON.stringify(mappings.map(([name]) => name).sort()) !==
      JSON.stringify(expectedMappings)) process.exit(1)
  if (!capabilities.every(([, available]) => available === false)) process.exit(1)
  if (!mappings.every(([, mapping]) => mapping === null)) process.exit(1)
}
NODE
)
```

Both probe commands execute before either status is checked. Exit status `2`
with the exact redacted JSON above is the expected G2 `BLOCK`; any other status,
shape, failure code, capability value, or mapping value makes reproduction fail.
Captured probe output is validated without being printed.

The synthetic tests prove the source-neutral contract, parser behavior, path
containment, redaction, cleanup, and safe failure behavior. They do **not** prove
the real WeChat schema or source stability. Kim's on-demand capability values
come from the separate live redacted reader proof, not from these fixtures.

## WeChat capability decision

The redacted report exposes the single public failure code
`WECHAT_ATOMIC_OPEN_UNAVAILABLE`. Its capability booleans are `false` and its
field mappings are `null`, so every requirement remains unproven.

| Required capability | Decision | Public failure code |
|---|---|---|
| Source account identity | `NOT PROVEN / BLOCK` | `WECHAT_ATOMIC_OPEN_UNAVAILABLE` |
| Conversation and participant identity | `NOT PROVEN / BLOCK` | `WECHAT_ATOMIC_OPEN_UNAVAILABLE` |
| Stable message identity | `NOT PROVEN / BLOCK` | `WECHAT_ATOMIC_OPEN_UNAVAILABLE` |
| Final message text | `NOT PROVEN / BLOCK` | `WECHAT_ATOMIC_OPEN_UNAVAILABLE` |
| Authoritative direction or sender | `NOT PROVEN / BLOCK` | `WECHAT_ATOMIC_OPEN_UNAVAILABLE` |
| Timestamp or ordering key | `NOT PROVEN / BLOCK` | `WECHAT_ATOMIC_OPEN_UNAVAILABLE` |
| Incremental change detection | `NOT PROVEN / BLOCK` | `WECHAT_ATOMIC_OPEN_UNAVAILABLE` |

WeChat source decision: **BLOCK**. Its production connector remains disabled.

## Kim capability decision

The user-authorized local reader proves the first six bounded read capabilities.
Incremental change detection remains unproven, so this is an on-demand Skill
PASS and a production-connector BLOCK. The earlier structured-snapshot probe
continues to fail closed with `KIM_ATOMIC_OPEN_UNAVAILABLE` and is not used by
the Skill.

| Required capability | Decision | Public failure code |
|---|---|---|
| Source account identity | `PROVEN for on-demand` | none |
| Conversation and participant identity | `PROVEN for on-demand` | none |
| Stable message identity | `PROVEN for on-demand` | none |
| Final message text | `PROVEN for on-demand` | none |
| Authoritative direction or sender | `PROVEN for on-demand` | none |
| Timestamp or ordering key | `PROVEN for on-demand` | none |
| Incremental change detection | `NOT PROVEN / BLOCK` | `KIM_CHAT_LIVE_PROOF_INCOMPLETE` |

Kim decision: **on-demand Skill PASS; production connector BLOCK**.

## Risk disposition and unblock conditions

Continuing would require guessing source paths or schemas, accepting a
time-of-check/time-of-use race, or using a prohibited fallback. Any of those
would violate the read-only safety boundary and could misattribute identities,
direction, revisions, or conversation participation.

To re-open the WeChat gate:

1. Provide a reviewed atomic directory-FD/`openat` helper, or a source-owned
   read-only structured interface. This is a prerequisite, not proof that the
   source will pass.
2. Re-run that source's live redacted probe through the reviewed path.
3. Produce authoritative and stable live evidence for every capability in its
   table. All requirements must pass; partial evidence remains `BLOCK`.

To re-open the Kim production-connector gate, prove a durable cursor or an
equivalent bounded change-detection contract across restarts, late arrivals,
edits, retractions, and source mutations. The current on-demand PASS is evidence,
not an automatic production-connector approval.

The local pinned DSH `0.1.1-rc.2` G1 result and this source G2 result are independent:
G1 is `PASS` for the pinned local runtime, while portable npm `0.1.1-rc.2` distribution
is separately `BLOCKED`. Neither result supplies missing live source evidence.

The on-demand Kim Skill and its four restricted tools were implemented. Storage,
migration, background scheduling, and production connectors were not added;
real services were not changed; and the existing legacy keyboard/OCR capture
remains active and unchanged.
