# Task RF-2A-G Brief — Goose source readiness

## Objective

Turn the pinned `third_party/goose` gitlink into verifiable, reproducible source/build input without yet implementing the Host adapter or claiming runtime readiness.

## Requirements

1. Verify exact revision `d9d08f0e051531e921f561fcb77aa0ed589e9de9`, clean gitlink, `Cargo.lock`, workspace manifest, `rust-toolchain.toml` and license files.
2. Generate a canonical source/build manifest that records submodule path/url/revision, lock/toolchain/license digests, selected sidecar crate/package, supported target inputs and frozen build command.
3. The verifier fails on missing submodule, wrong revision, dirty source, lock/toolchain/license drift or non-canonical manifest.
4. Build preparation and release build are separate. Release command must use the pinned toolchain and locked dependencies; do not silently update lockfiles.
5. Output `GO_GOOSE_SOURCE_READY` only for source/build provenance. It must not claim Goose Host/Provider/Query gates.

## Ownership

- New Goose-specific source manifest/verifier/script/tests only.
- Do not modify `.gitmodules`, the submodule contents, shared assignment/router/supervisor/provider files, Python Term gate or UI.

## Verification

- RED/GREEN source verifier tests;
- read-only cargo metadata/build-input check where locally available;
- diff/compile/manifest consistency only. Security review is deferred to the final security phase.
