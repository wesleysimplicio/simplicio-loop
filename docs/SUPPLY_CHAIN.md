# Supply chain — what's real, what's blocked (#292)

Issue #292 asks for CI-attested checksums, SBOM, provenance, keyless/OIDC signing, and clean-room
install smokes against real registries. `.github/workflows/` was removed repo-wide in PR #311
(GitHub Actions billing lockout); there is currently no CI substrate in this repository capable of
holding an OIDC token or running a build-once job. This document is the honest scope line: what
this repo can prove **today**, locally, and what genuinely requires CI/OIDC/a registry publish
that doesn't exist here.

## Implemented now (real, tested, run-today)

| Tool | Command | Proves |
|---|---|---|
| `scripts/release_verify.py checksums-generate` / `checksums-verify` | `python3 scripts/release_verify.py checksums-generate --dir dist --output dist/SHA256SUMS.json` | SHA-256 + size for every artifact in a directory; `verify` fails closed on any missing file, tampered digest, or undeclared extra file. |
| `scripts/sbom_generate.py generate` | `python3 scripts/sbom_generate.py generate --artifact dist/<wheel>` | A CycloneDX-shaped SBOM built from `pyproject.toml` direct dependencies + `importlib.metadata` (resolved version/license where installed), the current `git rev-parse HEAD`, and (optionally) the sha256 of a real artifact on disk. Unresolved components are labeled, not silently dropped. |
| `scripts/release_verify.py sign` / `verify-signature` | `python3 scripts/release_verify.py sign --file dist/SHA256SUMS.json` | Detached gpg signing of the checksum manifest **if and only if** `gpg` is installed AND a usable secret key is configured. If either is missing it exits non-zero with `blocked: true` and a plain-text reason — it never fabricates a signature. |
| `scripts/install_smoke.py run` | `python3 scripts/install_smoke.py run --expected-version X.Y.Z` | Builds a real wheel (`python -m build --wheel --no-isolation`), creates a disposable venv, installs *only* that wheel (`--no-deps`, `--no-index`) into it, and proves — with `PYTHONPATH` cleared — that the imported module resolves to the venv's `site-packages`, not this repo checkout, and that `importlib.metadata.version()` matches. |

Run all four in a `dist/` workflow:

```bash
python3 -m build --wheel --no-isolation --outdir dist
python3 scripts/release_verify.py checksums-generate --dir dist --output dist/SHA256SUMS.json
python3 scripts/sbom_generate.py generate --artifact dist/simplicio_loop-*.whl --output dist/sbom.json
python3 scripts/release_verify.py sign --file dist/SHA256SUMS.json    # blocks without a gpg key
python3 scripts/install_smoke.py run --expected-version "$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
```

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
- **`--no-deps` install.** `simplicio-loop`'s one runtime dependency (`simplicio-cli`) is not
  vendored for offline install in this environment, so the smoke installs the wheel with
  `--no-deps`. This proves packaging/import correctness, not the full dependency closure. The
  receipt's `install.no_deps: true` field makes this explicit rather than silently narrowing scope.

## Relationship to `source_state.py`

`simplicio_loop/source_state.py` already defaults `checksums_verified`, `signatures_verified`,
`sbom_present`, and `install_smoke.passed` to `false` and requires a real gate to flip them — it
does not fabricate `true`. This was true before this change (verified by reading the source) and
remains the contract: none of the local tools above are wired to flip those fields, because they
prove a **local build**, not the **CI-attested + registry-published** state those fields are meant
to represent. Wiring that up is part of the still-blocked Fase 9 and requires Fases 3/5/6 to exist
first.

## Bottom line

Fase 4 (checksums/SBOM/signature) and Fase 7 (install smoke) now have real, tested, locally
achievable implementations covering everything that does not require a CI substrate. Fases 2, 3,
5, 6, 8, and 9 remain genuinely blocked on GitHub Actions billing being restored or an equivalent
CI/OIDC substrate landing — see docs/RELEASE.md for the phase-by-phase reasoning.
