import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { PINNED_DSH_COMMIT, PINNED_DSH_VERSION, verifyPinnedCheckout } from './isolation.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const pluginRoot = resolve(scriptDir, '..')
const asModule = path => import(pathToFileURL(path).href)

export async function dispatchHealthPinned({ dshSource } = {}) {
  if (dshSource === undefined) throw new Error('dispatchHealthPinned requires a fresh built DSH source')
  verifyPinnedCheckout(dshSource)
  const pluginArtifact = join(pluginRoot, 'lib/index.js')
  if (!existsSync(pluginArtifact)) throw new Error(`built Personal Context Host not found: ${pluginArtifact}`)

  const [cordis, tools, skills, systemPrompt, llm, plugin] = await Promise.all([
    asModule(join(dshSource, 'vendor/cordis/lib/index.js')),
    asModule(join(dshSource, 'packages/core/tools/lib/index.js')),
    asModule(join(dshSource, 'packages/skill/skill/lib/index.js')),
    asModule(join(dshSource, 'packages/core/system-prompt/lib/index.js')),
    asModule(join(dshSource, 'packages/llm/llm/lib/index.js')),
    asModule(pluginArtifact),
  ])
  const ctx = new cordis.Context()
  try {
    await ctx.plugin(systemPrompt.default)
    await ctx.plugin(tools.default)
    await ctx.plugin(skills.default)
    plugin.apply(ctx)
    const kimSkill = await ctx.skills.get('kim-chat-history')
    assert.equal(kimSkill?.source, 'runtime')
    assert.equal(kimSkill?.provider, 'personal-context')
    assert.equal(kimSkill?.invocation.modelInvocable, true)
    assert.equal(kimSkill?.invocation.userInvocable, true)
    assert.match(kimSkill?.content ?? '', /kim_chat_messages/)
    const signal = new AbortController().signal
    let call = 0
    const execute = argumentsValue => ctx.tools.execute({
      signal,
      callId: llm.CallId(`personal-context-health-${call += 1}`),
      name: 'personal_context_health',
      arguments: argumentsValue,
    })

    const success = await execute({})
    assert.equal(success.isError, false)
    assert.deepEqual(success.value, { status: 'scaffold', schemaVersion: 0, sources: [] })

    const cases = [
      [{ unexpected: true }, 'invalid personal_context_health arguments: unexpected property "unexpected"'],
      [[], 'invalid personal_context_health arguments: "arguments" must be an object'],
      ['text', 'invalid personal_context_health arguments: "arguments" must be an object'],
      [null, 'invalid personal_context_health arguments: "arguments" must be an object'],
    ]
    for (const [argumentsValue, expectedMessage] of cases) {
      const result = await execute(argumentsValue)
      assert.equal(result.isError, true)
      assert.equal(result.error?.message, expectedMessage)
      assert.equal(result.error?.info, undefined)
      assert.deepEqual(result.content, [{ type: 'text', text: `Error: ${expectedMessage}` }])
    }

    const result = {
      status: 'ok',
      dshVersion: PINNED_DSH_VERSION,
      dshCommit: PINNED_DSH_COMMIT,
      tool: 'personal_context_health',
      dispatch: 'rc2 ToolRuntime',
      invalidErrorInfo: 'absent',
      runtimeSkill: 'kim-chat-history',
    }
    process.stdout.write(`${JSON.stringify(result)}\n`)
    return result
  } finally {
    await ctx.fiber.dispose()
  }
}
