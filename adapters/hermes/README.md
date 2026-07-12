# Hermes adapter (legacy alias)

**This is a legacy alias.** `hermes` was renamed to `simplicio_agent` as the canonical adapter
ID — see [../simplicio_agent/README.md](../simplicio_agent/README.md) for the full adapter
documentation. Treat `hermes` exactly as `simplicio_agent`: same native bindings, same install
contract, same loop behavior; only the id/binary/config-path names differ during the compat
window.

```bash
bash scripts/install.sh hermes          # still works — installs identically to simplicio_agent
```

Kept only so existing installs, saved scripts, and `HERMES_PROFILE` env usage keep working
without a breaking change. It will be removed once the deprecation threshold in
`adapters/MATRIX.md` is reached — migrate to `simplicio_agent` (binary `simplicio-agent`, config
`~/.simplicio-agent/config.yaml`) when convenient.
