"""Hookwall ledger facade backed by MapperStore operations and events."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .hookwall_gate import HookwallBlocked, validate_pre_decision, verify_post_receipt
from .mapper_operations import MapperOperationsAdapter, MapperOperationsError
from .mapper_run_journal import MapperJournalError, MapperRunJournal
from .remote_queue import QueueConflict

SCHEMA = "simplicio.hookwall-ledger/v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class MapperHookwallEffectLedger:
    """Run Hookwall's state machine on MapperStore's effect authority.

    ``effect_confirmed`` records observation only.  The Mapper effect is
    committed only after the post receipt validates, so a failed post gate can
    still transition the prepared effect to ``unknown`` without synthesizing a
    receipt.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        operations: MapperOperationsAdapter | None = None,
        auto_create: bool = False,
    ) -> None:
        self.database = Path(database).expanduser().absolute()
        self.operations = operations or MapperOperationsAdapter(
            self.database, auto_create=auto_create
        )
        self.journal = MapperRunJournal(
            self.database, adapter=self.operations, auto_create=auto_create
        )

    @staticmethod
    def _run_id(envelope: Mapping[str, Any]) -> str:
        del envelope
        return "hookwall-ledger"

    @staticmethod
    def _effect_id(key: str) -> str:
        return "hookwall:" + key

    @staticmethod
    def _identity(envelope: Mapping[str, Any]) -> tuple[str, str]:
        attempt_id = str(envelope.get("attempt_id") or "").strip()
        fence = str(envelope.get("fence") or "").strip()
        if not attempt_id or not fence:
            raise HookwallBlocked(
                "mapper_attempt_id_missing",
                "Mapper-backed Hookwall requires attempt_id and fence",
            )
        return attempt_id, fence

    def _events(self, envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self.journal.events(self._run_id(envelope))

    def _state_event(
        self, envelope: Mapping[str, Any], key: str
    ) -> dict[str, Any] | None:
        found = None
        for event in self._events(envelope):
            payload = event.get("payload") or {}
            if payload.get("idempotency_key") == key:
                found = event
        return found

    def _append(
        self,
        envelope: Mapping[str, Any],
        event_type: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        run_id = self._run_id(envelope)
        if not self.journal.events(run_id):
            self.journal.append(
                run_id,
                "run_started",
                {"scope": "hookwall", "workspace": envelope["workspace"]},
                idempotency_key="hookwall:started",
            )
        return self.journal.append(
            run_id,
            event_type,
            dict(payload),
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _raise_mapper_error(error: BaseException, operation: str) -> None:
        detail = str(error)
        reason = "mapper_effect_" + operation + "_failed"
        for candidate in ("STALE_FENCE", "LEASE_NOT_ACTIVE", "LEASE_EXPIRED"):
            if candidate in detail:
                reason = candidate.lower()
                break
        raise HookwallBlocked(reason, detail) from error

    def reserve(
        self,
        envelope: Mapping[str, Any],
        pre_decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        env = validate_pre_decision(envelope, pre_decision)
        key = str(env["idempotency_key"])
        previous = self._state_event(env, key)
        if previous is not None:
            state = str((previous.get("payload") or {}).get("state") or "")
            if state == "VERIFIED":
                return {
                    "action": "REPLAY_VERIFIED",
                    "state": state,
                    "evidence": (previous.get("payload") or {}).get("evidence"),
                }
            raise HookwallBlocked(
                "effect_reconciliation_required",
                f"prior transaction is {state or previous.get('kind')}; effect will not be replayed",
            )
        attempt_id, fence = self._identity(env)
        try:
            self.operations.prepare_effect_for_attempt(
                attempt_id,
                fence,
                effect_id=self._effect_id(key),
                idempotency_key=key,
                payload={"envelope": dict(env), "pre_decision": dict(pre_decision)},
            )
        except (MapperOperationsError, QueueConflict) as error:
            self._raise_mapper_error(error, "prepare")
        self._append(
            env,
            "hookwall_reserved",
            {
                "idempotency_key": key,
                "state": "RESERVED",
                "attempt_id": attempt_id,
                "fence": fence,
                "envelope_hash": env["envelope_hash"],
                "envelope": dict(env),
            },
            f"hookwall:{key}:reserved",
        )
        return {"action": "EXECUTE", "state": "RESERVED"}

    def effect_confirmed(self, key: str, result: Mapping[str, Any]) -> dict[str, Any]:
        envelope = self._envelope_for_key(key)
        previous = self._state_event(envelope, key)
        if previous is None:
            raise HookwallBlocked("hookwall_bypass", "effect has no reservation")
        state = str((previous.get("payload") or {}).get("state") or "")
        if state == "VERIFIED":
            return {"state": state}
        if state not in {"RESERVED", "EFFECT_CONFIRMED"}:
            raise HookwallBlocked("invalid_effect_transition", state)
        event = self._append(
            envelope,
            "hookwall_effect_confirmed",
            {
                "idempotency_key": key,
                "state": "EFFECT_CONFIRMED",
                "effect_hash": _hash(result),
            },
            f"hookwall:{key}:confirmed",
        )
        return {"state": "EFFECT_CONFIRMED", "event_hash": event["event"]["event_hash"]}

    def verify_and_commit(
        self,
        envelope: Mapping[str, Any],
        pre_decision: Mapping[str, Any],
        receipt: Mapping[str, Any],
        post_decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        env = validate_pre_decision(envelope, pre_decision)
        evidence = verify_post_receipt(env, pre_decision, receipt, post_decision)
        key = str(env["idempotency_key"])
        previous = self._state_event(env, key)
        if previous is None:
            raise HookwallBlocked("effect_not_persisted", "post gate requires confirmed effect journal")
        state = str((previous.get("payload") or {}).get("state") or "")
        if state == "VERIFIED":
            return (previous.get("payload") or {}).get("evidence") or evidence
        if state not in {"RESERVED", "EFFECT_CONFIRMED"}:
            raise HookwallBlocked("effect_not_persisted", state)
        attempt_id, fence = self._identity(env)
        try:
            self.operations.commit_effect_for_attempt(
                self._effect_id(key), attempt_id, fence, receipt
            )
        except (MapperOperationsError, QueueConflict) as error:
            self._raise_mapper_error(error, "commit")
        self._append(
            env,
            "hookwall_verified",
            {"idempotency_key": key, "state": "VERIFIED", "evidence": evidence},
            f"hookwall:{key}:verified",
        )
        return evidence

    def mark_unresolved(self, key: str, reason_code: str) -> dict[str, Any]:
        envelope = self._envelope_for_key(key)
        previous = self._state_event(envelope, key)
        if previous is None:
            self._append(
                envelope,
                "hookwall_blocked",
                {"idempotency_key": key, "state": "BLOCKED", "reason_code": reason_code},
                f"hookwall:{key}:blocked",
            )
            return {"state": "BLOCKED", "reason_code": reason_code}
        payload = previous.get("payload") or {}
        state = str(payload.get("state") or "")
        if state == "VERIFIED":
            return {"state": state}
        if state == "UNCERTAIN":
            return {"state": state, "reason_code": reason_code}
        attempt_id, fence = self._identity(envelope)
        try:
            self.operations.mark_effect_unknown_for_attempt(
                self._effect_id(key), attempt_id, fence
            )
        except (MapperOperationsError, QueueConflict) as error:
            self._raise_mapper_error(error, "unknown")
        self._append(
            envelope,
            "hookwall_unresolved",
            {"idempotency_key": key, "state": "UNCERTAIN", "reason_code": reason_code},
            f"hookwall:{key}:unresolved",
        )
        return {"state": "UNCERTAIN", "reason_code": reason_code}

    def _envelope_for_key(self, key: str) -> dict[str, Any]:
        for event in self.journal.events("hookwall-ledger"):
            payload = event.get("payload") or {}
            if payload.get("idempotency_key") == key:
                return dict(payload["envelope"])
        raise HookwallBlocked("hookwall_bypass", "effect has no reservation")

    def verify_audit_chain(self) -> dict[str, Any]:
        try:
            projection = self.journal.replay("hookwall-index")
        except MapperJournalError as error:
            return {"schema": SCHEMA, "status": "INVALID", "reason_code": str(error)}
        return {
            "schema": SCHEMA,
            "status": "VERIFIED",
            "reason_code": "ok",
            "verified_events": projection["sequence"],
            "head_hash": projection["head_hash"],
            "offline": True,
        }

    def status(self, key: str) -> dict[str, Any] | None:
        try:
            envelope = self._envelope_for_key(key)
        except HookwallBlocked:
            return None
        event = self._state_event(envelope, key)
        if event is None:
            return None
        return {"idempotency_key": key, **(event.get("payload") or {})}


__all__ = ["MapperHookwallEffectLedger", "SCHEMA"]
