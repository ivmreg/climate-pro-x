"""Recorder and registry regressions for retained room history."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import Event, State
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_efficiency.assignments import (
    compose, configured_inputs, identity, inside, timestamp, validate_visits,
)
from custom_components.thermal_efficiency.history import RoomHistoryManager, observation
from custom_components.thermal_efficiency.room_sensor import RoomSourceSensor


@pytest.fixture
async def manager(hass):
    registry = er.async_get(hass)
    for name in ("a", "b"):
        area = ar.async_get(hass).async_create(name)
        entity = registry.async_get_or_create("sensor", "test", name, suggested_object_id=name)
        registry.async_update_entity(entity.entity_id, area_id=area.id)
    config = {"outdoor": "sensor.outdoor", "rooms": {
        "a": {"temperature": "sensor.a"}, "b": {"temperature": "sensor.b"}}}
    entry = MockConfigEntry(domain="thermal_efficiency", data=config, version=2)
    entry.add_to_hass(hass)
    result = RoomHistoryManager(hass, entry, config)
    await result.async_initialize()
    return result


@pytest.mark.parametrize(("value", "unit", "role", "expected"), [
    ("68", "°F", "temperature", 20), ("293.15", "K", "temperature", 20),
    ("nan", "°C", "temperature", None), ("inf", "°C", "temperature", None),
    ("unknown", "°C", "temperature", None), ("20", None, "temperature", None),
    ("10", "W", "heating_power", None), ("101", "%", "heating_power", None),
    ("-1", "%", "heating_power", None), ("0", "%", "heating_power", 0),
])
def test_observation_units(value, unit, role, expected):
    assert observation(State("sensor.test", value, {"unit_of_measurement": unit}), role) == expected
    assert observation(None, role) is None


def test_pure_validation():
    assert identity("a", "b") != identity("b", "a")
    assert inside(3600, [{"start": 1800, "end": 7200}])
    assert not inside(0, [{"start": 1800, "end": None}])
    with pytest.raises(ValueError):
        timestamp("2026-01-01")
    assert timestamp("1970-01-01T00:00:00+00:00") == 0
    assert configured_inputs({"rooms": {}, "co2": ["sensor.a", "sensor.b"], "water": "water:all"}) == {"sensor.a", "sensor.b", "water:all"}
    for visits in (
        [{"role": "temperature", "start": 3, "end": 2}],
        [{"role": "temperature", "start": 1, "end": float("inf")}],
        [{"role": "temperature", "start": None, "end": None}, {"role": "temperature", "start": 1, "end": 2}],
    ):
        with pytest.raises(ValueError):
            validate_visits({"rooms": {"a": {"visits": visits}}})


async def test_move_return_restart_and_no_stale_copy(recorder_mock, hass, manager):
    original = manager.bindings()[0]
    manager._subscribe_states()
    hass.states.async_set("sensor.a", "20", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert manager.values[original.id] == 20
    when = manager._now()
    registry = er.async_get(hass)
    registry.async_update_entity("sensor.a", area_id=manager.data["rooms"]["b"]["area_id"])
    assert manager._reconcile_registry(when)
    assert original.id not in manager.values
    assert len(manager.bindings()) == 3
    incoming = next(b for b in manager.bindings() if b.room_id == "b" and b.source_entity_id == "sensor.a")
    assert incoming.id not in manager.values
    hass.states.async_set("sensor.a", "20", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert manager.values[incoming.id] == 20  # unchanged report is an observation
    registry.async_update_entity("sensor.a", area_id=manager.data["rooms"]["a"]["area_id"])
    assert manager._reconcile_registry(when + 1)
    assert len(manager.bindings()) == 3
    assert manager._stream_active(original.id)
    await manager._save()
    again = RoomHistoryManager(hass, manager.entry, manager.config)
    await again.async_initialize()
    assert len(again.bindings()) == 3
    assert again.values == {}
    assert again._stream_active(original.id)
    validate_visits(again.data)
    await manager.async_shutdown()
    await again.async_shutdown()


async def test_offline_move_requires_correction(hass, manager):
    old = manager.bindings()[0]
    last = manager.data["last_verified"]
    registry = er.async_get(hass)
    registry.async_update_entity("sensor.a", area_id=manager.data["rooms"]["b"]["area_id"])
    again = RoomHistoryManager(hass, manager.entry, manager.config)
    await again.async_initialize()
    assert not again._stream_active(old.id)
    assert again.data["sources"][old.source_id]["pending"]
    with pytest.raises(ValueError, match="stale"):
        await again.async_correct(old.source_id, "b", last, -1)
    with pytest.raises(ValueError, match="invalid_time"):
        await again.async_correct(old.source_id, "b", last - 1, again.data["revision"])
    await again.async_correct(old.source_id, "b", last, again.data["revision"])
    assert not again.data["sources"][old.source_id]["pending"]
    assert again.values == {}


async def test_rename_delete_recreate_and_explicit_replacement(hass, manager):
    binding = manager.bindings()[0]
    registry = er.async_get(hass)
    registry.async_update_entity("sensor.a", new_entity_id="sensor.renamed")
    assert manager._reconcile_registry(manager._now())
    assert manager.data["sources"][binding.source_id]["entity_id"] == "sensor.renamed"
    registry.async_remove("sensor.renamed")
    manager._reconcile_registry(manager._now())
    assert not manager._stream_active(binding.id)
    recreated = registry.async_get_or_create("sensor", "test", "a", suggested_object_id="renamed")
    source = manager._source(recreated.entity_id, "temperature")
    assert source["id"] != binding.source_id
    await manager._save()
    updated = deepcopy(manager.config)
    updated["rooms"]["a"]["temperature"] = recreated.entity_id
    again = RoomHistoryManager(hass, manager.entry, updated)
    await again.async_initialize()
    assert any(b.source_id == source["id"] and again._stream_active(b.id) for b in again.bindings())


async def test_capture_quality_entity_and_checkpoint(recorder_mock, hass, manager):
    binding = manager.bindings()[0]
    sensor = RoomSourceSensor(manager, binding)
    assert sensor.native_value is None
    assert not sensor.available
    assert sensor.device_info["identifiers"]
    assert sensor.extra_state_attributes["quality"] == "awaiting_observation"
    manager._subscribe_states()
    hass.states.async_set("sensor.a", "20", {"unit_of_measurement": "°C", "restored": True})
    await hass.async_block_till_done()
    assert sensor.native_value is None
    hass.states.async_set("sensor.a", "21", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert sensor.native_value == 21
    assert sensor.available
    hass.states.async_set("sensor.a", "unavailable")
    await hass.async_block_till_done()
    assert not sensor.available
    assert manager.data["streams"][binding.id]["coverage"][-1]["end"] is not None
    hass.states.async_set("sensor.a", "22", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    await manager._checkpoint(manager._now() + 86401)
    assert not sensor.available
    assert sensor.extra_state_attributes["quality"] == "stale"
    await manager.async_shutdown()


async def test_composition_legacy_cutover_gaps_and_cumulative(manager):
    data = manager.data
    cutoff = data["migration"]["cutoff"]
    binding = manager.bindings()[0]
    archive = data["migration"]["sources"]["sensor.a"]["statistic_id"]
    old = {"start": cutoff - 3600, "mean": 19}
    live = {"start": cutoff, "mean": 22}
    data["migration"]["status"] = "complete"
    data["streams"][binding.id]["coverage"] = [{"start": cutoff, "end": None}]
    stats = {archive: [old, live], binding.entity_id: [old, live]}
    result, conf = compose(stats, manager.config, data, UTC)
    assert result[conf["rooms"]["a"]["temperature"]] == [old, live]
    data["streams"][binding.id]["quarantine"] = [{"start": cutoff, "end": cutoff + 100}]
    result, conf = compose(stats, manager.config, data, UTC)
    assert result[conf["rooms"]["a"]["temperature"]] == [old]
    # Original entities are entirely absent: preserved data still composes.
    assert result["sensor.a"] == [old]
    data["migration"]["status"] = "failed"
    result, conf = compose({"sensor.a": [old]}, manager.config, data, UTC)
    assert result[conf["rooms"]["a"]["temperature"]] == [old]


async def test_platform_tracks_area_events_and_renamed_owned_stream(recorder_mock, hass, manager, monkeypatch):
    from custom_components.thermal_efficiency.history_migration import HistoryMigrator
    monkeypatch.setattr(HistoryMigrator, "run", AsyncMock())
    added = []
    coordinator = MagicMock(async_request_refresh=AsyncMock())
    await manager.async_setup_platform(added.extend, coordinator)
    await manager._migration_task
    assert len(added) == 2
    registry = er.async_get(hass)
    old = manager.bindings()[0]
    registry.async_update_entity("sensor.a", area_id=manager.data["rooms"]["b"]["area_id"])
    await hass.async_block_till_done()
    assert len(added) == 3
    assert not manager._stream_active(old.id)
    new = next(b for b in manager.bindings() if b.room_id == "b" and b.source_entity_id == "sensor.a")
    registry.async_update_entity(new.entity_id, new_entity_id="sensor.owned_renamed")
    await hass.async_block_till_done()
    assert manager.data["streams"][new.id]["entity_id"] == "sensor.owned_renamed"
    assert new.entity_id in manager.statistic_ids()
    assert "sensor.owned_renamed" in manager.statistic_ids()
    assert "temperature" not in manager.current_rooms()["a"]
    # The minute checkpoint must not create a second in-flight migration task.
    manager._tick(dt_util.utcnow())
    await hass.async_block_till_done()
    await manager._migration_task
    await manager.async_shutdown()


async def test_migration_failure_is_visible_and_retryable(manager, monkeypatch):
    from custom_components.thermal_efficiency.history_migration import HistoryMigrator
    monkeypatch.setattr(HistoryMigrator, "run", AsyncMock(side_effect=ValueError("test failure")))
    await manager.async_migrate_history()
    assert not manager.ready
    assert manager.data["migration"]["status"] == "failed"
    assert manager.data["migration"]["error"] == "ValueError"
    async def complete(_self):
        manager.data["migration"]["status"] = "complete"
    monkeypatch.setattr(HistoryMigrator, "run", complete)
    await manager.async_migrate_history()
    assert manager.ready
    manager._start_migration()
    assert manager._migration_task is None


async def test_same_id_replacement_and_non_destructive_correction(recorder_mock, hass, manager):
    original = manager.bindings()[0]
    hass.states.async_set("sensor.a", "20", {"unit_of_measurement": "°C"})
    await manager.async_replace("a", "temperature", "sensor.a", manager.data["revision"])
    assert len(manager.bindings()) == 3
    assert not manager._stream_active(original.id)
    assert manager.data["sources"][original.source_id]["retired"]
    assert all(not v for v in manager.values.values())  # no cached replacement sample
    retired = next(v for v in manager.data["rooms"]["a"]["visits"] if v.get("stream") == original.id)
    end = retired["end"]
    await manager.async_edit_visit(retired["id"], end - 86400, end, True, manager.data["revision"])
    corrected = next(v for v in manager.data["rooms"]["a"]["visits"] if v["id"] == retired["id"])
    assert corrected["stream"] is None
    assert corrected["as_recorded"]["stream"] == original.id
    assert original.id in manager.data["streams"]
    snapshot = deepcopy(manager.data)
    with pytest.raises(ValueError):
        await manager.async_edit_visit(retired["id"], end, end - 1, False, manager.data["revision"])
    assert manager.data == snapshot
    await manager.async_shutdown()


async def test_statistic_ids_retains_global_inputs_when_ready(manager):
    assert "sensor.outdoor" in manager.statistic_ids()
    manager.config["gas_meter"] = "sensor.gas"
    assert "sensor.gas" in manager.statistic_ids()
    manager.data["migration"]["status"] = "complete"
    assert manager.ready
    ids_ready = manager.statistic_ids()
    assert "sensor.outdoor" in ids_ready
    assert "sensor.gas" in ids_ready


def test_validate_visits_detects_cross_room_overlap():
    data_same_stream = {
        "rooms": {
            "a": {"visits": [{"role": "temperature", "stream": "s1", "start": 100, "end": 200}]},
            "b": {"visits": [{"role": "temperature", "stream": "s1", "start": 150, "end": 250}]},
        },
        "streams": {"s1": {"id": "s1", "source_id": "src1"}},
    }
    with pytest.raises(ValueError, match="Assignments overlap"):
        validate_visits(data_same_stream)

    data_same_source = {
        "rooms": {
            "a": {"visits": [{"role": "temperature", "stream": "s1", "start": 100, "end": 200}]},
            "b": {"visits": [{"role": "temperature", "stream": "s2", "start": 150, "end": 250}]},
        },
        "streams": {
            "s1": {"id": "s1", "source_id": "src1"},
            "s2": {"id": "s2", "source_id": "src1"},
        },
    }
    with pytest.raises(ValueError, match="Assignments overlap"):
        validate_visits(data_same_source)


async def test_first_startup_area_mismatch_creates_pending(hass):
    area_reg = ar.async_get(hass)
    living_area = area_reg.async_create("Living Room")
    kitchen_area = area_reg.async_create("Kitchen")

    entity_reg = er.async_get(hass)
    sensor = entity_reg.async_get_or_create("sensor", "test", "temp", suggested_object_id="temp")
    entity_reg.async_update_entity(sensor.entity_id, area_id=kitchen_area.id)

    config = {
        "outdoor": "sensor.out",
        "rooms": {
            "living_room": {"name": "Living Room", "temperature": sensor.entity_id}
        },
    }
    entry = MockConfigEntry(domain="thermal_efficiency", data=config, version=2)
    entry.add_to_hass(hass)
    mgr = RoomHistoryManager(hass, entry, config)
    await mgr.async_initialize()

    # Room adopts true room area from area registry
    assert mgr.data["rooms"]["living_room"]["area_id"] == living_area.id
    # Source is marked pending because sensor was in kitchen_area
    source = next(s for s in mgr.data["sources"].values() if s["entity_id"] == sensor.entity_id)
    assert source.get("pending") is not None
    visits = mgr.data["rooms"]["living_room"]["visits"]
    assert len(visits) == 2
    assert visits[0]["cause"] == "legacy"
    assert visits[0]["end"] is not None
    assert visits[1]["cause"] == "gap"
    assert visits[1]["stream"] is None
    binding = mgr.bindings()[0]
    assert not mgr._stream_active(binding.id)
    await mgr.async_shutdown()


async def test_coverage_boundaries_persisted_immediately(recorder_mock, hass, manager):
    save_calls = []
    orig_save = manager._save
    async def track_save():
        save_calls.append(manager._now())
        await orig_save()
    manager._save = track_save

    manager._subscribe_states()
    # First state change opens coverage interval: must immediately persist
    hass.states.async_set("sensor.a", "21", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert len(save_calls) >= 1

    save_calls.clear()
    # Going unavailable closes coverage interval via _gap: must immediately persist
    hass.states.async_set("sensor.a", "unavailable")
    await hass.async_block_till_done()
    assert len(save_calls) >= 1
    await manager.async_shutdown()


async def test_async_replace_clears_pending_and_updates_area(hass, manager):
    binding = manager.bindings()[0]
    source = manager.data["sources"][binding.source_id]
    source["pending"] = {"since": 1000, "observed": 1000}
    source["area_id"] = "unknown_area"

    hass.states.async_set("sensor.a", "20", {"unit_of_measurement": "°C"})
    await manager.async_replace("a", "temperature", "sensor.a", manager.data["revision"])

    assert source.get("pending") is None
    assert source["area_id"] == manager.data["rooms"]["a"]["area_id"]
    await manager.async_shutdown()


async def test_migration_failure_exponential_backoff_and_retry(manager, monkeypatch):
    from custom_components.thermal_efficiency.history_migration import HistoryMigrator
    monkeypatch.setattr(HistoryMigrator, "run", AsyncMock(side_effect=RuntimeError("db error")))

    await manager.async_migrate_history()
    assert manager.data["migration"]["status"] == "failed"
    assert manager.data["migration"]["retries"] == 1
    next_retry1 = manager.data["migration"]["next_retry"]
    assert next_retry1 >= manager._now() + 59

    # Normal tick or _start_migration within backoff window does not retry
    manager._start_migration()
    assert manager._migration_task is None

    # Explicit manual retry resets backoff and spawns immediately
    await manager.async_retry_migration()
    assert manager._migration_task is not None
    await manager._migration_task

    assert manager.data["migration"]["retries"] == 2
    next_retry2 = manager.data["migration"]["next_retry"]
    assert next_retry2 >= manager._now() + 119


def test_observation_heating_power_unit_aliases():
    assert observation(State("sensor.test", "50", {"unit_of_measurement": "%"}), "heating_power") == 50.0
    assert observation(State("sensor.test", "75.5", {"unit_of_measurement": "percent"}), "heating_power") == 75.5
    assert observation(State("sensor.test", "25", {"unit_of_measurement": "percentage"}), "heating_power") == 25.0
    assert observation(State("sensor.test", "10", {"unit_of_measurement": " PERCENT "}), "heating_power") == 10.0
    assert observation(State("sensor.test", "50", {"unit_of_measurement": "W"}), "heating_power") is None


async def test_freshness_guard_rejects_prerestart_cached_state(recorder_mock, hass, manager):
    binding = manager.bindings()[0]
    manager._subscribe_states()
    now_ts = manager._now()
    manager._started_at = now_ts + 100

    # Simulate an event whose last_reported is before startup boundary
    state = State("sensor.a", "22", {"unit_of_measurement": "°C"}, last_reported=datetime.fromtimestamp(now_ts + 50, UTC))
    event = Event("state_changed", {"entity_id": "sensor.a", "new_state": state}, time_fired_timestamp=now_ts + 110)
    manager._state_changed(event)
    assert manager.values.get(binding.id) != 22

    # Fresh report after startup boundary is accepted
    fresh_state = State("sensor.a", "22", {"unit_of_measurement": "°C"}, last_reported=datetime.fromtimestamp(now_ts + 150, UTC))
    fresh_event = Event("state_changed", {"entity_id": "sensor.a", "new_state": fresh_state}, time_fired_timestamp=now_ts + 150)
    manager._state_changed(fresh_event)
    assert manager.values.get(binding.id) == 22
    await manager.async_shutdown()


async def test_async_correct_closes_pending_gap_at_effective(hass, manager):
    # Simulate an uncertain offline move creating a pending gap in room a
    when = manager._now() - 100
    source_id = manager.bindings()[0].source_id
    source = manager.data["sources"][source_id]
    source["pending"] = {"since": when, "observed": when}
    for v in manager.data["rooms"]["a"]["visits"]:
        if v.get("end") is None:
            v["end"] = when
    manager.data["rooms"]["a"]["visits"].append({
        "id": "gap_visit_1", "stream": None, "role": "temperature",
        "start": when, "end": None, "cause": "gap", "legacy": False, "expected": True,
    })

    # Destination is room b, effective 50 seconds after gap start
    effective = when + 50
    await manager.async_correct(source_id, "b", effective, manager.data["revision"])

    # Gap visit in room a must be closed at effective
    gap_visit = next(v for v in manager.data["rooms"]["a"]["visits"] if v.get("id") == "gap_visit_1")
    assert gap_visit["end"] == effective
    # Source is no longer pending
    assert source.get("pending") is None
    # New visit in room b starts at effective
    new_visit = next(v for v in manager.data["rooms"]["b"]["visits"] if v.get("start") == effective)
    assert new_visit["role"] == "temperature"
    await manager.async_shutdown()


async def test_first_startup_area_mismatch_allows_historical_effective_time(hass):
    area_reg = ar.async_get(hass)
    living_area = area_reg.async_create("Living")
    kitchen_area = area_reg.async_create("Kitchen")

    entity_reg = er.async_get(hass)
    sensor = entity_reg.async_get_or_create("sensor", "test", "temp_first", suggested_object_id="temp_first")
    entity_reg.async_update_entity(sensor.entity_id, area_id=kitchen_area.id)

    config = {
        "outdoor": "sensor.out",
        "rooms": {
            "living": {"name": "Living", "temperature": sensor.entity_id},
            "kitchen": {"name": "Kitchen"},
        },
    }
    entry = MockConfigEntry(domain="thermal_efficiency", data=config, version=2)
    entry.add_to_hass(hass)
    mgr = RoomHistoryManager(hass, entry, config)
    await mgr.async_initialize()

    source = next(s for s in mgr.data["sources"].values() if s["entity_id"] == sensor.entity_id)
    assert source["pending"]["since"] == 0

    # User corrects move to have taken place 1000 seconds before startup
    historical_effective = 100.0
    await mgr.async_correct(source["id"], "kitchen", historical_effective, mgr.data["revision"])
    assert source.get("pending") is None
    kitchen_visit = next(v for v in mgr.data["rooms"]["kitchen"]["visits"] if v.get("start") == historical_effective)
    assert kitchen_visit["role"] == "temperature"
    await mgr.async_shutdown()


async def test_current_rooms_preserves_role_during_pending_unresolved_move(hass, manager):
    source_id = manager.bindings()[0].source_id
    source = manager.data["sources"][source_id]
    when = manager._now() - 100
    source["pending"] = {"since": when, "observed": when}

    # Close active stream visit and open gap visit in room a
    for v in manager.data["rooms"]["a"]["visits"]:
        if v.get("end") is None:
            v["end"] = when
    manager.data["rooms"]["a"]["visits"].append({
        "id": "pending_gap", "stream": None, "role": "temperature",
        "start": when, "end": None, "cause": "gap", "legacy": False, "expected": True,
    })

    # While pending, current_rooms() must preserve sensor.a for room a
    rooms = manager.current_rooms()
    assert rooms["a"].get("temperature") == "sensor.a"

    # Once resolved, pending is cleared and role reflects the resolution
    await manager.async_correct(source_id, "b", when + 10, manager.data["revision"])
    rooms_resolved = manager.current_rooms()
    assert "temperature" not in rooms_resolved["a"]
    assert rooms_resolved["b"].get("temperature") == "sensor.a"
    await manager.async_shutdown()


async def test_async_edit_visit_re_inclusion_restores_stream(recorder_mock, hass, manager):
    original = manager.bindings()[0]
    hass.states.async_set("sensor.a", "20", {"unit_of_measurement": "°C"})
    await manager.async_replace("a", "temperature", "sensor.a", manager.data["revision"])
    retired = next(v for v in manager.data["rooms"]["a"]["visits"] if v.get("stream") == original.id)
    end = retired["end"]

    # Exclude visit sets stream to None while preserving as_recorded
    await manager.async_edit_visit(retired["id"], end - 86400, end, True, manager.data["revision"])
    visit = next(v for v in manager.data["rooms"]["a"]["visits"] if v["id"] == retired["id"])
    assert visit["stream"] is None
    assert visit["as_recorded"]["stream"] == original.id

    # Re-including with exclude=False restores original stream
    await manager.async_edit_visit(retired["id"], end - 86400, end, False, manager.data["revision"])
    visit = next(v for v in manager.data["rooms"]["a"]["visits"] if v["id"] == retired["id"])
    assert visit["stream"] == original.id
    await manager.async_shutdown()


async def test_room_rename_preserves_stored_area_id(hass):
    area_reg = ar.async_get(hass)
    orig_area = area_reg.async_create("Original Room")
    other_area = area_reg.async_create("Other Room")

    config = {
        "outdoor": "sensor.outdoor",
        "rooms": {"room_1": {"name": "Original Room", "temperature": "sensor.t1"}},
    }
    entry = MockConfigEntry(domain="thermal_efficiency", data=config, version=2)
    entry.add_to_hass(hass)

    mgr = RoomHistoryManager(hass, entry, config)
    await mgr.async_initialize()
    assert mgr.data["rooms"]["room_1"]["area_id"] == orig_area.id
    await mgr._save()
    await mgr.async_shutdown()

    # Rename display name in config to match other_area
    new_config = {
        "outdoor": "sensor.outdoor",
        "rooms": {"room_1": {"name": "Other Room", "temperature": "sensor.t1"}},
    }
    hass.config_entries.async_update_entry(entry, data=new_config)
    mgr2 = RoomHistoryManager(hass, entry, new_config)
    await mgr2.async_initialize()
    assert mgr2.data["rooms"]["room_1"]["name"] == "Other Room"
    # Stored area_id must be authoritative and not overwritten
    assert mgr2.data["rooms"]["room_1"]["area_id"] == orig_area.id
    await mgr2.async_shutdown()


async def test_history_mutations_request_coordinator_refresh(recorder_mock, hass, manager):
    coordinator = AsyncMock()
    manager._coordinator = coordinator

    # Set valid state for sensor.a
    hass.states.async_set("sensor.a", "20", {"unit_of_measurement": "°C"})

    # 1. async_replace requests refresh
    await manager.async_replace("a", "temperature", "sensor.a", manager.data["revision"])
    assert coordinator.async_request_refresh.call_count == 1

    # 2. async_edit_visit requests refresh
    retired = next(v for v in manager.data["rooms"]["a"]["visits"] if v.get("end") is not None)
    end = retired["end"]
    await manager.async_edit_visit(retired["id"], end - 100, end, True, manager.data["revision"])
    assert coordinator.async_request_refresh.call_count == 2

    # 3. async_correct requests refresh
    source_id = manager.bindings()[0].source_id
    source = manager.data["sources"][source_id]
    when = manager._now() - 200
    source["pending"] = {"since": when, "observed": when}
    await manager.async_correct(source_id, "b", when + 10, manager.data["revision"])
    assert coordinator.async_request_refresh.call_count == 3
    await manager.async_shutdown()


async def test_checkpoint_publishes_stale_quality(hass, manager):
    when = manager._now()
    sid = manager.bindings()[0].id
    stream = manager.data["streams"][sid]
    stream["last_report"] = when - 90000  # older than 86400s silence threshold

    mock_entity = MagicMock()
    mock_entity.hass = hass
    manager.entities[sid] = mock_entity

    await manager._checkpoint(when)

    assert stream["quality"] == "stale"
    mock_entity.async_write_ha_state.assert_called_once()
    await manager.async_shutdown()


async def test_async_correct_staged_failure_preserves_state(hass, manager, monkeypatch):
    source_id = manager.bindings()[0].source_id
    source = manager.data["sources"][source_id]
    when = manager._now() - 200
    source["pending"] = {"since": when, "observed": when}
    snapshot = deepcopy(manager.data)

    from custom_components.thermal_efficiency import history as h_mod
    def mock_validate(data):
        raise ValueError("simulated_validation_failure")
    monkeypatch.setattr(h_mod, "validate_visits", mock_validate)

    with pytest.raises(ValueError, match="simulated_validation_failure"):
        await manager.async_correct(source_id, "b", when + 10, manager.data["revision"])

    # Manager data must be identical to snapshot (no in-place mutations preserved)
    assert manager.data == snapshot
    await manager.async_shutdown()


async def test_async_correct_only_closes_matching_source_gap(hass, manager):
    # Close existing visit in destination room b before effective time
    when_a = manager._now() - 200
    for v in manager.data["rooms"]["b"]["visits"]:
        v["end"] = when_a

    # Room a has pending move for sensor a: close active stream visit and open gap
    source_a_id = manager.bindings()[0].source_id
    for v in manager.data["rooms"]["a"]["visits"]:
        if v.get("end") is None:
            v["end"] = when_a
    manager.data["sources"][source_a_id]["pending"] = {"since": when_a, "observed": when_a}
    manager.data["rooms"]["a"]["visits"].append({
        "id": "gap_a", "stream": None, "role": "temperature",
        "start": when_a, "end": None, "cause": "gap", "legacy": False, "expected": True,
        "source_id": source_a_id,
    })

    # An unrelated room "other" has an open gap for temperature
    manager.data["rooms"]["other"] = {"name": "Other", "visits": []}
    when_other = manager._now() - 300
    manager.data["rooms"]["other"]["visits"].append({
        "id": "gap_other", "stream": None, "role": "temperature",
        "start": when_other, "end": None, "cause": "gap", "legacy": False, "expected": True,
        "source_id": "unrelated_source",
    })

    # Correct source a into room b
    await manager.async_correct(source_a_id, "b", when_a + 50, manager.data["revision"])

    # Room a's gap was closed at when_a + 50
    gap_a = next(v for v in manager.data["rooms"]["a"]["visits"] if v["id"] == "gap_a")
    assert gap_a["end"] == when_a + 50

    # Unrelated room "other"'s gap remains open (end is None)
    gap_other = next(v for v in manager.data["rooms"]["other"]["visits"] if v["id"] == "gap_other")
    assert gap_other["end"] is None
    await manager.async_shutdown()



