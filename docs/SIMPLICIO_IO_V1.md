# Simplicio public I/O

Loop is the coordinator and owns only `run`: it dispatches `understand`,
`search`, `change` and `verify`, then converges their evidence. All public
messages use `simplicio.io/v1`; component-specific receipts remain internal.
Consumers must rely on the envelope fields, never Fast offsets or producer
private filenames. Breaking changes require a new major envelope version.

