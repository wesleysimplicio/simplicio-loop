from simplicio_loop.execution_route import decide_route, verify_route_hash


def test_production_route_contract_is_deterministic_and_hybrid_without_worker():
    worker = decide_route("mechanically update and test the indexed file", True, False)
    hybrid = decide_route("mechanically update and test the indexed file", False, False)
    agent = decide_route("investigate ambiguous semantic failure", True, True)
    assert worker.route == "worker"
    assert hybrid.route == "hybrid"
    assert agent.route == "agent"
    assert verify_route_hash(worker.to_dict())
    assert verify_route_hash(hybrid.to_dict())
    assert verify_route_hash(agent.to_dict())
