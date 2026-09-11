"""Integration-owned measurement entities grouped under stable room devices."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import EntityCategory

from .const import DOMAIN


class RoomSourceSensor(SensorEntity):
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name = True

    def __init__(self, manager, binding):
        self.manager, self.binding = manager, binding
        self.entity_id = binding.entity_id
        self._attr_unique_id = manager.data["streams"][binding.id]["unique_id"]
        self._attr_name = f"{binding.role.replace('_', ' ').title()} · {binding.id[:6]}"
        self._attr_native_unit_of_measurement = "°C" if binding.role == "temperature" else "%"
        if binding.role == "temperature":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{manager.entry.entry_id}:room:{binding.room_id}")},
            name=manager.data["rooms"][binding.room_id]["name"],
            suggested_area=manager.data["rooms"][binding.room_id]["name"],
            manufacturer="climate-pro-x", model="Room history",
        )

    async def async_added_to_hass(self):
        self.manager.register_entity(self.binding, self)

    @property
    def native_value(self):
        return self.manager.values.get(self.binding.id)

    @property
    def available(self):
        return self.native_value is not None and self.manager._stream_active(self.binding.id)

    @property
    def extra_state_attributes(self):
        stream = self.manager.data["streams"][self.binding.id]
        source = self.manager.data["sources"][self.binding.source_id]
        return {"source_entity_id": source["entity_id"], "room_id": self.binding.room_id,
                "active": self.manager._stream_active(self.binding.id),
                "quality": stream.get("quality", "awaiting_observation")}


class HistoryStatusSensor(CoordinatorEntity, SensorEntity):
    _attr_name = "Thermal Efficiency history migration"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:database-check"

    def __init__(self, coordinator, manager):
        super().__init__(coordinator)
        self.manager = manager
        self._attr_unique_id = f"{DOMAIN}_{manager.entry.entry_id}_history_status"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.manager:
            self.manager.register_status_sensor(self)

    async def async_will_remove_from_hass(self):
        if self.manager:
            self.manager.unregister_status_sensor(self)
        await super().async_will_remove_from_hass()

    @property
    def native_value(self):
        return self.manager.data["migration"]["status"]

    @property
    def extra_state_attributes(self):
        migration = self.manager.data["migration"]
        return {"cutoff": migration["cutoff"], "error": migration.get("error"),
                "preserved_hourly_rows": sum(c["count"] for s in migration["sources"].values()
                                             for c in s["chunks"].values() if c["kind"] == "hour"),
                "pending_assignments": sum(bool(s.get("pending")) for s in self.manager.data["sources"].values())}
