# Maintenance-deferred / backlog-only mode

Use this mode when a control-plane substrate is frozen for maintenance but the broader loop must keep making progress. It converts discovered correction work into a durable receipt and leaves mutation for a later maintenance window.

## Example

```powershell
python -m simplicio_loop.cli run --repo . --task task.md
python -m simplicio_loop.cli maintenance-deferred --repo . <run-id> `
  --mode maintenance_deferred `
  --disposition backlog_only `
  --correction-summary "Runtime scheduler correction discovered" `
  --deferral-reason "Active control-plane run is frozen" `
  --resume-instruction "Re-arm from maintenance-receipt.json" `
  --resume-instruction "Run the operator after the maintenance window"
```

The command writes `.orchestrator/runs/<run-id>/maintenance-receipt.json` with the correction, reason, resume instructions, timestamp, and evidence status. It sets `completion.ready=false`, marks the operator `backlog_only`, and advances `next_action=resume_from_maintenance_receipt`. No mutation operator is invoked and a completion promise remains rejected until the normal evidence and watcher gates pass.

Use normal converge/operate mode when the control plane is writable and the selected change is safe to apply. Use backlog-only only when the deferral reason is explicit and the receipt can be handed to a later maintenance run.
