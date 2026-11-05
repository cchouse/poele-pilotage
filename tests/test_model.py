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
