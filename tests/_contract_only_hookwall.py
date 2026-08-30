"""Test seam for dispatch scenarios that do not exercise ledger persistence."""


class ContractOnlyHookwallLedger:
    """Validate Hookwall receipts without writing through the retired legacy ledger."""

    def reserve(self, _envelope, _pre_decision):
        return {"action": "EXECUTE", "state": "RESERVED"}

    def effect_confirmed(self, _key, _result):
        return {"state": "EFFECT_CONFIRMED"}

    def verify_and_commit(self, envelope, pre_decision, receipt, post_decision):
        from simplicio_loop.hookwall_gate import verify_post_receipt

        return verify_post_receipt(envelope, pre_decision, receipt, post_decision)

    def mark_unresolved(self, _key, _reason):
        return None
