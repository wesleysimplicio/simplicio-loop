# Supply chain — what's real, what's blocked (#292)

Issue #292 asks for CI-attested checksums, SBOM, provenance, keyless/OIDC signing, and clean-room
install smokes against real registries. This repository contains GitHub workflow files, but they
are not a required gate or evidence source for this work and were not executed here. This document
is the honest scope line: what this repo can prove **today**, locally, and what genuinely requires
CI/OIDC/a registry publish beyond the local evidence available here.

## Implemented now (real, tested, run-today)

| Tool | Command | Proves |
|---|---|---|
| `scripts/release_verify.py checksums-generate` / `checksums-verify` | `python3 scripts/release_verify.py checksums-generate --dir dist --output dist/SHA256SUMS.json` | SHA-256 + size for every artifact in a directory; `verify` fails closed on any missing file, tampered digest, or undeclared extra file. |
| `scripts/sbom_generate.py generate` | `python3 scripts/sbom_generate.py generate --artifact dist/<wheel>` | A CycloneDX-shaped SBOM built from `pyproject.toml` direct dependencies + `importlib.metadata` (resolved version/license where installed), the current `git rev-parse HEAD`, and (optionally) the sha256 of a real artifact on disk. Unresolved components are labeled, not silently dropped. |
| `scripts/release_verify.py sign` / `verify-signature` | `python3 scripts/release_verify.py sign --file dist/SHA256SUMS.json` | Detached gpg signing of the checksum manifest **if and only if** `gpg` is installed AND a usable secret key is configured. If either is missing it exits non-zero with `blocked: true` and a plain-text reason — it never fabricates a signature. |
| `scripts/install_smoke.py run` | `python3 scripts/install_smoke.py run --expected-version X.Y.Z` | Builds a real wheel (`python -m build --wheel --no-isolation`), creates a disposable venv, installs *only* that wheel (`--no-deps`, `--no-index`) into it, and proves — with `PYTHONPATH` cleared — that the imported module resolves to the venv's `site-packages`, not this repo checkout, and that `importlib.metadata.version()` matches. |
| `scripts/provenance_generate.py generate` | `python3 scripts/provenance_generate.py generate --artifact dist/<wheel>` | A locally-signable in-toto/SLSA-shaped provenance statement (see "Provenance without OIDC" below) — real git commit SHA, real artifact digest, real build invocation, explicitly `ci_attested: false` / `oidc: false` / `builder_identity: "local-machine"`. |
| `scripts/release_rehearsal.py run` | `python3 scripts/release_rehearsal.py run --repo .` | Chains every row above, in order, against a disposable `git archive` scratch copy of `HEAD`: version-bump → build → checksum → sign (best-effort) → SBOM → provenance → clean-room install-smoke. Proves the local pipeline composes end-to-end, not just that each tool works alone. Never mutates the real repo's version files, never tags, never publishes. |

Run the whole chain in one command:

```bash
python3 scripts/release_rehearsal.py run --repo .
```

or step by step in a `dist/` workflow:

```bash
python3 -m build --wheel --no-isolation --outdir dist
python3 scripts/release_verify.py checksums-generate --dir dist --output dist/SHA256SUMS.json
python3 scripts/sbom_generate.py generate --artifact dist/simplicio_loop-*.whl --output dist/sbom.json
python3 scripts/release_verify.py sign --file dist/SHA256SUMS.json    # blocks without a gpg key
python3 scripts/provenance_generate.py generate --artifact dist/simplicio_loop-*.whl --output dist/provenance.json
python3 scripts/install_smoke.py run --expected-version "$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
```

## Provenance without OIDC

Issue #292 Fase 4 §3 asks for provenance "assinada por identidade OIDC do workflow" — signed by a
CI workflow's OIDC identity. OIDC is *one* mechanism for getting provenance, not the only one: a
locally-signed, structured provenance statement built from real git/build metadata is itself a
verifiable claim, just rooted in a different (weaker) trust anchor — a human-controlled gpg key
instead of a CI-issued short-lived Sigstore certificate. `scripts/provenance_generate.py` produces
such a statement (in-toto `Statement/v1` shape, `predicateType`
`https://slsa.dev/provenance/v1`): subject = artifact name + sha256; `predicate.builder.id` is
hardcoded to the string `"local-machine"` (never spoofs a CI runner identity); `predicate.invocation`
carries the real git remote + commit SHA + build command; `predicate.metadata.generatedAt` is a
real UTC timestamp. It is signed the same best-effort, fail-closed way `release_verify.py sign`
signs the checksum manifest — detached gpg, blocked (not fabricated) without a configured key.

What this explicitly does NOT claim: an OIDC-rooted identity, an immutable CI-runner log, or a
Sigstore/Rekor transparency-log entry. The statement's own `ci_attested`/`oidc` fields say so, and
`docs/RELEASE.md` tracks OIDC/Trusted-Publishing-rooted provenance as still blocked under Fase 5.

## Local rehearsal (Fase 6, local subset)

`scripts/release_rehearsal.py run` is the answer to "does the whole local chain actually work
together, not just each piece in isolation": it `git archive`s the tracked tree at `HEAD` into a
scratch directory (never the working tree, so uncommitted/ignored files can't leak in), bumps the
version there only (default: a `+rehearsalNNNN` PEP 440 local-version label that can never collide
with a real release version), builds a real wheel, checksums/signs/SBOMs/provenance-statements it,
and clean-room install-smokes it — a full `planned → built → checksummed → signed|sign_blocked →
sbom → provenance → smoke-verified` state machine in one receipt. It never edits the real repo's
version files, never creates a tag, and never talks to PyPI/npm/GitHub. This is the honest,
currently-achievable subset of Fase 6 — the actual "publish the same bytes to every registry" claim
still needs a real publish target, which does not exist here.

## Explicitly NOT claimed (and why)

- **CI attestation / provenance.** Every artifact above is generated **locally**, on a developer
  machine, not inside a CI job. The SBOM and checksum manifest both carry
  `"generated_locally": true` / `"ci_attested": false` so a downstream consumer can't mistake this
  for a build-once CI attestation. Real provenance (repository, workflow, runner identity, build
  parameters) requires a CI job to exist; none does right now.
- **OIDC / Sigstore keyless signing.** No `cosign`/`sigstore` tooling is installed in this
  environment, and OIDC federation is, by construction, minted by a CI identity provider
  (GitHub Actions' `id-token: write` claims) — there is no local-machine equivalent. `gpg`
  detached-signing is used instead where a key is actually configured; it is a strictly weaker
  substitute and is documented as such, not conflated with Fase 5's OIDC requirement.
- **Registry install smoke (PyPI / npm / GitHub Release).** `install_smoke.py` only proves the
  local-build clean-room contract (fresh venv, `--no-deps`, isolation from the checkout). It does
  NOT install from `pypi.org`, `registry.npmjs.org`, or a GitHub Release asset, because this
  change does not publish anywhere — doing so would require the very OIDC/build-once pipeline
  that's blocked. Faking a "PyPI smoke" against an index nothing was published to would itself be
  the kind of fabricated proof issue #292 is complaining about, so it isn't done.
- **`--no-deps` install.** `simplicio-loop`'s runtime dependencies (including the direct
  `simplicio-cli` and `simplicio-mapper` operator distributions) are not vendored for offline
  install in this environment, so the smoke installs the wheel with
  `--no-deps`. This proves packaging/import correctness, not the full dependency closure. The
  receipt's `install.no_deps: true` field makes this explicit rather than silently narrowing scope.

## Relationship to `source_state.py`

`simplicio_loop/source_state.py` still defaults `checksums_verified`, `signatures_verified`,
`sbom_present`, and `install_smoke.passed` to `false` and requires a real gate to flip them — it
does not fabricate `true`. Re-confirmed correct in this round (verified by reading the source, not
assumed): those fields only flip when `_should_verify_release_artifacts(...)` is true, and even
then they're populated from `simplicio_loop/external_verifiers.py::verify_release`, which for a
real tagged GitHub Release: downloads the actual release assets, recomputes SHA-256 over the
downloaded bytes, attempts `gh attestation verify`, parses an attached SBOM asset if present, and
install-smokes the downloaded wheel in a throwaway venv (`external_verifiers.run_install_smoke`).
That is a genuinely byte-level, registry-side verification for the **GitHub Release** leg — more
than earlier rounds of this issue credited. What remains blocked is the equivalent for the
**PyPI** and **npm** legs, because nothing has ever been published to either from this repo; that
still needs Fases 3/5/6 (an actual publish pipeline) to exist first.

## Bottom line

Fase 4 (checksums/SBOM/signature/provenance) and Fase 7 (install smoke) have real, tested, locally
achievable implementations covering everything that does not require a CI substrate.
`scripts/release_rehearsal.py` additionally proves the local subset of Fase 6 — the whole chain
composing end-to-end against a disposable scratch copy, without publishing anywhere. The
GitHub-Release leg of Fase 7/9 already does real byte-level, registry-side verification via
`simplicio_loop/external_verifiers.py`. Fases 2, 3, 5, 8, and the PyPI/npm legs of 6/9 remain
genuinely blocked on GitHub Actions billing being restored or an equivalent CI/OIDC substrate
landing — see docs/RELEASE.md for the phase-by-phase reasoning, and
`docs/adr/0004-release-oidc-trusted-publishing-permanently-blocked.md` for the formal, signed-off
ADR freezing that as a permanent structural blocker (not an open judgment call) until a CI
identity provider exists in this repository again.
