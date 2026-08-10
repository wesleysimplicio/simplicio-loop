# LLM Operating Instructions

This is the active English instruction surface for Simplicio Loop and its Prism route.
Read it before planning, implementation, validation, release, or handoff.

## Language and contracts

- Write new instructions, route explanations, receipts, plans, and active documentation in English.
- Treat `language: en` and `instruction_language: en` in the Prism catalog and route envelope as the canonical language contract.
- Keep public schemas and code identifiers stable unless the task explicitly changes a contract.
- Preserve historical changelogs and localization/reference material; do not treat them as active instructions.

## Ownership

- Mapper surveys the repository and selects bounded context.
- Fast accelerates indexed retrieval; it is never the source of truth.
- Dev CLI owns deterministic edits and verification.
- Loop owns orchestration, slots, retries, convergence, and completion evidence.
- Runtime is optional for ordinary Loop orchestration and owns governed execution, checkpoints, receipts, and reconciliation when required.

## Required workflow

1. Pin repository, revision, scope, and the completion oracle.
2. Route the request through Prism and load only the listed skills.
3. Survey with Mapper; use Fast for broad or repeated retrieval.
4. Plan bounded work in Loop; keep one task per slot and retry only under the contract.
5. Apply changes through Dev CLI and run the smallest relevant tests.
6. Record evidence, review the diff, and close only when the completion oracle is proven.

## Safety rules

- Prefer the simplest end-to-end implementation and avoid speculative abstraction.
- Do not add compatibility layers or fallbacks for obsolete behavior without explicit approval.
- Stop on unknown revision, scope, dependency, or mutation authority.
- Never claim a test, release, or merge without durable evidence.
