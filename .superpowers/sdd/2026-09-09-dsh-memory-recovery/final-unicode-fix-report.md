# Unicode continuation correction

Only the scoped page-range Unicode defect is changed; the first four approved
final-review fixes are untouched.

Root cause: a UTF-16 slice can end/start with a lone surrogate. Returning it as
SQLite UTF-8 TEXT replaces that code unit with U+FFFD before JavaScript reads it.
Both direct real SQLite paging and the actual native Web HTTP route reproduced
`A😀B` becoming `A��B` when pages split between the surrogate pair.

Fix: the SQLite UDF returns `JSON.stringify(slice)`, escaping lone surrogates
before crossing the TEXT boundary. `readPage` parses that transport JSON and
computes continuation offsets from the restored string's UTF-16 length. The
external API, fixed version, offset units and bounds are unchanged. HTTP's
existing JSON response serialization preserves both halves as escaped JSON.

Verification (Node `/Users/sushi/.nvm/versions/node/v22.20.0/bin/node`):

- RED: direct SQLite and real HTTP tests both observed replacement characters.
- GREEN: the same two targeted tests pass; joined pages exactly equal original
  canonical content, and the direct test verifies code units D83D / DE00.
- Full app: `node --test apps/dsh-agent/tests/*.test.mjs` — 111/111 passed,
  0 failed, 13.6 seconds.
- HTTP GREEN fixture retained: `/Users/sushi/dsh-memory-web-16Af3R`;
  latest full-suite HTTP fixture: `/Users/sushi/dsh-memory-web-a1Zd4y`.

No model request, third-party edit, deletion, old Vault/session operation, or
existing service stop. README and concurrent acceptance-document changes are
preserved and excluded from this commit.
