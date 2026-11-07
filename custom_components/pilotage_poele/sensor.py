from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE

from .entity import PoeleEntity
from .model import ETATS


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    p = entry.runtime_data
    async_add_entities([Etat(p), ProchainAllumage(p), AllumagesJour(p), Confiance(p)])


class Etat(PoeleEntity, SensorEntity):
    """État du poêle + tout ce que montre la carte (attributs non enregistrés dans l'historique)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(ETATS)
    _unrecorded_attributes = frozenset({
        "plan", "autorisation", "garde_fous", "appris", "historique", "journal", "derniere_action", "run",
        "arret", "zones", "eau", "exterieur", "pieces_noms", "drapeaux", "etat_libelle", "statut_brut", "boutons", "pieces_etat", "tetes", "synthese", "eau_poele"})

    def __init__(self, p) -> None:
        super().__init__(p, "etat")

    @property
    def native_value(self) -> str:
        return self.p.etat

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(self.hass)
        e = self.p.entry.entry_id
        snap = self.p.snapshot()
        snap["boutons"] = {
            "allumer": reg.async_get_entity_id("button", "pilotage_poele", f"{e}_allumer"),
            "annuler": reg.async_get_entity_id("button", "pilotage_poele", f"{e}_annuler"),
            "auto": reg.async_get_entity_id("switch", "pilotage_poele", f"{e}_auto"),
            "arret_auto": reg.async_get_entity_id("switch", "pilotage_poele", f"{e}_arret_auto"),
            "absent": reg.async_get_entity_id("switch", "pilotage_poele", f"{e}_absent"),
            "tetes": reg.async_get_entity_id("switch", "pilotage_poele", f"{e}_tetes"),
            "eau": reg.async_get_entity_id("switch", "pilotage_poele", f"{e}_eau"),
        }
        return snap


class ProchainAllumage(PoeleEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, p) -> None:
        super().__init__(p, "prochain_allumage")

    @property
    def native_value(self) -> datetime | None:
        pl = self.p.plan
        if pl is None or not pl.besoin or pl.allumage is None:
            return None
        return pl.allumage.astimezone(timezone.utc)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pl = self.p.plan
        return {"raison": pl.raison if pl else None}


class AllumagesJour(PoeleEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, p) -> None:
        super().__init__(p, "allumages_jour")

    @property
    def native_value(self) -> int:
        from homeassistant.util import dt as dt_util
        return self.p._allumages_aujourdhui(dt_util.now())


class Confiance(PoeleEntity, SensorEntity):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, p) -> None:
        super().__init__(p, "confiance")

    @property
    def native_value(self) -> int:
        return round(self.p.appris.confiance([n for n, _ in self.p.pieces_cfg()]) * 100)
