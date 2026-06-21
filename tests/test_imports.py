"""Smoke test: every module imports cleanly (no API key / heavy run required)."""


def test_core_modules_import():
    import agents  # noqa: F401
    import config  # noqa: F401
    import patient_data  # noqa: F401
    import schemas  # noqa: F401
    from band import adapter, audit, room  # noqa: F401
    from observability import evaluator, tracing  # noqa: F401
    from mesh import fetch_mesh  # noqa: F401


def test_patients_have_expected_shape():
    from patient_data import PATIENTS

    assert "PT-7421" in PATIENTS
    assert "PT-2048" in PATIENTS  # benign decoy for the correction-loop demo
    # the benign decoy carries a known-good ground-truth label
    assert PATIENTS["PT-2048"]["profile"]["ground_truth_escalate"] is False
    for pdata in PATIENTS.values():
        assert {"profile", "sensor_history", "self_reports"} <= pdata.keys()


def test_terminal_ui_imports():
    import main  # noqa: F401  (imports rich + patient_data, defines callbacks)
