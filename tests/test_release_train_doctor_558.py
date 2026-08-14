from scripts.release_train import doctor_release_train


def test_doctor_does_not_claim_eight_repo_conformance():
    report = doctor_release_train()
    assert report["schema"] == "simplicio.release-train-doctor/v1"
    assert report["loop_engine"]["status"] == "MEASURED"
    assert report["loop_engine"]["compose"] is True
    assert report["eight_repo_conformance"] == "UNVERIFIED"
    assert report["closes_eight_repo_ac"] is False
    names = {item["component"] for item in report["children"]}
    assert "simplicio-agent" in names
    assert "simplicio-loop-oss" in names
    assert all(item["status"] == "UNVERIFIED" for item in report["children"])
