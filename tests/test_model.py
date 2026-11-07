from datetime import datetime, time, timedelta, timezone

from custom_components.pilotage_poele.model import (
    Appris, Params, PieceMesure, classer, echeances, garde_fous, pente, planifier,
)

PARIS = timezone(timedelta(hours=1))


def now(h=5, m=0):
    return datetime(2026, 11, 5, h, m, tzinfo=PARIS)


def test_classer():
    assert classer("Heating", "heat", "heating") == "marche"
    assert classer("Ignition", "heat", None) == "allumage"
    assert classer("Cleaning", "heat", None) == "extinction"
    assert classer("Off", "off", "off") == "arret"
    assert classer("Standby", "off", None) == "arret"
    assert classer("Idle", "heat", None) == "marche"      # veille thermostatique, poêle allumé
    assert classer("Idle", "off", None) == "arret"
    assert classer("Alarm: pellet", "heat", None) == "probleme"
    assert classer("???", "heat", None) == "inconnu"
    assert classer(None, "off", None) == "arret"
    assert classer("Heating", "heat", None, disponible=False) == "inconnu"
    assert classer("unavailable", "unavailable", None) == "inconnu"


def test_pente():
    pts = [(i * 600.0, 18 + 0.5 * i * 600 / 3600) for i in range(12)]
    assert abs(pente(pts, 0, 7000) - 0.5) < 0.01
    assert pente(pts[:3], 0, 7000) is None


def test_echeances_triees_et_futures():
    e = echeances(now(8), [time(7), time(18)])
    assert [x.hour for x in e] == [18, 7, 18]
    assert all(x > now(8) for x in e)


def test_plan_calcul_a_rebours_valeurs_par_defaut():
    p = planifier(now(2), now(7), [PieceMesure("Chambre", 19.0, 20.5)], Appris(), marge=1.25)
    assert p.besoin and p.limitante == "Chambre"
    # besoin ~1,5 °C (un peu plus avec le refroidissement), 0,5 °C/h par défaut : ~3 h + 20 + 30 min, +25 %
    assert 4.0 * 60 < p.duree_min < 6.0 * 60
    assert p.allumage < p.echeance and "par défaut" in p.raison and "Chambre" in p.raison


def test_plan_pas_de_besoin():
    p = planifier(now(2), now(7), [PieceMesure("Chambre", 21.5, 20.5)], Appris())
    assert not p.besoin and p.allumage is None and "Pas besoin" in p.raison


def test_plan_pire_piece_commande_et_apprentissage_raccourcit():
    pieces = [PieceMesure("Salon", 19.5, 20.5), PieceMesure("Bureau", 17.0, 20.0)]
    a = Appris()
    p1 = planifier(now(2), now(7), pieces, a)
    assert p1.limitante == "Bureau"
    a.maj_montee("Bureau", 1.5)
    a.ajouter_delai(10)
    p2 = planifier(now(2), now(7), pieces, a)
    assert p2.duree_min < p1.duree_min


def test_plan_en_retard():
    p = planifier(now(6, 30), now(7), [PieceMesure("Chambre", 15.0, 20.0)], Appris())
    assert p.besoin and p.en_retard and p.allumage == now(6, 30)


def test_plan_sans_mesure():
    p = planifier(now(2), now(7), [PieceMesure("X", None, 20.0)], Appris())
    assert not p.besoin and "aucun calcul" in p.raison


def test_appris_bornes_et_lissage():
    a = Appris()
    a.maj_montee("A", 10)       # absurde : ignoré
    assert a.montee("A")[1] is False
    a.maj_montee("A", 1.0)
    a.maj_montee("A", 0.5)
    assert 0.5 < a.montee("A")[0] < 1.0
    a.ajouter_delai(500)        # absurde
    assert a.delais == []
    assert Appris.from_dict(a.to_dict()).montee("A") == a.montee("A")


def _params(**kw):
    base = dict(auto=True, absent=False, verrou=False, fenetre_debut=time(5), fenetre_fin=time(22, 30),
                max_allumages=3, arret_min=45)
    base.update(kw)
    return Params(**base)


def test_garde_fous():
    ok = lambda **kw: garde_fous(kw.pop("now", now(6)), kw.pop("etat", "arret"), _params(**kw.pop("p", {})),
                                  kw.pop("n", 0), kw.pop("arret", None), None, None)
    assert ok()[0]
    assert not ok(p={"auto": False})[0]
    assert not ok(p={"absent": True})[0]
    assert not ok(p={"verrou": True})[0]
    assert not ok(etat="marche")[0]
    assert not ok(etat="inconnu")[0]
    assert not ok(now=now(3))[0] and not ok(now=now(23))[0]
    assert not ok(n=3)[0]
    assert not ok(arret=now(6).timestamp() - 600)[0]
    assert ok(arret=now(6).timestamp() - 3600)[0]


def test_fichiers_json_valides():
    import json, pathlib
    base = pathlib.Path(__file__).parent.parent / "custom_components" / "pilotage_poele"
    for f in [*base.glob("translations/*.json"), base / "manifest.json"]:
        json.loads(f.read_text(encoding="utf-8"))
    fr = json.loads((base / "translations" / "fr.json").read_text(encoding="utf-8"))
    assert fr["options"]["step"]["init"]["data"]["hysteresis"].startswith("Marge")


def test_amorcer_depuis_historique():
    from custom_components.pilotage_poele.model import ETATS, Appris, amorcer_depuis_historique
    a, m, r = ETATS.index("arret"), ETATS.index("marche"), ETATS.index("allumage")
    h, t = [], 0
    def seg(n, etat, t0, pente_h):
        nonlocal t
        for k in range(n):
            t += 300
            h.append([t, etat, None, None, [t0 + pente_h * (k * 300) / 3600]])
    seg(60, a, 20.0, -0.2)                 # 5 h d'arrêt : refroidissement 0,2 °C/h
    seg(3, r, 17.0, 0.0)                   # 15 min d'allumage
    seg(36, m, 17.0, 0.8)                  # 3 h de marche : +0,8 °C/h
    seg(60, a, 20.0, -0.1)
    ap = Appris()
    b = amorcer_depuis_historique(h, ["Salon"], {"Salon": 20.5}, ap)
    assert b["refroid"] == 2 and b["montee"] == 1 and b["delais"] == 1
    assert ap.refroid("Salon")[1] and 0.1 < ap.refroid("Salon")[0] < 0.2
    assert abs(ap.montee("Salon")[0] - 0.8) < 0.1
    assert ap.delai_allumage()[1]
    # pièce déjà au-dessus de sa cible : pas de montée apprise
    ap2 = Appris()
    b2 = amorcer_depuis_historique(h, ["Salon"], {"Salon": 15.0}, ap2)
    assert b2["montee"] == 0


def test_planning_pur():
    from datetime import datetime
    from custom_components.pilotage_poele.planning import changements, consigne_a, prochain_changement, valider
    p = valider({"0": [{"h": "22:30", "c": 18}, {"h": "06:30", "c": 20.7}, {"h": "x", "c": 3}], "1": [{"h": "06:30", "c": 20.5}]})
    assert p == {"0": [{"h": "06:30", "c": 20.5}, {"h": "22:30", "c": 18.0}], "1": [{"h": "06:30", "c": 20.5}]}
    lun_5h = datetime(2026, 11, 9, 5, 0)
    assert consigne_a(p, datetime(2026, 11, 9, 7, 0))[0] == 20.5
    assert consigne_a(p, datetime(2026, 11, 9, 23, 0))[0] == 18.0
    assert consigne_a(p, lun_5h)[0] == 20.5                      # retombe sur la dernière tranche commencée
    assert [c for _, c in changements(p, lun_5h, 24)] == [20.5, 18.0]
    assert prochain_changement(p, lun_5h)[0].hour == 6
    assert consigne_a({}, lun_5h) is None and changements(None, lun_5h) == []


# ---------- température extérieure ----------
from custom_components.pilotage_poele.model import K_DEFAUT, tranche, amorcer_depuis_historique, ETATS


def test_tranches():
    assert tranche(None) is None and tranche(2) == "froid" and tranche(8) == "frais" and tranche(15) == "doux"


def test_refroidissement_proportionnel_a_l_ecart():
    a = Appris()
    chaud, _ = a.refroid("c", 6.0)
    froid, _ = a.refroid("c", 18.0)
    assert froid > chaud and abs(froid / chaud - 3) < 0.01
    assert a.refroid("c") == (0.15, False)                       # sans extérieur : comportement d'origine


def test_k_appris_remplace_la_valeur_par_defaut():
    a = Appris()
    a.maj_refroid("c", 0.3, 15.0)                                # 0,3 °C/h pour 15 °C d'écart : k = 0,02
    v, appris = a.refroid("c", 10.0)
    assert appris and abs(v - 0.2) < 1e-6
    assert a.k_perte("c") == (0.02, True)


def test_montee_par_defaut_ajustee_a_l_exterieur():
    a = Appris()
    d = a.montee_detail("c", 2.0, 20.0)                          # 18 °C d'écart > 12 °C de référence
    assert d["source"] == "defaut_ext" and not d["appris"] and d["v"] < 0.5
    d2 = a.montee_detail("c", 14.0, 20.0)
    assert d2["v"] > 0.5
    assert a.montee_detail("c")["source"] == "defaut" and a.montee_detail("c")["v"] == 0.5


def test_montee_apprise_par_tranche_et_corrigee_ailleurs():
    a = Appris()
    a.maj_montee("c", 0.4, 2.0, 20.0)                            # appris par temps froid
    assert a.montee_detail("c", 3.0, 20.0)["source"] == "tranche"
    d = a.montee_detail("c", 14.0, 20.0)                         # tranche « doux » jamais vue : correction
    assert d["source"] == "corrigee" and d["v"] > 0.4 and d["appris"]
    a.maj_montee("c", 0.7, 14.0, 20.0)
    assert a.montee_detail("c", 13.0, 20.0)["source"] == "tranche"
    assert abs(a.montee_detail("c", 13.0, 20.0)["v"] - 0.7) < 1e-6


def test_planifier_plus_tot_quand_il_fait_froid():
    pieces = [PieceMesure("c", 18.0, 20.5)]
    doux = planifier(now(), now(18), pieces, Appris(), exterieur=14.0)
    froid = planifier(now(), now(18), pieces, Appris(), exterieur=0.0)
    sans = planifier(now(), now(18), pieces, Appris())
    assert froid.allumage < sans.allumage < doux.allumage
    assert froid.exterieur == 0.0 and "Extérieur : 0,0 °C" in froid.raison
    assert "Extérieur" not in sans.raison and sans.exterieur is None


def test_amorcer_apprend_avec_l_exterieur():
    i_arret = ETATS.index("arret")
    # 8 h d'arrêt : la pièce passe de 21 à 18,6 °C (0,3 °C/h) avec 3 °C dehors (écart ≈ 16 °C)
    h = [[int(1000 + k * 600), i_arret, None, 3.0, [21.0 - 0.3 * (k * 600 / 3600)]] for k in range(48)]
    a = Appris()
    amorcer_depuis_historique(h, ["c"], {"c": 20.5}, a)
    assert a.pieces["c"].get("n_k", 0) == 1
    k, ok = a.k_perte("c")
    assert ok and 0.015 < k < 0.025


def test_ancien_refroidissement_ignore_avec_exterieur():
    a = Appris()
    a.maj_refroid("c", 0.56)                                     # mesuré sans extérieur : écart de référence inconnu
    assert a.refroid("c") == (0.56, True)                        # sans extérieur : on s'en sert comme avant
    v, appris = a.refroid("c", 11.7)                             # avec extérieur : valeur par défaut ajustée à l'écart
    assert not appris and abs(v - 0.15 * 11.7 / 12) < 1e-6
    p = planifier(now(), now(8), [PieceMesure("c", 20.3, 20.5)], a, exterieur=8.6)
    assert p.pieces[0]["besoin"] < 1.0                           # et non 2 °C comme avec 0,56 °C/h
