from datetime import timedelta

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.pilotage_poele.const import (
    CONF_CIBLE, CONF_EAU, CONF_POELE, CONF_STATUT, CONF_TEMPERATURE, DOMAIN, SUBENTRY_PIECE,
)

POELE, STATUT, EAU = "climate.poele", "sensor.statut_poele", "sensor.eau"
TEMP, CIBLE = "sensor.chambre_t", "sensor.chambre_cible"


def eid(hass, entry, domaine, cle):
    return er.async_get(hass).async_get_entity_id(domaine, DOMAIN, f"{entry.entry_id}_{cle}")


async def installer(hass, freezer, heure="2026-11-05 05:30:00+01:00", temp="17.0", cible="20.5", statut="Off", zone=None):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to(heure)
    hass.states.async_set(POELE, "off", {"hvac_action": "off"})
    hass.states.async_set(STATUT, statut)
    hass.states.async_set(EAU, "30")
    hass.states.async_set(TEMP, temp)
    hass.states.async_set(CIBLE, cible)
    appels = {"on": async_mock_service(hass, "climate", "turn_on"), "off": async_mock_service(hass, "climate", "turn_off"),
              "notif": async_mock_service(hass, "persistent_notification", "create")}
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert r["type"] is FlowResultType.FORM
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_POELE: POELE, CONF_STATUT: STATUT, CONF_EAU: EAU})
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    r = await hass.config_entries.subentries.async_init((entry.entry_id, SUBENTRY_PIECE), context={"source": SOURCE_USER})
    donnees = {"name": "Chambre", CONF_TEMPERATURE: TEMP}
    donnees.update({"zone": zone} if zone else {CONF_CIBLE: CIBLE})
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], donnees)
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    await hass.async_block_till_done()
    return entry, appels


async def avancer(hass, freezer, entry, minutes):
    freezer.tick(timedelta(minutes=minutes))
    await entry.runtime_data._tick(None)
    await hass.async_block_till_done()


async def bascule(hass, entry, cle, on=True):
    await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": eid(hass, entry, "switch", cle)}, blocking=True)
    await hass.async_block_till_done()


def poele(hass, statut, mode="heat", action="heating"):
    hass.states.async_set(POELE, mode, {"hvac_action": action})
    hass.states.async_set(STATUT, statut)


async def test_installation_unique_et_entites(hass, freezer):
    entry, _ = await installer(hass, freezer)
    for d, c in (("sensor", "etat"), ("sensor", "prochain_allumage"), ("sensor", "confiance"), ("binary_sensor", "probleme"),
                 ("switch", "auto"), ("switch", "arret_auto"), ("switch", "absent"), ("button", "allumer"), ("button", "annuler")):
        assert eid(hass, entry, d, c), (d, c)
    # tout est désactivé par défaut
    for c in ("auto", "arret_auto", "absent"):
        assert hass.states.get(eid(hass, entry, "switch", c)).state == "off"
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert r["type"] is FlowResultType.ABORT


async def test_mode_conseil_calcule_sans_rien_envoyer(hass, freezer):
    entry, appels = await installer(hass, freezer)
    st = hass.states.get(eid(hass, entry, "sensor", "etat"))
    assert st.state == "arret"
    plan = st.attributes["plan"]
    assert plan["besoin"] and plan["limitante"] == "Chambre" and "Chambre" in plan["raison"]
    assert "désactivé" in st.attributes["autorisation"]["raison"].lower()
    assert appels["on"] == []
    assert hass.states.get(eid(hass, entry, "sensor", "prochain_allumage")).state not in ("unknown", "unavailable")


async def test_allumage_auto_et_apprentissage_du_delai(hass, freezer):
    entry, appels = await installer(hass, freezer)
    await bascule(hass, entry, "auto")
    assert len(appels["on"]) == 1
    assert any("allumé automatiquement" in a.data["message"] for a in appels["notif"])
    await avancer(hass, freezer, entry, 2)
    assert len(appels["on"]) == 1                       # pas de double commande
    poele(hass, "Ignition")
    await avancer(hass, freezer, entry, 5)
    poele(hass, "Heating")
    await avancer(hass, freezer, entry, 7)
    p = entry.runtime_data
    assert p.etat == "marche" and p.appris.delais and 10 <= p.appris.delais[0] <= 20
    assert p.run["source"] == "auto"


async def test_pas_d_allumage_hors_plage_horaire(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 03:00:00+01:00")
    await bascule(hass, entry, "auto")
    assert appels["on"] == []
    assert "plage autorisée" in hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["autorisation"]["raison"]


async def test_mode_absent_bloque(hass, freezer):
    entry, appels = await installer(hass, freezer)
    await bascule(hass, entry, "absent")
    await bascule(hass, entry, "auto")
    assert appels["on"] == []


async def test_pas_d_allumage_si_pas_de_besoin(hass, freezer):
    entry, appels = await installer(hass, freezer, temp="21.5")
    await bascule(hass, entry, "auto")
    assert appels["on"] == []
    assert hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["plan"]["besoin"] is False


async def test_pas_d_allumage_trop_pres_de_l_echeance(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 06:50:00+01:00", temp="15.0")
    hass.config_entries.async_update_entry(entry, options={"immediat_actif": False})
    await hass.async_block_till_done()
    await bascule(hass, entry, "auto")
    assert appels["on"] == []                           # 10 min avant 7 h : inutile et risqué


async def test_echec_d_allumage_verrouille(hass, freezer):
    entry, appels = await installer(hass, freezer)
    await bascule(hass, entry, "auto")
    assert len(appels["on"]) == 1
    await avancer(hass, freezer, entry, 45)             # le poêle n'est jamais passé en marche
    p = entry.runtime_data
    assert p.drapeaux["verrou"] and not p.drapeaux["auto"]
    assert hass.states.get(eid(hass, entry, "binary_sensor", "probleme")).state == "on"
    assert any("n'a pas abouti" in a.data["message"] for a in appels["notif"])
    await avancer(hass, freezer, entry, 5)
    assert len(appels["on"]) == 1                       # plus aucune commande


async def test_annuler_empeche_l_allumage_et_eteint(hass, freezer):
    entry, appels = await installer(hass, freezer)
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, entry, "button", "annuler")}, blocking=True)
    await bascule(hass, entry, "auto")
    assert appels["on"] == []                           # échéance annulée
    # allumage forcé puis annulation : le poêle est éteint
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, entry, "button", "allumer")}, blocking=True)
    assert len(appels["on"]) == 1
    poele(hass, "Heating")
    await avancer(hass, freezer, entry, 12)
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, entry, "button", "annuler")}, blocking=True)
    await hass.async_block_till_done()
    assert len(appels["off"]) == 1


async def test_arret_de_securite_duree_max(hass, freezer):
    entry, appels = await installer(hass, freezer)
    await bascule(hass, entry, "auto")
    poele(hass, "Heating")
    await avancer(hass, freezer, entry, 15)
    assert entry.runtime_data.etat == "marche"
    await avancer(hass, freezer, entry, 8 * 60 + 10)
    assert len(appels["off"]) >= 1
    assert any("arrêt de sécurité" in a.data["message"] for a in appels["notif"])


async def test_arret_automatique_quand_tout_est_chaud(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 10:00:00+01:00", temp="21.0", cible="20.5")
    await bascule(hass, entry, "arret_auto")
    poele(hass, "Heating")
    await avancer(hass, freezer, entry, 1)
    await avancer(hass, freezer, entry, 60)
    assert appels["off"] == []                          # < 90 min de marche
    await avancer(hass, freezer, entry, 40)
    assert len(appels["off"]) == 1
    await avancer(hass, freezer, entry, 5)
    assert len(appels["off"]) == 1                      # pas de renvoi tant que la commande est récente


async def test_apprentissage_de_la_montee_et_journal(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 05:30:00+01:00", temp="17.0")
    await bascule(hass, entry, "auto")
    poele(hass, "Heating")
    t = 17.0
    for _ in range(30):                                  # 2 h 30 de chauffe, +0,6 °C/h
        t += 0.05
        hass.states.async_set(TEMP, f"{t:.2f}")
        await avancer(hass, freezer, entry, 5)
    poele(hass, "Cleaning")
    await avancer(hass, freezer, entry, 5)
    p = entry.runtime_data
    mont, appris = p.appris.montee("Chambre")
    assert appris and 0.4 < mont < 0.8
    j = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["journal"][0]
    assert j["source"] == "auto" and "Chambre" in j["pieces"]


async def test_persistance_apres_rechargement(hass, freezer):
    entry, appels = await installer(hass, freezer)
    entry.runtime_data.appris.ajouter_delai(14)
    await bascule(hass, entry, "auto")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    p = entry.runtime_data
    assert p.appris.delais == [14.0] and p.drapeaux["auto"] is True
    assert len(appels["on"]) == 1                        # pas de nouvel allumage au rechargement


async def test_carte_servie(hass, freezer, hass_client):
    entry, _ = await installer(hass, freezer)
    r = await (await hass_client()).get("/pilotage_poele/pilotage-poele-card.js")
    assert r.status == 200


async def test_formulaire_options_s_ouvre_et_enregistre(hass, freezer):
    entry, _ = await installer(hass, freezer)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.FORM, r
    import voluptuous_serialize
    from homeassistant.helpers import config_validation as cv
    voluptuous_serialize.convert(r["data_schema"], custom_serializer=cv.custom_serializer)   # comme le fait l'interface
    r = await hass.config_entries.options.async_configure(r["flow_id"], {
        "matin_actif": True, "matin_heure": "06:30:00", "soir_actif": False, "soir_heure": "18:00:00",
        "fenetre_debut": "05:00:00", "fenetre_fin": "22:00:00", "max_allumages": 2, "marche_min": 60,
        "marche_max": 400, "arret_min": 30, "marge": 1.3, "hysteresis": 0.5})
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN)[0].options["max_allumages"] == 2


async def test_reconfigurer_le_poele_et_la_piece(hass, freezer):
    entry, _ = await installer(hass, freezer)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    import voluptuous_serialize
    from homeassistant.helpers import config_validation as cv
    voluptuous_serialize.convert(r["data_schema"], custom_serializer=cv.custom_serializer)
    sub = next(iter(entry.subentries.values()))
    r = await hass.config_entries.subentries.async_init((entry.entry_id, SUBENTRY_PIECE),
                                                        context={"source": "reconfigure", "subentry_id": sub.subentry_id})
    voluptuous_serialize.convert(r["data_schema"], custom_serializer=cv.custom_serializer)


async def test_conditions_arret_affichees(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 10:00:00+01:00", temp="21.0", cible="20.5")
    await bascule(hass, entry, "arret_auto")
    poele(hass, "Heating")
    await avancer(hass, freezer, entry, 1)
    await avancer(hass, freezer, entry, 30)
    x = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["arret"]
    ids = {c["id"]: c for c in x["conditions"]}
    assert ids["actif"]["ok"] and ids["marche"]["ok"] and ids["pieces"]["ok"]
    assert not ids["duree"]["ok"] and not x["declenche"]
    assert x["arret_prevu"] is not None                  # attendu : fin des 90 min de marche
    assert ids["pieces"]["pieces"][0]["requis"] == 20.8
    # une pièce trop froide : plus d'heure prévue
    hass.states.async_set(TEMP, "20.6")
    await avancer(hass, freezer, entry, 5)
    x = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["arret"]
    assert x["arret_prevu"] is None and not x["pieces_ok"]


async def test_amorce_une_seule_fois_depuis_historique(hass, freezer):
    entry, appels = await installer(hass, freezer)
    p = entry.runtime_data
    p.appris.pieces.clear()
    p.amorce = False
    t = 1_000_000
    p.historique = [[t + k * 300, 0, None, None, [20.0 - 0.2 * (k * 300) / 3600] * len(p.pieces_cfg())] for k in range(60)]
    p._amorcer()
    assert p.amorce and p.bilan_amorce["refroid"] >= 1
    n = p.appris.pieces[p.pieces_cfg()[0][0]]["n_refroid"]
    p._amorcer()
    assert p.appris.pieces[p.pieces_cfg()[0][0]]["n_refroid"] == n     # pas de double apprentissage
    assert p._export()["amorce"] is True


# ---------- 0.5 : planning par zone ----------
PLANNING = {str(j): [{"h": "06:30", "c": 20.5}, {"h": "22:30", "c": 17.0}] for j in range(7)}


async def _planning(hass, zone="chambre", planning=PLANNING):
    await hass.services.async_call("pilotage_poele", "definir_planning", {"zone": zone, "planning": planning}, blocking=True)
    await hass.async_block_till_done()


async def test_piece_sans_zone_ni_cible_refusee(hass, freezer):
    entry, _ = await installer(hass, freezer)
    r = await hass.config_entries.subentries.async_init((entry.entry_id, SUBENTRY_PIECE), context={"source": SOURCE_USER})
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"name": "Salon", CONF_TEMPERATURE: TEMP})
    assert r["type"] is FlowResultType.FORM and r["errors"]["base"] == "zone_ou_cible"


async def test_formulaires_serialisables_planning(hass, freezer):
    import voluptuous_serialize
    from homeassistant.helpers import config_validation as cv
    entry, _ = await installer(hass, freezer, zone="chambre")
    r = await hass.config_entries.subentries.async_init((entry.entry_id, SUBENTRY_PIECE), context={"source": SOURCE_USER})
    voluptuous_serialize.convert(r["data_schema"], custom_serializer=cv.custom_serializer)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    voluptuous_serialize.convert(r["data_schema"], custom_serializer=cv.custom_serializer)


async def test_planning_donne_la_consigne_et_l_echeance(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 05:30:00+01:00", temp="17.0", zone="chambre")
    await _planning(hass)
    nombre = eid(hass, entry, "number", "consigne_chambre")
    assert float(hass.states.get(nombre).state) == 17.0          # tranche de 22:30 la veille
    p = entry.runtime_data
    await avancer(hass, freezer, entry, 1)
    assert p.plan.besoin and not p.plan.immediat
    assert p.plan.echeance.strftime("%H:%M") == "06:30"          # le changement de consigne est l'échéance
    zones = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["zones"]
    assert zones["chambre"]["source"] == "planning" and zones["chambre"]["prochain"]["c"] == 20.5


async def test_derogation_relance_un_conseil_immediat_puis_retour_planning(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 19:16:00+01:00", temp="19.5", zone="chambre")
    await _planning(hass, planning={str(j): [{"h": "06:30", "c": 19.5}, {"h": "22:30", "c": 17.0}] for j in range(7)})
    p = entry.runtime_data
    await avancer(hass, freezer, entry, 1)
    assert not p.plan.immediat                                   # 19,5 atteint : rien d'urgent
    nombre = eid(hass, entry, "number", "consigne_chambre")
    await hass.services.async_call("number", "set_value", {"entity_id": nombre, "value": 20.5}, blocking=True)
    await hass.async_block_till_done()
    assert p.plan.immediat and p.plan.allumage <= p.plan.echeance
    assert "Consigne en cours" in p.plan.raison and "20,5" in p.plan.raison
    zones = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["zones"]
    assert zones["chambre"]["source"] == "derogation"
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, entry, "button", "retour_planning_chambre")}, blocking=True)
    await hass.async_block_till_done()
    assert float(hass.states.get(nombre).state) == 19.5 and not p.plan.immediat


async def test_derogation_prend_fin_a_la_prochaine_tranche(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 22:00:00+01:00", temp="19.5", zone="maison")
    await _planning(hass, zone="maison")
    await hass.services.async_call("pilotage_poele", "derogation", {"zone": "maison", "consigne": 21.0}, blocking=True)
    nombre = eid(hass, entry, "number", "consigne_maison")
    assert float(hass.states.get(nombre).state) == 21.0
    await avancer(hass, freezer, entry, 40)                      # 22:40 : la tranche de 22:30 a repris la main
    assert float(hass.states.get(nombre).state) == 17.0
    assert not entry.runtime_data.derogations


async def test_allumage_automatique_immediat(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="19.0", zone="chambre")
    await _planning(hass)
    await bascule(hass, entry, "auto")
    await avancer(hass, freezer, entry, 1)
    assert len(appels["on"]) == 1                                # 19,0 pour 20,5 : allumage tout de suite
    assert any("sous la consigne" in a.data["message"] for a in appels["notif"])


async def test_annuler_un_conseil_immediat_l_ignore(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="19.0", zone="chambre")
    await _planning(hass)
    p = entry.runtime_data
    assert p.plan.immediat
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, entry, "button", "annuler")}, blocking=True)
    await bascule(hass, entry, "auto")
    assert appels["on"] == [] and not p.plan.immediat


async def test_arret_anticipe_si_la_consigne_va_baisser(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 22:00:00+01:00", temp="20.6", zone="chambre")
    await _planning(hass)
    await bascule(hass, entry, "arret_auto")
    poele(hass, "Heating")
    await avancer(hass, freezer, entry, 1)
    await avancer(hass, freezer, entry, 5)
    x = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["arret"]
    piece = [c for c in x["conditions"] if c["id"] == "pieces"][0]["pieces"][0]
    assert piece["anticipe"] and piece["requis"] == 17.3         # 22:30 abaisse la consigne à 17 dans moins de 45 min
    assert piece["ok"]


async def test_planning_persiste_et_valide(hass, freezer):
    entry, _ = await installer(hass, freezer, zone="chambre")
    await _planning(hass, planning={"0": [{"h": "25:00", "c": 20}, {"h": "07:00", "c": 99}, {"h": "07:00", "c": 20.3}]})
    assert entry.runtime_data.planning["chambre"] == {"0": [{"h": "07:00", "c": 20.5}]}
    assert entry.runtime_data._export()["planning"]["chambre"]
    await _planning(hass, planning={})
    assert "chambre" not in entry.runtime_data.planning
