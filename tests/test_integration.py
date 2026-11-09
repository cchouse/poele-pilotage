from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
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


async def installer(hass, freezer, heure="2026-11-05 05:30:00+01:00", temp="17.0", cible="20.5", statut="Off", zone=None, ecart=None):
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
    if ecart is not None:
        donnees["ecart_declenchement"] = ecart
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
    plan = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["plan"]
    assert plan["besoin"] is False or (plan["seuil"] and not plan["immediat"] and not plan["en_retard"])


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
    await avancer(hass, freezer, entry, 11)               # la sortie dure : le cycle est clos
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


async def test_toutes_les_pieces_sont_exposees(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 05:30:00+01:00", temp="17.0", zone="chambre")
    await avancer(hass, freezer, entry, 1)
    pe = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["pieces_etat"]
    assert len(pe) == len(entry.runtime_data.pieces_cfg()) >= 1
    assert {"nom", "temperature", "consigne", "zone"} <= set(pe[0])


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


async def test_consigne_sans_planning_reste_sans_limite(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 22:00:00+01:00", temp="19.5", zone="maison")
    await hass.services.async_call("pilotage_poele", "derogation", {"zone": "maison", "consigne": 21.0}, blocking=True)
    nombre = eid(hass, entry, "number", "consigne_maison")
    p = entry.runtime_data
    assert p.derogations["maison"]["jusqu"] is None
    await avancer(hass, freezer, entry, 24 * 60)                 # 24 h plus tard : toujours là
    assert float(hass.states.get(nombre).state) == 21.0 and "maison" in p.derogations
    z = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["zones"]["maison"]
    assert z["derogation"]["jusqu"] is None
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, entry, "button", "retour_planning_maison")}, blocking=True)
    await hass.async_block_till_done()
    assert not p.derogations


async def test_decision_par_zone_moyenne(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="19.8", zone="maison")
    hass.states.async_set("sensor.salon_t", "21.5")
    r = await hass.config_entries.subentries.async_init((entry.entry_id, SUBENTRY_PIECE), context={"source": SOURCE_USER})
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"name": "Salon", CONF_TEMPERATURE: "sensor.salon_t", "zone": "maison"})
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    await hass.async_block_till_done()
    await _planning(hass, zone="maison", planning={str(j): [{"h": "00:00", "c": 20.5}] for j in range(7)})
    p = entry.runtime_data
    await avancer(hass, freezer, entry, 1)
    assert not (p.plan and p.plan.immediat)                      # moyenne 20,65 ≥ 20,5 : rien à faire
    z = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["zones"]["maison"]
    assert z["moyenne"] == 20.65 and len(z["pieces_temp"]) == 2
    cond = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["arret"]["conditions"]
    lignes = next(c for c in cond if c["id"] == "pieces")["pieces"]
    assert len(lignes) == 1 and "Zone Maison" in lignes[0]["nom"] and lignes[0]["temperature"] == 20.65
    hass.config_entries.async_update_entry(entry, options={**entry.options, "decision_zone": False})
    await hass.async_block_till_done()
    p = entry.runtime_data
    await avancer(hass, freezer, entry, 1)
    assert p.plan and p.plan.immediat                            # pièce par pièce : la Chambre manque 0,7 °C


async def test_allumage_automatique_immediat(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="19.0", zone="chambre")
    await _planning(hass)
    await bascule(hass, entry, "auto")
    await avancer(hass, freezer, entry, 1)
    assert len(appels["on"]) == 1                                # 19,0 pour 20,5 : allumage tout de suite
    assert any("sous la consigne" in a.data["message"] for a in appels["notif"])


async def test_ecart_propre_a_la_piece(hass, freezer):
    # 19,0 pour 20,5 : il manque 1,5 °C
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="19.0", zone="chambre", ecart=2.0)
    await _planning(hass)
    p = entry.runtime_data
    sub = next(iter(entry.subentries.values()))
    assert sub.data["ecart_declenchement"] == 2.0
    assert not p.plan.immediat                                   # 1,5 < 2,0 : la pièce attend
    pe = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["pieces_etat"][0]
    assert pe["ecart"] == 2.0 and pe["ecart_propre"] is True
    hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, "ecart_declenchement": 1.0})
    await hass.async_block_till_done()                           # la modification recharge l'intégration
    p = entry.runtime_data
    await avancer(hass, freezer, entry, 1)
    assert p.plan.immediat and "supérieur à 1,0" in p.plan.raison.replace(".", ",")


async def test_ecart_vide_prend_la_valeur_globale(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="19.0", zone="chambre")
    await _planning(hass)
    p = entry.runtime_data
    assert p.plan.immediat                                       # 1,5 > 0,5 global
    pe = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["pieces_etat"][0]
    assert pe["ecart"] == 0.5 and pe["ecart_propre"] is False


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


async def test_nettoyage_court_ne_remet_pas_les_compteurs_a_zero(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 10:00:00+01:00", temp="21.0", cible="20.5")
    await bascule(hass, entry, "arret_auto")
    poele(hass, "Heating")
    await avancer(hass, freezer, entry, 1)
    p = entry.runtime_data
    marche0 = p.run["marche"]
    for _ in range(8):                                    # toutes les 12 min : 3 min de nettoyage du brasero
        await avancer(hass, freezer, entry, 12)
        poele(hass, "Cleaning")
        await avancer(hass, freezer, entry, 3)
        poele(hass, "Heating")
        await avancer(hass, freezer, entry, 1)
    assert p.run is not None and p.run["marche"] == marche0 and not p.journal
    assert len(appels["off"]) >= 1                        # 128 min de marche : l'arrêt automatique a bien fini par se déclencher


# ---------- têtes thermostatiques ----------
TETE1, TETE2 = "climate.tete_1", "climate.tete_2"


async def _tetes(hass, entry, **opts):
    hass.config_entries.async_update_entry(entry, options={**entry.options, **opts})
    await hass.async_block_till_done()                           # recharge l'intégration
    return async_mock_service(hass, "climate", "set_temperature")


async def test_tetes_recoivent_la_consigne_de_la_zone(hass, freezer):
    hass.states.async_set(TETE1, "heat", {"min_temp": 7, "max_temp": 28})
    hass.states.async_set(TETE2, "heat", {"min_temp": 7, "max_temp": 28})
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="20.0", zone="chambre")
    await _planning(hass)
    envois = await _tetes(hass, entry, tetes_chambre=[TETE1, TETE2])
    p = entry.runtime_data
    await avancer(hass, freezer, entry, 1)
    assert sorted(a.data["entity_id"] for a in envois) == [TETE1, TETE2]
    assert all(a.data["temperature"] == 20.5 for a in envois)
    await avancer(hass, freezer, entry, 5)
    assert len(envois) == 2                                      # rien de nouveau : pas de renvoi
    info = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["tetes"]
    assert info["actif"] and info["zones"]["chambre"]["nb"] == 2 and info["zones"]["chambre"]["c"] == 20.5
    # dérogation : les têtes suivent tout de suite
    await hass.services.async_call("pilotage_poele", "derogation", {"zone": "chambre", "consigne": 21.5}, blocking=True)
    assert [a.data["temperature"] for a in envois[2:]] == [21.5, 21.5]
    # fin de journée : tranche de 22:30 à 17 °C
    freezer.move_to("2026-11-05 22:31:00+01:00")
    await entry.runtime_data._tick(None)
    assert envois[-1].data["temperature"] == 17.0


async def test_tetes_decalage_et_limites(hass, freezer):
    hass.states.async_set(TETE1, "heat", {"min_temp": 7, "max_temp": 21})
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="20.0", zone="chambre")
    await _planning(hass)
    envois = await _tetes(hass, entry, tetes_chambre=[TETE1], tetes_decalage=1.0)
    await avancer(hass, freezer, entry, 1)
    assert envois[-1].data["temperature"] == 21.0                # 20,5 + 1 = 21,5 ramené au maximum de la tête (21)


async def test_tetes_interrupteur_et_reprise(hass, freezer):
    hass.states.async_set(TETE1, "heat", {})
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="20.0", zone="chambre")
    await _planning(hass)
    envois = await _tetes(hass, entry, tetes_chambre=[TETE1])
    await avancer(hass, freezer, entry, 1)
    assert len(envois) == 1
    await bascule(hass, entry, "tetes", on=False)
    await hass.services.async_call("pilotage_poele", "derogation", {"zone": "chambre", "consigne": 22.0}, blocking=True)
    assert len(envois) == 1                                      # désactivé : rien n'est envoyé
    await bascule(hass, entry, "tetes", on=True)
    assert envois[-1].data["temperature"] == 22.0                # réactivé : resynchronisation


async def test_tete_indisponible_puis_retour(hass, freezer):
    hass.states.async_set(TETE1, "unavailable")
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="20.0", zone="chambre")
    await _planning(hass)
    envois = await _tetes(hass, entry, tetes_chambre=[TETE1])
    await avancer(hass, freezer, entry, 1)
    assert envois == []
    z = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["tetes"]["zones"]["chambre"]
    assert z["ok"] is False and "indisponible" in z["erreur"]
    hass.states.async_set(TETE1, "heat", {})
    await avancer(hass, freezer, entry, 1)
    assert len(envois) == 1 and envois[0].data["temperature"] == 20.5


async def test_options_tetes_serialisables(hass, freezer):
    import voluptuous_serialize
    from homeassistant.helpers import config_validation as cv
    entry, _ = await installer(hass, freezer)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    out = voluptuous_serialize.convert(r["data_schema"], custom_serializer=cv.custom_serializer)
    assert {"tetes_chambre", "tetes_maison", "tetes_decalage"} <= {x["name"] for x in out}
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {**{k: v for k, v in entry.options.items()}, "tetes_chambre": [TETE1], "tetes_decalage": 0.5,
                       **{k: v for k, v in __import__("custom_components.pilotage_poele.const", fromlist=["x"]).OPTIONS_DEFAUT.items()
                          if k not in ("tetes_chambre", "tetes_maison", "tetes_decalage")}})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["data"]["tetes_chambre"] == [TETE1]


async def test_exterieur_dans_le_plan(hass, freezer):
    hass.states.async_set("sensor.ext", "2.0")
    entry, _ = await installer(hass, freezer)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "exterieur": "sensor.ext"})
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await avancer(hass, freezer, entry, 1)
    p = entry.runtime_data.plan
    assert p.exterieur == 2.0 and "Extérieur : 2,0 °C" in p.raison
    assert p.pieces[0]["montee_source"] == "defaut_ext"


async def test_synthese(hass, freezer):
    hass.states.async_set("sensor.ext", "4.0")
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="19.0")
    hass.config_entries.async_update_entry(entry, data={**entry.data, "exterieur": "sensor.ext"})
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    p = entry.runtime_data
    now = p.historique and None or __import__("homeassistant.util.dt", fromlist=["x"]).now()
    ts = now.timestamp()
    arret, marche = 0, 2
    # 24 h : arrêt, puis 2 marches de 1 h (12 lignes de 5 min chacune), extérieur de 0 à 8 °C
    p.historique = []
    for k in range(288):
        t = ts - (287 - k) * 300
        etat = marche if (60 <= k < 72 or 150 <= k < 162) else arret
        p.historique.append([int(t), etat, None, 0.0 + 8.0 * k / 287, [19.0]])
    s = p._synthese(now, p._mesures())
    assert s["ext"] == 4.0 and s["ext_min"] == 0.0 and s["ext_max"] == 8.0
    assert s["dju"] == 14.0                                      # 18 − (0 + 8) / 2
    assert s["marche_min"] == 120 and s["allumages"] == 2
    assert s["int_moy"] == 19.0 and s["ecart_moy"] == 1.5 and s["nb_sous"] == 1   # cible 20,5
    assert s["perte"] > 0 and s["perte_apprise"] is False
    assert abs(s["eco_pct"] - 100 / (20.5 - 4.0)) < 0.1
    assert hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["synthese"]["ext"] == 4.0


async def test_synthese_sans_capteur_exterieur(hass, freezer):
    entry, _ = await installer(hass, freezer)
    s = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["synthese"]
    assert s["ext"] is None and "dju" not in s and "perte" not in s and "eco_pct" not in s


# ---------- loi d'eau ----------
async def _ext(hass, entry, valeur):
    hass.states.async_set("sensor.ext", str(valeur))
    if "exterieur" not in entry.data:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "exterieur": "sensor.ext"})
        await hass.async_block_till_done()
    return hass.config_entries.async_entries(DOMAIN)[0]


def _poele_eau(hass, consigne=62, statut="Heating", mn=50, mx=80):
    hass.states.async_set(POELE, "heat", {"hvac_action": "heating", "temperature": consigne, "min_temp": mn, "max_temp": mx})
    hass.states.async_set(STATUT, statut)


async def test_eau_conseillee_suit_la_courbe_et_les_bornes(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="22.5")
    _poele_eau(hass, statut="Off")
    entry = await _ext(hass, entry, 0.0)
    await avancer(hass, freezer, entry, 1)
    assert entry.runtime_data.eau_info["c"] == 70
    await _ext(hass, entry, 7.5)
    await avancer(hass, freezer, entry, 1)
    assert entry.runtime_data.eau_info["c"] == 65
    await _ext(hass, entry, 20.0)
    await avancer(hass, freezer, entry, 1)
    assert entry.runtime_data.eau_info["c"] == 60                # au-delà de 15 °C : la valeur de 15 °C
    hass.config_entries.async_update_entry(entry, options={**entry.options, "eau_t15": 50.0})
    await hass.async_block_till_done()
    await avancer(hass, freezer, entry, 1)
    assert entry.runtime_data.eau_info["c"] == 58                # jamais sous le minimum (la pompe démarre à 55 °C)
    assert "limites" in entry.runtime_data.eau_info["raison"]


async def test_eau_coup_de_pouce_si_besoin_pressant(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="19.0", zone="chambre")
    await _planning(hass)
    _poele_eau(hass, statut="Off")
    entry = await _ext(hass, entry, 15.0)
    await avancer(hass, freezer, entry, 1)
    e = entry.runtime_data.eau_info
    assert e["ajust"] == 2.0 and e["c"] == 62 and "il manque" in e["raison"]


async def test_eau_sans_capteur_exterieur_pas_de_conseil(hass, freezer):
    entry, _ = await installer(hass, freezer)
    assert entry.runtime_data.eau_info is None
    assert hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["eau_poele"]["conseil"] is None


async def test_eau_envoi_sans_lire_le_poele(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="22.5")
    entry = await _ext(hass, entry, 0.0)                         # courbe : 70 °C
    _poele_eau(hass, 25)                                         # l'entité renvoie n'importe quoi : sans effet
    envois = async_mock_service(hass, "climate", "set_temperature")
    await bascule(hass, entry, "eau")
    await avancer(hass, freezer, entry, 1)
    assert [a.data["temperature"] for a in envois] == [70]       # envoi direct de la valeur conseillée
    for _ in range(4):
        await avancer(hass, freezer, entry, 35)
    assert len(envois) == 1                                      # la valeur conseillée n'a pas changé : rien de plus
    assert not [c for c in entry.runtime_data.commandes if c["source"] == "externe"]
    assert not any("sans que le pilotage" in a.data["message"] for a in appels["notif"])


async def test_eau_rien_en_mode_conseil_ou_hors_marche(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="22.5")
    entry = await _ext(hass, entry, 0.0)
    envois = async_mock_service(hass, "climate", "set_temperature")
    _poele_eau(hass, 62)
    await avancer(hass, freezer, entry, 1)
    assert envois == []                                          # interrupteur désactivé : conseil seulement
    hass.states.async_set(STATUT, "Off")
    hass.states.async_set(POELE, "off", {"hvac_action": "off", "temperature": 62})
    await bascule(hass, entry, "eau")
    await avancer(hass, freezer, entry, 1)
    assert envois == []                                          # poêle à l'arrêt : on n'y touche pas
    assert hass.states.get(eid(hass, entry, "switch", "eau")).state == "on"


async def test_eau_desactivee_apres_trois_refus(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="22.5")
    entry = await _ext(hass, entry, 0.0)
    _poele_eau(hass, 62)
    envois = async_mock_service(hass, "climate", "set_temperature", raise_exception=HomeAssistantError("refusé"))
    await bascule(hass, entry, "eau")
    for _ in range(3):
        await avancer(hass, freezer, entry, 1)                   # envoi refusé
        await avancer(hass, freezer, entry, 35)                  # nouvel essai 30 min plus tard, sans insister
    assert len(envois) == 3
    assert hass.states.get(eid(hass, entry, "switch", "eau")).state == "off"
    assert any("désactivé" in a.data["message"] for a in appels["notif"])


# ---------- estimation sans planning : la pièce va passer sous son seuil ----------
async def test_allumage_estime_sans_planning(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="20.2", zone="chambre")
    await hass.services.async_call("pilotage_poele", "derogation", {"zone": "chambre", "consigne": 20.5}, blocking=True)
    await avancer(hass, freezer, entry, 1)
    p = entry.runtime_data.plan
    assert p.seuil and p.besoin and not p.immediat and not p.en_retard
    assert "Aucun changement de consigne prévu" in p.raison and "passera sous 20,0 °C" in p.raison
    # 0,2 °C d'écart à 0,15 °C/h : seuil dans 80 min ; allumage 62,5 min avant : dans une vingtaine de minutes
    delta = (p.allumage - dt_util.now()).total_seconds() / 60
    assert 10 < delta < 25
    await bascule(hass, entry, "auto")
    assert appels["on"] == []
    await avancer(hass, freezer, entry, 5)
    hass.states.async_set(TEMP, "20.05")                         # la pièce a refroidi : le seuil approche
    await avancer(hass, freezer, entry, 2)
    assert len(appels["on"]) == 1                                # l'heure est venue : allumage automatique
    snap = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["plan"]
    assert snap["seuil"] is True


async def test_annuler_une_estimation_sans_planning(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="20.2", zone="chambre")
    await hass.services.async_call("pilotage_poele", "derogation", {"zone": "chambre", "consigne": 20.5}, blocking=True)
    await avancer(hass, freezer, entry, 1)
    assert entry.runtime_data.plan.seuil
    await entry.runtime_data.annuler()
    assert not (entry.runtime_data.plan and entry.runtime_data.plan.seuil)
    await bascule(hass, entry, "auto")
    hass.states.async_set(TEMP, "20.05")
    await avancer(hass, freezer, entry, 5)
    assert appels["on"] == []


async def test_aucune_consigne_message_clair(hass, freezer):
    entry, _ = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="20.2", zone="chambre")
    await avancer(hass, freezer, entry, 1)
    p = entry.runtime_data.plan
    assert p is not None and not p.besoin and "Aucune pièce n'a de température et de consigne" in p.raison


# ---------- journal des commandes, correction directe, consigne étrangère ----------
async def test_journal_des_commandes(hass, freezer):
    hass.states.async_set(TETE1, "heat", {})
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="20.0", zone="chambre")
    await _planning(hass)
    await _tetes(hass, entry, tetes_chambre=[TETE1])
    await avancer(hass, freezer, entry, 1)
    await entry.runtime_data.allumer_maintenant()
    j = hass.states.get(eid(hass, entry, "sensor", "etat")).attributes["commandes"]
    assert [c["source"] for c in j][:2] == ["force", "tetes"] or {c["source"] for c in j} >= {"force", "tetes"}
    assert j[0]["service"] == "turn_on" and j[0]["entite"] == POELE                 # le plus récent d'abord
    assert any(c["entite"] == TETE1 and c["valeur"] == 20.5 for c in j)


async def test_arret_si_eau_trop_chaude(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="17.0")
    hass.config_entries.async_update_entry(entry, options={**entry.options, "arret_delta_eau": 5.0})
    await hass.async_block_till_done()
    _poele_eau(hass, 60)
    poele(hass, "Marche", mode="heat", action="heating")
    hass.states.async_set(POELE, "heat", {"hvac_action": "heating", "temperature": 60, "min_temp": 50, "max_temp": 80})
    hass.states.async_set(EAU, "64")                             # moins de 5 °C au-dessus : rien
    for _ in range(3):
        await avancer(hass, freezer, entry, 2)
    assert appels["off"] == []
    hass.states.async_set(EAU, "65.5")                           # dépassement durable : arrêt
    await avancer(hass, freezer, entry, 1)
    assert appels["off"] == []                                   # pas avant 3 min
    await avancer(hass, freezer, entry, 4)
    assert len(appels["off"]) == 1
    assert any("au-dessus de la consigne d'eau" in a.data["message"] for a in appels["notif"])


async def test_pas_d_arret_eau_si_consigne_aberrante(hass, freezer):
    entry, appels = await installer(hass, freezer, heure="2026-11-05 12:00:00+01:00", temp="17.0")
    hass.config_entries.async_update_entry(entry, options={**entry.options, "arret_delta_eau": 5.0})
    await hass.async_block_till_done()
    hass.states.async_set(POELE, "heat", {"hvac_action": "heating", "temperature": 25, "min_temp": 50, "max_temp": 80})
    hass.states.async_set(STATUT, "Marche")
    hass.states.async_set(EAU, "62")                             # consigne lue 25 °C : hors limites, ignorée
    for _ in range(4):
        await avancer(hass, freezer, entry, 3)
    assert appels["off"] == []
