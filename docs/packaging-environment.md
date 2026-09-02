# Packaging environment boundary

`simplicio-loop` is installed as an independent Python distribution. Its supported
closure contains Loop, Simplicio CLI, Mapper, Fast, and the declared optional
extras in `pyproject.toml`. It does **not** install `hermes-agent` or
`simplicio-sprint` and does not declare `rich` directly.

## Supported installation

Use one virtual environment per product boundary:

```bash
python3.11 -m venv .venv-loop
. .venv-loop/bin/activate
python -m pip install --upgrade pip
python -m pip install .
python -m pip check
python -m pytest -q
```

Loop is the aggregate distribution for its two mandatory operators. A normal
`pip install simplicio-loop` declares both `simplicio-mapper` and `simplicio-cli`
directly; `simplicio-cli` provides the `simplicio-dev-cli` entrypoint. Verify the
installed bundle, including dependency ownership and PATH entrypoints, with:

```bash
simplicio-loop-stack --check
simplicio-loop preflight --repo . --strict --json
```

The check is expected to fail for `pip install --no-deps`, a manually removed
operator, or an environment where the console scripts are not on `PATH`.
Runtime and other accelerators remain separate optional components.

Hermes Agent and Simplicio Sprint are separate consumers. Do not install both
into the Loop environment. Their currently published requirements are
incompatible (`hermes-agent` requires `rich==14.3.3`, while `simplicio-sprint`
requires `rich>=15.0.0`), so a combined environment is intentionally outside
the supported boundary and must fail the packaging gate rather than silently
select a pin.

The boundary is deterministic: dependency ownership is declared by each
project's own manifest, and Loop must not add a compatibility pin for a package
it does not import. The executable verification in
`tests/test_packaging_environment.py` prevents an accidental direct dependency
on Hermes, Sprint, or `rich` from entering the Loop distribution.
