import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
if (process.platform !== 'darwin') {
  process.stderr.write('This local subscriptions build requires macOS Keychain; no plaintext fallback is available.\n');
  process.exitCode = 1;
} else {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const result = spawnSync('/usr/bin/clang', ['-O2', '-Wall', '-Wextra', '-Wno-deprecated-declarations',
    '-framework', 'Security', '-framework', 'CoreFoundation',
    'native/keychain-helper.c', '-o', 'native/keychain-helper'], { cwd: root, stdio: 'inherit' });
  process.exitCode = result.status ?? 1;
}
