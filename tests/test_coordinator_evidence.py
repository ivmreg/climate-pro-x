"""Coordinator provenance uses the revision that actually composed the inputs."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

from custom_components.thermal_efficiency.coordinator import ThermalCoordinator


async def test_coordinator_links_snapshot_and_profiles_pipeline(hass, monkeypatch):
    from custom_components.thermal_efficiency import coordinator as module
    configuration = {"outdoor": "sensor.private_outside", "rooms": {
        "private_room": {"temperature": "sensor.private_temperature"}}, "max_window_days": 365}
    coordinator = ThermalCoordinator(hass, configuration)

    class History:
        data = {"revision": 999}

        def statistic_ids(self):
            return set()

        async def async_prepare(self, stats, conf, timezone):
            return stats, {**conf, "history_revision": 71, "history_schema_version": 2}

    coordinator.history = History()
    monkeypatch.setattr(module, "async_fetch_recorder_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "validate_source_metadata_and_state", lambda *a, **kw: (None, None))
    monkeypatch.setattr(module, "get_instance", lambda _: hass)
    monkeypatch.setattr(module, "statistics_during_period", lambda *a: {"source": [{"start": 0, "mean": 20}]})
    captured = {}

    def calculate(stats, conf, *args):
        captured.update(conf)
        return {"analysis_evidence": {"schema_version": 1, "history_revision": conf["history_revision"]}}

    monkeypatch.setattr(module.thermal_math, "compute_all", calculate)
    result = await coordinator._async_update_data()
    evidence = result["analysis_evidence"]
    assert captured["history_revision"] == evidence["history_revision"] == 71
    assert evidence["history_schema_version"] == 2
    assert len(evidence["configuration_fingerprint"]) == 64
    assert len(evidence["evidence_id"]) == 64
    assert evidence["performance"]["returned_rows"] == 1
    assert all(evidence["performance"][k] >= 0 for k in (
        "metadata_seconds", "recorder_query_seconds", "composition_seconds", "calculation_seconds", "total_seconds"))


async def test_registry_commit_refreshes_analytics_outside_history_lock(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.thermal_efficiency.history import RoomHistoryManager
    conf = {"rooms": {}}
    entry = MockConfigEntry(domain="thermal_efficiency", data=conf)
    entry.add_to_hass(hass)
    history = RoomHistoryManager(hass, entry, conf)
    await history.async_initialize()

    async def refresh():
        assert not history._lock.locked()

    refresh_mock = AsyncMock(side_effect=refresh)
    history._coordinator = SimpleNamespace(async_request_refresh=refresh_mock)
    try:
        await history._commit_registry()
        refresh_mock.assert_awaited_once()
    finally:
        await history.async_shutdown()
