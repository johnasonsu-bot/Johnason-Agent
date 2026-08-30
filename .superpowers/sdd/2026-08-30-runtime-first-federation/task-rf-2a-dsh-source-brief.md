# Task RF-2A-D Brief — DeepSeek Harness source readiness

## Objective

Turn the pinned `third_party/deepseek-harness` gitlink into verifiable, reproducible source/build input without yet implementing the Host adapter or claiming runtime readiness.

## Requirements

1. Verify exact revision `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`, clean gitlink, `pnpm-lock.yaml`, workspace/package manifests, engine/package-manager declaration, patches and license/third-party notices.
2. Generate a canonical source/build manifest recording submodule path/url/revision, lock/engine/license/patch digests, selected sidecar package/entrypoint, supported target inputs and frozen build command.
3. The verifier fails on missing submodule, wrong revision, dirty source, lock/workspace/license/patch drift or non-canonical manifest.
4. Dependency preparation and release build are separate. Release command uses the frozen lock and may not download plugins or scan user plugin directories.
5. Output `GO_DSH_SOURCE_READY` only for source/build provenance. It must not claim DSH Host/Provider/Plugin gates.

## Ownership

- New DSH-specific source manifest/verifier/script/tests only.
- Do not modify `.gitmodules`, the submodule contents, shared assignment/router/supervisor/provider files, Python Term gate or UI.

## Verification

- RED/GREEN source verifier tests;
- read-only package/workspace metadata check where locally available;
- diff/compile/manifest consistency only. Security review is deferred to the final security phase.
