import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, stat, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { nativeRequire } from '../src/platform/runtime.mjs';

const expectedPins = {
  '@linxin666/dsh-client-ui-community-plugins': '0.4.18',
  '@linxin666/dsh-client-ui-preset-center': '0.4.18',
  '@linxin666/dsh-client-ui-skin-center': '0.4.18',
  '@linxin666/dsh-pet': '0.4.18',
  marked: '18.2.0',
};

async function fixture(source) {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-community-pins-'));
  const profile = join(dataRoot, 'profiles/web');
  await mkdir(profile, { recursive: true });
  const path = join(profile, 'pnpm-workspace.yaml');
  await writeFile(path, source);
  return { dataRoot, path };
}

async function applyPins(dataRoot) {
  const { pinCommunityDependencies } = await import('../src/platform/community-dependencies.mjs');
  return pinCommunityDependencies(dataRoot);
}

function loadWorkspace(source) {
  return nativeRequire()('js-yaml').load(source);
}

test('a generated profile workspace receives the five exact community dependency pins', async () => {
  const { dataRoot, path } = await fixture("packages:\n  - '.'\n");
  await applyPins(dataRoot);
  assert.deepEqual(loadWorkspace(await readFile(path, 'utf8')), {
    packages: ['.'],
    overrides: expectedPins,
  });
});

test('pinning preserves workspace fields and unrelated existing overrides', async () => {
  const { dataRoot, path } = await fixture(`packages:
  - '.'
  - 'plugins/*'
onlyBuiltDependencies: []
injectWorkspacePackages: true
catalog:
  example: '^1.2.3'
overrides:
  'unrelated@<2': '2.0.0'
  marked: '18.2.0'
`);
  await applyPins(dataRoot);
  assert.deepEqual(loadWorkspace(await readFile(path, 'utf8')), {
    packages: ['.', 'plugins/*'],
    onlyBuiltDependencies: [],
    injectWorkspacePackages: true,
    catalog: { example: '^1.2.3' },
    overrides: { 'unrelated@<2': '2.0.0', ...expectedPins },
  });
});

test('an already pinned workspace is not rewritten', async () => {
  const original = `# Preserve an already configured workspace byte for byte.
packages: ['.']
overrides:
  '@linxin666/dsh-client-ui-community-plugins': '0.4.18'
  '@linxin666/dsh-client-ui-preset-center': '0.4.18'
  '@linxin666/dsh-client-ui-skin-center': '0.4.18'
  '@linxin666/dsh-pet': '0.4.18'
  marked: '18.2.0'
`;
  const { dataRoot, path } = await fixture(original);
  const fixedTime = new Date('2000-01-01T00:00:00Z');
  await utimes(path, fixedTime, fixedTime);
  const before = await stat(path);
  await applyPins(dataRoot);
  await applyPins(dataRoot);
  assert.equal(await readFile(path, 'utf8'), original);
  assert.equal((await stat(path)).mtimeMs, before.mtimeMs);
});

test('every conflicting community pin is rejected without changing the workspace', async t => {
  for (const name of Object.keys(expectedPins)) {
    await t.test(name, async () => {
      const original = `packages: ['.']\noverrides:\n  '${name}': '99.0.0'\n`;
      const { dataRoot, path } = await fixture(original);
      await assert.rejects(applyPins(dataRoot), /conflict/i);
      assert.equal(await readFile(path, 'utf8'), original);
    });
  }
});

test('more specific community selectors cannot bypass an already present exact pin', async t => {
  for (const selector of [
    'foo>marked',
    'marked@<19',
    'marked@>=18.0.0',
    ' foo@^1.0.0> marked@>=18 <19 ',
    '@scope/parent> @linxin666/dsh-client-ui-community-plugins',
    '@linxin666/dsh-client-ui-preset-center@^0.4.0',
    'foo>@linxin666/dsh-client-ui-skin-center@<0.5',
    '@scope/parent@^2> @linxin666/dsh-pet@^0.4.0',
    'foo\t>marked',
    ' \tmarked@<19 \t',
  ]) {
    await t.test(selector, async () => {
      const original = nativeRequire()('js-yaml').dump({
        packages: ['.'],
        overrides: { ...expectedPins, [selector]: '99.0.0' },
      });
      const { dataRoot, path } = await fixture(original);
      await assert.rejects(applyPins(dataRoot), /conflict/i);
      assert.equal(await readFile(path, 'utf8'), original);
    });
  }
});

test('matching specific selectors and unrelated child selectors are retained when adding pins', async () => {
  const original = `packages: ['.']
overrides:
  'foo> marked@>=18 <19': '18.2.0'
  '@scope/parent@^2> @linxin666/dsh-pet@^0.4.0': '0.4.18'
  'marked@>=18': '18.2.0'
  'marked> unrelated@<2': '2.0.0'
  '@linxin666/dsh-pet> unrelated': '3.0.0'
  'marked-extra': '99.0.0'
  '@linxin666/dsh-pet-tools': '99.0.0'
`;
  const { dataRoot, path } = await fixture(original);
  await applyPins(dataRoot);
  assert.deepEqual(loadWorkspace(await readFile(path, 'utf8')), {
    packages: ['.'],
    overrides: {
      'foo> marked@>=18 <19': '18.2.0',
      '@scope/parent@^2> @linxin666/dsh-pet@^0.4.0': '0.4.18',
      'marked@>=18': '18.2.0',
      'marked> unrelated@<2': '2.0.0',
      '@linxin666/dsh-pet> unrelated': '3.0.0',
      'marked-extra': '99.0.0',
      '@linxin666/dsh-pet-tools': '99.0.0',
      ...expectedPins,
    },
  });
});

test('invalid workspace or overrides structures are rejected without changing the file', async t => {
  for (const [label, original] of [
    ['empty workspace', ''],
    ['sequence workspace', '- packages\n'],
    ['scalar workspace', 'invalid\n'],
    ['null overrides', "packages: ['.']\noverrides: null\n"],
    ['sequence overrides', "packages: ['.']\noverrides: []\n"],
    ['scalar overrides', "packages: ['.']\noverrides: '18.2.0'\n"],
    ['nested override value', "packages: ['.']\noverrides:\n  unrelated: { version: '1.0.0' }\n"],
    ['numeric pin value', "packages: ['.']\noverrides:\n  marked: 18\n"],
    ['malformed YAML', "packages: ['.'\n"],
  ]) {
    await t.test(label, async () => {
      const { dataRoot, path } = await fixture(original);
      await assert.rejects(applyPins(dataRoot), /workspace|overrides|YAML/i);
      assert.equal(await readFile(path, 'utf8'), original);
    });
  }
});
