# ADR-0003 — frozen attestation/SBOM policy for delivery-truth release gates (#290)

- **Status:** accepted
- **Date:** 2026-07-15
- **Supersedes / relates to:** #290 (delivery-truth fail-closed verification), #292 (supply-chain
  Fase 4/5, which this ADR narrows from "blocked pending CI" to "frozen local-substitute
  policy"), `simplicio_loop/external_verifiers.py`'s `AttestationVerifier`/`SbomVerifier` slice,
  `scripts/sbom_generate.py`, `scripts/provenance_generate.py`.

## Context

Issue #290 lists, among the "Decisões que precisam ser congeladas no planejamento":

> formatos de assinatura/attestation e SBOM aceitos

and the release-artifact gate (`verify_release_artifacts` in `simplicio_loop/external_verifiers.py`,
Fase 4) is fail-closed on `signature_reason_code`/`sbom_reason_code` whenever no attestation or
SBOM asset is present or parseable on the release. Left undecided, this gate stays permanently
`UNVERIFIED` for every release this repo publishes, because the ideal described in #290/#292 —
OIDC-signed, CI-attested provenance and a build-pipeline-generated SBOM — requires a CI
substrate this repo does not currently have: the two current workflows
(`simplicio-status-sync.yml` and `windows-progress-smoke.yml`) do not provide OIDC or a release
gate and were not used as evidence for this work (see `docs/SUPPLY_CHAIN.md`), and there is no
alternative CI runner configured for attested release builds.

Two scripts already exist and are exercised by their own test suites
(`tests/test_sbom_generate.py`, and the provenance script's own tests) that produce real,
non-fabricated artifacts from what a local machine actually has access to:

- `scripts/sbom_generate.py` — a real, deterministic CycloneDX-shaped SBOM built from
  `pyproject.toml` declared dependencies, `importlib.metadata`-resolved installed versions and
  licenses, the current `git rev-parse HEAD`, and (optionally) the SHA-256 digest of a real build
  artifact on disk. It explicitly does not claim to be a full transitive-dependency resolver or a
  CI-attested SBOM — it says so in its own docstring and in the SBOM's own metadata.
- `scripts/provenance_generate.py` — an in-toto/SLSA-provenance-shaped statement signed with a
  local gpg key (the same key `scripts/release_verify.py sign` uses for checksum manifests),
  carrying `builder_identity: "local-machine"`, `ci_attested: false`, `oidc: false`, the real git
  commit/remote the build ran from, and the real artifact digest. It explicitly refuses to
  fabricate an OIDC identity or claim CI provenance it cannot produce.

Both scripts are real and runnable today; neither is a placeholder. The open question is purely
policy: **does #290's `AttestationVerifier`/`SbomVerifier` accept these local-substitute
artifacts as satisfying the release gate, or does the gate stay permanently blocked until a CI
substrate exists?**

## Decision

**Accept the local-substitute artifacts as the frozen policy for this repo's release gate, with
the trust root explicitly downgraded and labeled — never silently upgraded to look like CI
attestation.**

Concretely:

1. **SBOM** — a release satisfies `sbom_present`/`SbomVerifier` when it carries an asset produced
   by `scripts/sbom_generate.py` (schema `simplicio.sbom/v1`, CycloneDX spec version 1.5), linked
   to the same artifact digest the checksum gate already verified. `verify_release_artifacts`
   today already accepts any well-formed SPDX (`spdxVersion` present) or CycloneDX
   (`bomFormat == "cyclonedx"`) document as `sbom_present=True` — this ADR confirms
   `sbom_generate.py`'s CycloneDX output is the accepted producer for this repo's releases, and
   freezes the format acceptance as intentional policy rather than an open question. Tightening
   the check to additionally cross-verify a component digest against the bytes this same
   verification pass downloaded (rather than trusting the SBOM's self-reported digest) is a
   follow-up hardening, tracked as a `SbomVerifier` enhancement, not required to close this
   policy decision.
2. **Attestation/signature** — a release satisfies `signatures_verified`/`AttestationVerifier`
   when it carries a provenance statement produced by `scripts/provenance_generate.py`
   (`predicate_type: https://slsa.dev/provenance/v1`, `simplicio.provenance-lite/v1`), gpg-signed,
   verifiable with `gpg --verify` against a known, pinned maintainer key fingerprint (not "any
   valid signature" — pinned the same way `scripts/release_verify.py verify` already pins the
   checksum-manifest signer). `gh attestation verify` (the OIDC/Sigstore-backed check) remains the
   **preferred** path — `verify_release_artifacts` already tries it first — and the local
   provenance statement is the fallback this ADR authorizes when no Sigstore-backed attestation
   exists, exactly the situation this repo is in without a CI substrate. **Implementation status:**
   as of this ADR, `verify_release_artifacts` only tries `gh attestation verify`; wiring the
   `provenance_generate.py`-statement fallback into the same function (checking for a
   `.intoto.jsonl`/`.att`-suffixed asset, verifying its gpg signature against the pinned
   fingerprint, and confirming its subject digest matches a downloaded artifact) is the concrete
   follow-up code change this decision authorizes and unblocks — the policy question ("is a local
   provenance statement an acceptable substitute at all") is what was open and is now settled;
   wiring it is ordinary implementation work against an already-decided policy.
3. **Trust-level labeling is mandatory, not cosmetic.** Every receipt produced under this policy
   carries `builder_identity: "local-machine"` and `ci_attested: false` verbatim from the
   provenance statement into the verifier receipt. `DeliveryEvidenceSet`/`delivery-receipt.json`
   consumers (the completion oracle, PR evidence assembly) MUST surface this trust level
   alongside `PASS` — a reviewer reading a `released` receipt must be able to see at a glance
   that the attestation is a local-provenance substitute, not a CI-issued one. This is not a
   downgrade of the `PASS` verdict itself (the bytes are still genuinely checksum- and
   digest-verified); it is an honest label on *how* the signature/provenance claim was rooted.
4. **This policy is revisited, not permanent.** If/when a CI substrate exists again for this
   repo (a workflow with the required OIDC/release controls, or an equivalent runner), the
   OIDC/Sigstore path becomes the only accepted one for new releases and this ADR's
   local-substitute path is marked superseded — existing releases already verified under this
   ADR keep their receipts (append-only; never rewritten) but new releases stop qualifying via
   the local path.

## Consequences

- `verify_release_artifacts`'s `sbom_reason_code` gate is already satisfiable today by attaching
  `sbom_generate.py`'s CycloneDX output to a release — no code change needed for that half of
  #290's Fase 4 acceptance criterion.
- `signature_reason_code` remains `attestation_not_found`/`attestation_check_unavailable` until
  the `provenance_generate.py`-statement fallback described above is actually wired into
  `verify_release_artifacts` — that wiring is now unblocked (the policy question is answered) and
  is tracked as ordinary follow-up implementation work, not a standing open decision.
- A release published without either script's output still fails closed with the existing
  `sbom_asset_absent` / `attestation_not_found` reason codes — this ADR authorizes accepting a
  specific weaker trust root once implemented, not skipping the check.
- Any future maintainer reading a `released`/`deployed` receipt can distinguish a CI-attested
  release from a local-provenance one purely from the receipt's own `builder_identity`/
  `ci_attested` fields, with no separate lookup required, once the fallback above is wired in.
