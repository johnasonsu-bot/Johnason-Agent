import { copyFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const outputRoot = resolve(repositoryRoot, 'docs/.vitepress/dist')

const artifacts = [
  'docs/analysis/2026-08-10-dintal-claw-gap-graph.html',
  'docs/analysis/2026-08-10-dintal-complete-snapshot-gap-graph.html',
  'docs/operations/project-operation-knowledge-graph.html',
]

for (const source of artifacts) {
  const relativePath = source.replace(/^docs\//, '')
  const destination = resolve(outputRoot, relativePath)
  await mkdir(dirname(destination), { recursive: true })
  await copyFile(resolve(repositoryRoot, source), destination)
}
