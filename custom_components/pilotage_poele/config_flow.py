"""Configuration par listes déroulantes ; réglages avancés dans « Configurer »."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry, ConfigFlow, ConfigFlowResult, ConfigSubentryFlow, OptionsFlow, SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .planning import ZONES
from .const import (
    CONF_CIBLE, CONF_ECART, CONF_EAU, CONF_ZONE, CONF_EXTERIEUR, CONF_NOTIF, CONF_POELE, CONF_STATUT, CONF_TEMPERATURE, DOMAIN,
    OPTIONS_DEFAUT, SUBENTRY_PIECE,
)

SENSOR = selector.EntitySelectorConfig(domain="sensor")


def _schema_principal(d: dict[str, Any] | None = None) -> vol.Schema:
    d = d or {}

    def req(k):
        return vol.Required(k, default=d[k]) if d.get(k) else vol.Required(k)

    def opt(k):
        return vol.Optional(k, description={"suggested_value": d.get(k)})

    return vol.Schema({
        req(CONF_POELE): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
        opt(CONF_STATUT): selector.EntitySelector(SENSOR),
        opt(CONF_EAU): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature")),
        opt(CONF_EXTERIEUR): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature")),
        opt(CONF_NOTIF): selector.EntitySelector(selector.EntitySelectorConfig(domain="notify")),
    })


def _nettoyer(u: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in u.items() if v not in (None, "")}


class PilotagePoeleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Pilotage poêle", data=_nettoyer(user_input))
        return self.async_show_form(step_id="user", data_schema=_schema_principal())

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data=_nettoyer(user_input))
        return self.async_show_form(step_id="reconfigure", data_schema=_schema_principal(dict(entry.data)))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptionsFlowHandler()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_PIECE: PieceSubentryFlow}


def _schema_options(d: dict[str, Any]) -> vol.Schema:
    def num(mn, mx, st=1, u=None):
        cfg = {"min": mn, "max": mx, "step": st, "mode": selector.NumberSelectorMode.BOX}
        if u:
            cfg["unit_of_measurement"] = u
        return selector.NumberSelector(selector.NumberSelectorConfig(**cfg))

    return vol.Schema({
        vol.Required("matin_actif", default=d["matin_actif"]): selector.BooleanSelector(),
        vol.Required("matin_heure", default=d["matin_heure"]): selector.TimeSelector(),
        vol.Required("soir_actif", default=d["soir_actif"]): selector.BooleanSelector(),
        vol.Required("soir_heure", default=d["soir_heure"]): selector.TimeSelector(),
        vol.Required("fenetre_debut", default=d["fenetre_debut"]): selector.TimeSelector(),
        vol.Required("fenetre_fin", default=d["fenetre_fin"]): selector.TimeSelector(),
        vol.Required("max_allumages", default=d["max_allumages"]): num(1, 6),
        vol.Required("marche_min", default=d["marche_min"]): num(30, 360, 5, "min"),
        vol.Required("marche_max", default=d["marche_max"]): num(120, 900, 10, "min"),
        vol.Required("arret_min", default=d["arret_min"]): num(0, 240, 5, "min"),
        vol.Required("marge", default=d["marge"]): num(1.0, 2.0, 0.05),
        vol.Required("hysteresis", default=d["hysteresis"]): num(0, 2, 0.1, "°C"),
        vol.Required("immediat_actif", default=d["immediat_actif"]): selector.BooleanSelector(),
        vol.Required("seuil_immediat", default=d["seuil_immediat"]): num(0.2, 3, 0.1, "°C"),
    })


class OptionsFlowHandler(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for k in ("max_allumages", "marche_min", "marche_max", "arret_min"):
                user_input[k] = int(user_input[k])
            return self.async_create_entry(data=user_input)
        return self.async_show_form(step_id="init", data_schema=_schema_options({**OPTIONS_DEFAUT, **self.config_entry.options}))


def _schema_piece(d: dict[str, Any] | None = None) -> vol.Schema:
    d = d or {}

    def req(k):
        return vol.Required(k, default=d[k]) if d.get(k) else vol.Required(k)

    return vol.Schema({
        req("name"): selector.TextSelector(),
        req(CONF_TEMPERATURE): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature")),
        vol.Optional(CONF_ZONE, description={"suggested_value": d.get(CONF_ZONE)}): selector.SelectSelector(
            selector.SelectSelectorConfig(options=[{"value": k, "label": v} for k, v in ZONES.items()],
                                          mode=selector.SelectSelectorMode.LIST)),
        vol.Optional(CONF_CIBLE, description={"suggested_value": d.get(CONF_CIBLE)}): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor", "number", "input_number"])),
        vol.Optional(CONF_ECART, description={"suggested_value": d.get(CONF_ECART)}): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.2, max=3, step=0.1, mode=selector.NumberSelectorMode.BOX,
                                          unit_of_measurement="°C")),
    })


def _donnees_piece(u: dict[str, Any]) -> dict[str, Any]:
    return {k: u[k] for k in (CONF_TEMPERATURE, CONF_ZONE, CONF_CIBLE, CONF_ECART) if u.get(k)}


class PieceSubentryFlow(ConfigSubentryFlow):
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        erreurs: dict[str, str] = {}
        if user_input is not None:
            noms = {s.title.casefold() for s in self._get_entry().subentries.values()}
            if user_input["name"].strip().casefold() in noms:
                erreurs["name"] = "nom_existe"
            elif not user_input.get(CONF_ZONE) and not user_input.get(CONF_CIBLE):
                erreurs["base"] = "zone_ou_cible"
            else:
                return self.async_create_entry(title=user_input["name"].strip(), data=_donnees_piece(user_input))
        return self.async_show_form(step_id="user", data_schema=_schema_piece(user_input), errors=erreurs)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        erreurs: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_ZONE) and not user_input.get(CONF_CIBLE):
                erreurs["base"] = "zone_ou_cible"
            else:
                return self.async_update_and_abort(self._get_entry(), sub, title=user_input["name"].strip(),
                                                   data=_donnees_piece(user_input))
        return self.async_show_form(step_id="reconfigure", data_schema=_schema_piece(user_input or {"name": sub.title, **sub.data}), errors=erreurs)
