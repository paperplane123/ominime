import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const PINNED_DSH_COMMIT = '73f7fe03bcf776e95f7840c7e398a90774290ac6'
export const PINNED_DSH_VERSION = '0.1.1-rc.2'

export const FRESH_BUILD_ARTIFACTS = [
  'vendor/cordis/lib/types/index.d.ts',
  'packages/core/tools/lib/types/index.d.ts',
  'packages/client/ui-conversation/lib/types/client/index.d.ts',
  'apps/cli/lib/bin.js',
]

const pluginRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const nodeEngines = JSON.parse(readFileSync(join(pluginRoot, 'package.json'), 'utf8')).engines?.node

export function assertSupportedNodeVersion(version = process.versions.node) {
  if (nodeEngines !== '^22.19.0 || >=24.0.0') {
    throw new Error(`unsupported Node engine declaration: ${JSON.stringify(nodeEngines)}`)
  }
  const match = /^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/.exec(version)
  const major = match === null ? Number.NaN : Number(match[1])
  const minor = match === null ? Number.NaN : Number(match[2])
  if (!((major === 22 && minor >= 19) || major >= 24)) {
    throw new Error(`unsupported Node ${version}; Personal Context requires ${nodeEngines}`)
  }
}

export function resolveDshSource({ argv = process.argv.slice(2), env = process.env } = {}) {
  const sourceFlag = argv.indexOf('--dsh-source')
  if (sourceFlag !== -1 && argv[sourceFlag + 1] === undefined) {
    throw new Error('--dsh-source requires a path')
  }
  const candidate = sourceFlag === -1
    ? (env.DSH_SOURCE ?? join(homedir(), 'work/personal/deepseek-harness'))
    : argv[sourceFlag + 1]
  return realpathSync(resolve(candidate))
}

export function verifyPinnedCheckout(dshSource, { runner = execFileSync } = {}) {
  const git = args => runner('git', ['-C', dshSource, ...args], { encoding: 'utf8' }).trim()
  const commit = git(['rev-parse', 'HEAD'])
  if (commit !== PINNED_DSH_COMMIT) {
    throw new Error(`DSH source commit mismatch: expected ${PINNED_DSH_COMMIT}, received ${commit}`)
  }
  const dirty = git(['status', '--short'])
  if (dirty !== '') throw new Error(`DSH source checkout is not clean:\n${dirty}`)
  const manifest = JSON.parse(readFileSync(join(dshSource, 'package.json'), 'utf8'))
  if (manifest.version !== PINNED_DSH_VERSION) {
    throw new Error(`DSH source version mismatch: expected ${PINNED_DSH_VERSION}, received ${manifest.version}`)
  }
  return { commit, version: manifest.version }
}

export function withIsolatedDshHome(action, {
  temporaryParent = tmpdir(),
  prefix = 'ominime-dsh-smoke-',
} = {}) {
  const temporaryRoot = mkdtempSync(join(resolve(temporaryParent), prefix))
  const dshHome = join(temporaryRoot, 'dsh-home')
  try {
    return action({ temporaryRoot, dshHome })
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true })
  }
}

export function runDsh(args, {
  cli,
  cwd,
  dshHome,
  runner = execFileSync,
  node = process.execPath,
  env = process.env,
}) {
  return runner(node, [cli, ...args], {
    cwd,
    encoding: 'utf8',
    env: { ...env, DSH_HOME: resolve(dshHome) },
  })
}

/** Build ignored pinned declarations and CLI output from tracked source in a disposable clone. */
export async function withFreshPinnedBuild(action, {
  canonicalSource = resolveDshSource(),
  temporaryParent = tmpdir(),
  runner = execFileSync,
  env = process.env,
} = {}) {
  assertSupportedNodeVersion()
  verifyPinnedCheckout(canonicalSource, { runner })
  const temporaryRoot = mkdtempSync(join(resolve(temporaryParent), 'ominime-dsh-fresh-'))
  const dshSource = join(temporaryRoot, 'deepseek-harness')
  try {
    runner('git', ['clone', '--shared', '--no-checkout', '--quiet', canonicalSource, dshSource], {
      encoding: 'utf8',
    })
    runner('git', ['-C', dshSource, 'checkout', '--detach', '--quiet', PINNED_DSH_COMMIT], {
      encoding: 'utf8',
    })
    verifyPinnedCheckout(dshSource, { runner })
    for (const relativePath of FRESH_BUILD_ARTIFACTS) {
      if (existsSync(join(dshSource, relativePath))) {
        throw new Error(`fresh checkout unexpectedly contains ignored artifact: ${relativePath}`)
      }
    }

    const buildEnv = { ...env, CI: 'true', LEFTHOOK: '0' }
    runner('corepack', ['pnpm', 'install', '--frozen-lockfile'], {
      cwd: dshSource,
      env: buildEnv,
      stdio: 'inherit',
    })
    runner('corepack', ['pnpm', 'run', 'build:lib'], {
      cwd: dshSource,
      env: buildEnv,
      stdio: 'inherit',
    })
    for (const relativePath of FRESH_BUILD_ARTIFACTS) {
      if (!existsSync(join(dshSource, relativePath))) {
        throw new Error(`fresh pinned build did not produce required artifact: ${relativePath}`)
      }
    }
    verifyPinnedCheckout(dshSource, { runner })
    return await action({ temporaryRoot, dshSource })
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true })
  }
}
