# Release process (#292)

This document tracks what part of issue #292's release pipeline is real today, and what remains
blocked. It exists so nobody has to reconstruct that history from the issue thread.

## Current, mechanical steps

1. **Version bump — one command, one PR.**

   ```bash
   python3 scripts/version_sync.py check                 # fails on any drift
   python3 scripts/version_sync.py apply --version X.Y.Z # rewrites every derived surface
   python3 scripts/version_sync.py manifest --json        # same shape as release_manifest.py
   ```

   `scripts/version_sync.py` (#292 Fase 1, shipped in PR #328) keeps `pyproject.toml`,
   `packaging/npm/package.json`, `.cursor-plugin/plugin.json`, and the `simplicio_loop/__init__.py`
   fallback in lockstep. `scripts/release_manifest.py` is the underlying parity gate; run it (or
   `version_sync.py check`) before opening a release PR.

2. **Local supply-chain artifacts** (this change — see docs/SUPPLY_CHAIN.md for full scope/limits):

   ```bash
   python3 scripts/install_smoke.py run --expected-version X.Y.Z   # build + clean-room install
   python3 scripts/release_verify.py checksums-generate --dir dist --output dist/SHA256SUMS.json
   python3 scripts/release_verify.py checksums-verify --dir dist --manifest dist/SHA256SUMS.json
   python3 scripts/sbom_generate.py generate --artifact dist/<wheel> --output dist/sbom.json
   python3 scripts/release_verify.py sign --file dist/SHA256SUMS.json   # blocks if no gpg key
   ```

   These are real, run-today commands. None of them require CI, a registry, or network access
   beyond what's already installed locally.

3. **Publish.** PyPI publishing is still the pre-#292 manual/token-based flow described in the
   issue's "Fluxo atual problemático" section. It has NOT been migrated to OIDC/Trusted
   Publishing, and the automatic build-once pipeline (tag → build → attest → publish → verify)
   has NOT been implemented. See "What remains blocked" below for why.

## What remains blocked, and why

`.github/workflows/` was removed repo-wide in PR #311 after a GitHub Actions billing lockout.
Per this repo's `CLAUDE.md`, CI is being centralized around `simplicio-runtime` instead of GitHub
Actions, but that replacement CI substrate does not exist yet in this repository. Issue #292's
Fases 2, 3, 5, 6, 8, and most of 9 are written against a GitHub-Actions-shaped pipeline
specifically:

- Fase 2 (release governance) assumes a required-status-check + protected `release` environment
  model that is a GitHub Actions/branch-protection feature.
- Fase 3 (build-once) assumes a dedicated CI job (`build-release-artifacts`) with a fixed runner
  image and `SOURCE_DATE_EPOCH` control — meaningless without a CI runner to execute it on.
- Fase 5 (OIDC/Trusted Publishing) is **inherently CI-specific**: PyPI/npm Trusted Publishing
  issues short-lived tokens to an OIDC identity minted BY a CI job (`repository`, `workflow`,
  `environment` claims) — there is no such thing as "OIDC from a local machine." This phase
  cannot be satisfied by any local script, by construction, not just for lack of tooling.
- Fase 6 (publish same bytes to each registry) needs an actual publish target to compare against;
  none of PyPI, npm, or GitHub Releases has been published to as part of this change.
- Fase 8 (idempotent partial-failure recovery across registries) needs Fase 3/5/6 to exist first.
- Fase 9 (`source_state`/delivery reconciliation on real receipts) can only consume real registry
  receipts once Fases 5-7 exist; `simplicio_loop/source_state.py` already defaults
  `checksums_verified`/`signatures_verified`/`sbom_present`/`install_smoke.passed` to `false` and
  does not fabricate them (this was already correct going into this change — verified by reading
  the source, not assumed).

**Judgment call:** rather than write GitHub Actions YAML that cannot run (this repo's Actions are
billing-locked) or claim OIDC/Sigstore coverage that doesn't exist, this change implements the
platform-agnostic subset of Fase 4 and Fase 7 as real, tested, local CLI tools (see
docs/SUPPLY_CHAIN.md), and leaves Fases 2/3/5/6/8/9 explicitly open pending either (a) GitHub
Actions billing being restored, or (b) `simplicio-runtime`'s replacement CI substrate landing with
OIDC-equivalent capability. Issue #292 stays open; it is not closeable under its own Definition of
Done today.
