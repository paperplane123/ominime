# DSH compatibility

This scaffold targets DeepSeek Harness `0.1.1-rc.2` at source commit `6ba46b8e2f107d55f0264987799d7b1fa28a5c44`.

## Verified extension points

- The Host half follows `docs/cookbook/adding-a-tool.md` and registers the read-only `personal_context_health` definition through `ctx.tools.register(...)` with the `tools` service injected. The current rc2 G1 registered the definition with the real pinned `ToolRuntime` and executed both the success and invalid-argument paths.
- The browser half contributes to the additive `conversation.view` list slot defined in `packages/client/ui-conversation/src/client/contract/slots.ts`. The pinned source describes this slot as one whole tab rendered one at a time and explicitly recommends it for adding a view without replacing `conversation.session`.
- Registration uses `ctx.slots.inject('conversation.view', ...)`, as required by `packages/client/AGENTS.md`, so it waits for the owner declaration and follows its lifecycle.
- `lib/client.js` uses the `window.__ModuleLoader__.load({ id, factory })` handoff implemented by `packages/client/tsdown.client.ts` and consumed by `packages/client/modules/src/index.ts`.
- The first out-of-tree view uses direct Chinese copy (`个人上下文`) rather than taking a dependency on the pinned locale service. This keeps the scaffold's dependency surface minimal; a complete locale namespace belongs with the later functional GUI, not this compatibility probe.

## Reproducible G1 command

After `pnpm install`, run `pnpm run test:g1`. It deliberately continues past transpilation:

1. `pnpm test` builds the Host/client bundles and runs the manifest, lifecycle, isolation/cleanup, schema, and Host behavior tests.
2. `scripts/g1-pinned.mjs` first enforces this package's Node engine (`^22.19.0 || >=24.0.0`), so unsupported Node 23 and pre-22.19 runtimes fail before cloning or installation. It then rejects a canonical source checkout whose commit, root version, or tracked-file status differs from the pin and creates a disposable detached shared clone at that exact commit. The required ignored declarations and CLI output must be absent before `pnpm install --frozen-lockfile` and `pnpm run build:lib`, and present afterward. The canonical checkout is never installed, built, or modified.
3. The same freshly built clone supplies the real pinned Host/client declarations to temporary TypeScript projects, dispatches the health tool through the real pinned `Context` and `ToolRuntime`, and supplies the real pinned CLI to an isolated install smoke. The smoke uses a unique temporary `DSH_HOME`, checks the composed Host/browser package row and both installed `lib/index.js` and `lib/client.js`, and all temporary clone, typecheck, and profile roots are removed in `finally`.

`pnpm run typecheck:pinned` and `pnpm run smoke:pinned` are also available as standalone proofs; each creates and removes its own fresh pinned build. Source discovery accepts `--dsh-source`, `DSH_SOURCE`, or the documented local default. No absolute path is committed.

## Current rc2 verification

On 2026-08-28, `pnpm run test:g1` passed against the clean pinned `0.1.1-rc.2` checkout with Node `24.19.0`:

- The plugin build and all 312 unit tests passed.
- Freshly built Host and client declarations accepted the production TypeScript sources.
- The real pinned `ToolRuntime` registered the packaged Kim Skill and returned the expected health result for `{}` while preserving the plugin-owned invalid-argument behavior.
- The local tarball installed into a unique temporary `DSH_HOME`; `--dump-config` exposed the `personal-context` Host row and package row, and the installed Host, client, probe, and Kim Skill artifacts were present.
- The installed WeChat and legacy Kim snapshot probes returned only their expected fail-closed codes in the isolated home. This is artifact smoke evidence, not current Kim on-demand capability evidence.

Therefore G1 is **PASS for local source and package compatibility with the pinned `0.1.1-rc.2` runtime**. All clone, typecheck, package, profile, and session roots used by the command were temporary and removed after completion. This run did not start a browser or claim a new rc2 GUI observation.

## Historical rc5 GUI observation

On 2026-08-17, the then-pinned `0.1.0-rc.5` runtime at `47f943859bef60e4160492346772ded9b24f765a` supplied the original browser observation: the temporary Web workspace displayed the normal `对话` and `轨迹` tabs alongside `个人上下文`, and the plugin asset was served from the installed package. That frozen observation supports the original scaffold record only; it is not re-attributed to rc2. Current automated lifecycle tests still exercise callback registration, disposal, owner collapse/redeclaration, and the Chinese tab content without claiming a real-browser rc2 proof.

## Source feasibility gate

G2 remains **BLOCKED overall**:

- WeChat reports only `WECHAT_ATOMIC_OPEN_UNAVAILABLE`.
- The user-authorized on-demand Kim Skill is `PASS` for six bounded read capabilities.
- The Kim production connector and durable incremental synchronization remain `BLOCKED`; the separate legacy snapshot probe still returns only `KIM_ATOMIC_OPEN_UNAVAILABLE`.

The on-demand Kim proof does not establish restart-safe change detection, late-arrival behavior, edits, retractions, or automatic synchronization. WeChat still lacks a reviewed atomic directory-descriptor/`openat` helper or source-owned read-only structured interface. These remaining gaps block both production connectors and overall G2 without negating the narrower Kim PASS.

Both production connectors remain disabled. Storage, migration, and other
downstream source implementation must not proceed while G2 is blocked. The
existing legacy keyboard/OCR capture remains active and unchanged.

See [`chat-source-gate.md`](../docs/verification/chat-source-gate.md) for the authoritative capability decisions and exact unblock conditions.

## Distribution constraint

The manifest records every directly named DSH runtime package as an exact `0.1.1-rc.2` optional peer. G1 resolves the runtime from the clean local source commit above and builds it in a disposable clone; the repository does not commit a machine-specific `file:`/`link:` path. Build-time DSH imports remain type-only and runtime dependencies remain external.

Portable npm distribution remains **BLOCKED** until an independently verified registry or packaged-artifact path provides every exact peer. The PASS above is intentionally limited to the clean local checkout at the exact pinned commit and local tarball smoke; it is not a claim that a third party can reproduce the install from the public registry alone.

This portable distribution block is independent of G2. A local pinned G1
`PASS` does not prove source feasibility, and resolving npm distribution would
not resolve the WeChat or Kim source blocks.
