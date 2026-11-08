"""Logique pure (sans Home Assistant) du pilotage prédictif d'un poêle à granulés hydro.

Choix techniques (voir le rapport de recherche) :
* l'heure d'allumage se calcule À REBOURS depuis l'échéance, avec la pièce la plus lente ;
* durée = (délai d'allumage + latence eau/pièce + besoin / vitesse de montée) × marge ;
* les paramètres s'apprennent (moyenne glissante) ; tant qu'ils ne sont pas appris, des valeurs
  PRUDENTES par défaut sont utilisées et signalées comme telles ;
* jamais d'action sans passer par les garde-fous.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

ETATS = ("arret", "allumage", "marche", "extinction", "probleme", "inconnu")
ETATS_LIBELLES = {
    "arret": "Arrêté",
    "allumage": "Allumage en cours",
    "marche": "En marche",
    "extinction": "Extinction / nettoyage",
    "probleme": "Problème",
    "inconnu": "État inconnu",
}

# valeurs par défaut prudentes (non apprises)
DELAI_ALLUMAGE_MIN = 20.0     # minutes entre la commande et la marche établie
LATENCE_MIN = 30.0            # minutes avant que la chaleur arrive dans la pièce
MONTEE_DEFAUT = 0.5           # °C/h
REFROIDISSEMENT_DEFAUT = 0.15 # °C/h
ALPHA = 0.3                   # lissage des apprentissages
EXT_REF_DELTA = 12.0          # °C d'écart intérieur − extérieur pour lesquels les valeurs par défaut sont valables
K_DEFAUT = REFROIDISSEMENT_DEFAUT / EXT_REF_DELTA   # °C/h perdus par °C d'écart avec l'extérieur
TRANCHES_LIB = {"froid": "par temps froid (moins de 5 °C)", "frais": "par temps frais (5 à 12 °C)",
                "doux": "par temps doux (12 °C et plus)"}


def tranche(ext: float | None) -> str | None:
    if ext is None:
        return None
    return "froid" if ext < 5 else ("frais" if ext < 12 else "doux")


def _moy(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None

_PROBLEME = ("alarm", "alarme", "error", "erreur", "fault", "defaut", "défaut", "lock", "blocage", "fail")
_ALLUMAGE = ("ignit", "allum", "start", "démarr", "demarr", "preheat", "pre-heat", "pre heat")
_EXTINCTION = ("clean", "nettoy", "stop", "extinct", "cool", "refroid", "shut", "final")
_MARCHE = ("heat", "chauff", "run", "work", "marche", "burn", "modul", "power", "normal", "on")
_ARRET = ("off", "standby", "eteint", "éteint", "arret", "arrêt", "veille", "idle")


def classer(statut: str | None, hvac_mode: str | None, hvac_action: str | None, disponible: bool = True) -> str:
    """Traduit l'état brut du poêle en un des états ETATS. Prudent : en cas de doute -> 'inconnu'."""
    if not disponible:
        return "inconnu"
    t = (statut or "").strip().lower()
    mode = (hvac_mode or "").lower()
    action = (hvac_action or "").lower()
    if t in ("unknown", "unavailable"):
        t = ""
    if t:
        if any(k in t for k in _PROBLEME):
            return "probleme"
        if any(k in t for k in _ALLUMAGE):
            return "allumage"
        if any(k in t for k in _EXTINCTION):
            return "extinction"
        if t == "idle":
            return "marche" if mode not in ("off", "") else "arret"
        if any(k in t for k in _ARRET) and not any(k in t for k in ("heat", "chauff")):
            return "arret"
        if any(k in t for k in _MARCHE):
            return "marche"
        return "inconnu"
    if mode == "off":
        return "arret"
    if action == "heating":
        return "marche"
    if action in ("off", "idle") and mode == "off":
        return "arret"
    return "inconnu"


def pente(echantillons: list[tuple[float, float]], t0: float, t1: float) -> float | None:
    """Pente (°C/h) par régression linéaire sur [t0, t1] ; None si trop peu de données."""
    pts = [(t, v) for t, v in echantillons if t0 <= t <= t1]
    if len(pts) < 4 or pts[-1][0] - pts[0][0] < 2400:
        return None
    base = pts[0][0]
    xs = [(t - base) / 3600.0 for t, _ in pts]
    ys = [v for _, v in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


@dataclass
class Appris:
    delais: list[float] = field(default_factory=list)       # minutes commande -> marche
    pieces: dict[str, dict[str, Any]] = field(default_factory=dict)  # nom -> {montee, n_montee, refroid, n_refroid}

    def delai_allumage(self) -> tuple[float, bool]:
        if self.delais:
            d = sorted(self.delais)
            p80 = d[min(len(d) - 1, int(0.8 * len(d)))]
            return p80, True
        return DELAI_ALLUMAGE_MIN, False

    def k_perte(self, nom: str) -> tuple[float, bool]:
        """°C/h perdus par °C d'écart avec l'extérieur (appris sur les arrêts, sinon valeur par défaut)."""
        p = self.pieces.get(nom)
        if p and p.get("n_k", 0) > 0:
            return min(0.1, max(0.002, p["k"])), True
        return K_DEFAUT, False

    def montee_detail(self, nom: str, ext: float | None = None, interieur: float | None = None) -> dict[str, Any]:
        """Vitesse de montée (°C/h) avec sa provenance. Sans extérieur : comportement d'origine."""
        p = self.pieces.get(nom) or {}
        if ext is not None and interieur is not None:
            delta = interieur - ext
            t = tranche(ext)
            bt = (p.get("montee_t") or {}).get(t)
            if bt and bt.get("n", 0) > 0:
                return {"v": max(0.1, bt["v"]), "appris": True, "source": "tranche", "tranche": t}
            if p.get("n_montee", 0) > 0:
                if "delta_moy" in p:
                    k, _ = self.k_perte(nom)
                    v = p["montee"] - k * (delta - p["delta_moy"])
                    corrige = abs(delta - p["delta_moy"]) >= 1.0
                    return {"v": max(0.1, v), "appris": True, "source": "corrigee" if corrige else "appris", "tranche": t}
                return {"v": max(0.1, p["montee"]), "appris": True, "source": "appris", "tranche": t}
            return {"v": max(0.15, MONTEE_DEFAUT - K_DEFAUT * (delta - EXT_REF_DELTA)), "appris": False,
                    "source": "defaut_ext", "tranche": t}
        if p.get("n_montee", 0) > 0:
            return {"v": max(0.1, p["montee"]), "appris": True, "source": "appris", "tranche": None}
        return {"v": MONTEE_DEFAUT, "appris": False, "source": "defaut", "tranche": None}

    def montee(self, nom: str, ext: float | None = None, interieur: float | None = None) -> tuple[float, bool]:
        d = self.montee_detail(nom, ext, interieur)
        return d["v"], d["appris"]

    def refroid(self, nom: str, delta: float | None = None) -> tuple[float, bool]:
        """Refroidissement (°C/h). Avec l'écart intérieur − extérieur : proportionnel à cet écart."""
        p = self.pieces.get(nom)
        if delta is not None:
            k, k_appris = self.k_perte(nom)
            if k_appris:
                return max(0.0, k * max(delta, 0.0)), True
            # un refroidissement mesuré sans connaître l'extérieur n'a pas d'écart de référence : je l'ignore
            return min(0.6, max(0.03, REFROIDISSEMENT_DEFAUT * max(delta, 0.0) / EXT_REF_DELTA)), False
        if p and p.get("n_refroid", 0) > 0:
            return max(0.0, p["refroid"]), True
        return REFROIDISSEMENT_DEFAUT, False

    def ajouter_delai(self, minutes: float) -> None:
        if 1 <= minutes <= 90:
            self.delais = (self.delais + [round(minutes, 1)])[-20:]

    def maj_montee(self, nom: str, v: float, ext: float | None = None, interieur: float | None = None) -> None:
        if not 0.05 <= v <= 6:
            return
        p = self.pieces.setdefault(nom, {})
        n = p.get("n_montee", 0)
        p["montee"] = v if n == 0 else (1 - ALPHA) * p["montee"] + ALPHA * v
        p["n_montee"] = n + 1
        if ext is not None and interieur is not None:
            delta = interieur - ext
            p["delta_moy"] = delta if "delta_moy" not in p else (1 - ALPHA) * p["delta_moy"] + ALPHA * delta
            bt = p.setdefault("montee_t", {}).setdefault(tranche(ext), {"v": v, "n": 0})
            bt["v"] = v if bt["n"] == 0 else (1 - ALPHA) * bt["v"] + ALPHA * v
            bt["n"] += 1

    def maj_refroid(self, nom: str, v: float, delta: float | None = None) -> None:
        if not 0.0 <= v <= 3:
            return
        p = self.pieces.setdefault(nom, {})
        n = p.get("n_refroid", 0)
        p["refroid"] = v if n == 0 else (1 - ALPHA) * p["refroid"] + ALPHA * v
        p["n_refroid"] = n + 1
        if delta is not None and delta >= 3.0 and v / delta <= 0.1:
            k = v / delta
            nk = p.get("n_k", 0)
            p["k"] = k if nk == 0 else (1 - ALPHA) * p["k"] + ALPHA * k
            p["n_k"] = nk + 1

    def confiance(self, noms: list[str]) -> float:
        a = min(1.0, len(self.delais) / 5)
        if not noms:
            return round(a, 2)
        b = sum(min(1.0, self.pieces.get(n, {}).get("n_montee", 0) / 5) for n in noms) / len(noms)
        return round((a + b) / 2, 2)

    def to_dict(self) -> dict[str, Any]:
        return {"delais": self.delais, "pieces": self.pieces}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Appris":
        d = d or {}
        return cls(delais=list(d.get("delais", [])), pieces=dict(d.get("pieces", {})))


@dataclass
class PieceMesure:
    nom: str
    temperature: float | None
    cible: float | None


@dataclass
class Plan:
    besoin: bool
    echeance: datetime | None = None
    allumage: datetime | None = None
    duree_min: float = 0.0
    limitante: str | None = None
    pieces: list[dict[str, Any]] = field(default_factory=list)
    raison: str = ""
    en_retard: bool = False
    immediat: bool = False
    exterieur: float | None = None
    seuil: bool = False          # estimé sans changement de consigne : la pièce va passer sous son seuil


def _v(x: float, n: int = 1) -> str:
    return f"{x:.{n}f}".replace(".", ",")


def note_montee(d: dict[str, Any]) -> str:
    """Précision (entre parenthèses) sur l'origine de la vitesse de montée d'une pièce."""
    return {"tranche": f" (apprise {TRANCHES_LIB.get(d.get('tranche'), '')})".replace(" )", ")"),
            "corrigee": " (apprise, corrigée selon l'extérieur)",
            "defaut_ext": " (valeur par défaut ajustée à l'extérieur, pas encore apprise)",
            "defaut": " (valeur par défaut, pas encore apprise)"}.get(d.get("montee_source"), "")


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def echeances(now: datetime, heures: list[time]) -> list[datetime]:
    """Prochaines échéances (aujourd'hui puis demain) triées, strictement dans le futur."""
    out = []
    for jour in (0, 1):
        for h in heures:
            dt = datetime.combine(now.date() + timedelta(days=jour), h, tzinfo=now.tzinfo)
            if dt > now:
                out.append(dt)
    return sorted(out)


def planifier(
    now: datetime,
    echeance: datetime,
    pieces: list[PieceMesure],
    appris: Appris,
    marge: float = 1.25,
    deja_en_marche: bool = False,
    exterieur: float | None = None,
) -> Plan:
    """Calcule l'heure d'allumage à rebours depuis l'échéance, avec la pièce la plus lente."""
    mesurables = [p for p in pieces if p.temperature is not None and p.cible is not None]
    if not mesurables:
        return Plan(False, echeance, raison="Je n'ai pas de pièce avec une température et une cible : aucun calcul possible.")
    delai, delai_appris = appris.delai_allumage()

    def duree(heures_avant: float) -> tuple[float, list[dict[str, Any]]]:
        det = []
        pire = 0.0
        for p in mesurables:
            refr, refr_appris = appris.refroid(p.nom, None if exterieur is None else p.temperature - exterieur)
            prevu = p.temperature - refr * max(0.0, heures_avant)
            besoin = max(0.0, p.cible - prevu)
            dm = appris.montee_detail(p.nom, exterieur, (p.temperature + p.cible) / 2.0)
            mont, mont_appris = dm["v"], dm["appris"]
            d = besoin / mont * 60.0
            pire = max(pire, d)
            det.append({
                "nom": p.nom, "temperature": round(p.temperature, 2), "cible": round(p.cible, 2),
                "prevue": round(prevu, 2), "besoin": round(besoin, 2), "montee": round(mont, 2),
                "montee_apprise": mont_appris, "montee_source": dm["source"], "tranche": dm["tranche"],
                "duree_min": round(d), "refroid_appris": refr_appris,
            })
        return pire, det

    pire, det = duree(0.0)
    total = (delai + LATENCE_MIN + pire) * marge
    debut = echeance - timedelta(minutes=total)
    h_avant = max(0.0, (debut - now).total_seconds() / 3600.0)
    pire, det = duree(h_avant)             # 2e passe : on tient compte du refroidissement avant l'allumage
    total = (delai + LATENCE_MIN + pire) * marge
    debut = echeance - timedelta(minutes=total)
    det.sort(key=lambda d: -d["duree_min"])
    besoin_max = max(d["besoin"] for d in det)
    limit = det[0]["nom"]
    if besoin_max <= 0.1:
        return Plan(False, echeance, None, 0.0, limit, det,
                    f"Pas besoin d'allumer avant {_hhmm(echeance)} : toutes les pièces seront à leur température.")
    retard = debut < now
    allumage = now if retard else debut
    d0 = det[0]
    note = note_montee(d0)
    parts = [
        f"Allumage conseillé à {_hhmm(allumage)} pour que « {limit} » soit à {_v(d0['cible'])} °C à {_hhmm(echeance)} : "
        f"il manquera {_v(d0['besoin'])} °C, la pièce monte de {_v(d0['montee'])} °C/h{note}, "
        f"plus {round(delai)} min d'allumage{'' if delai_appris else ' (par défaut)'} et {round(LATENCE_MIN)} min de latence, "
        f"avec {round((marge - 1) * 100)} % de marge."
    ]
    if retard:
        parts.append("C'est déjà tard : à allumer maintenant, l'objectif sera peut-être atteint un peu après.")
    if exterieur is not None:
        parts.append(f"Extérieur : {_v(exterieur)} °C pris en compte.")
    return Plan(True, echeance, allumage, round(total), limit, det, " ".join(parts), retard, exterieur=exterieur)


@dataclass
class Params:
    auto: bool
    absent: bool
    verrou: bool
    fenetre_debut: time
    fenetre_fin: time
    max_allumages: int
    arret_min: float


def garde_fous(now: datetime, etat: str, p: Params, allumages_aujourdhui: int, dernier_arret: float | None,
               plan: Plan | None, ignore_echeance: float | None) -> tuple[bool, str]:
    """(autorisé, raison). L'allumage automatique n'a lieu que si TOUT est vert."""
    if not p.auto:
        return False, "Allumage automatique désactivé : mode conseil."
    if p.verrou:
        return False, "Verrouillé après un échec d'allumage : réactivez l'allumage automatique après vérification du poêle."
    if p.absent:
        return False, "Mode absent : aucun allumage automatique."
    if etat != "arret":
        return False, f"Le poêle n'est pas à l'arrêt ({ETATS_LIBELLES.get(etat, etat).lower()})."
    if not (p.fenetre_debut <= now.timetz().replace(tzinfo=None) <= p.fenetre_fin):
        return False, f"Hors de la plage autorisée ({p.fenetre_debut.strftime('%H:%M')}–{p.fenetre_fin.strftime('%H:%M')})."
    if allumages_aujourdhui >= p.max_allumages:
        return False, f"Limite atteinte : {p.max_allumages} allumages automatiques aujourd'hui."
    if dernier_arret is not None and (now.timestamp() - dernier_arret) < p.arret_min * 60:
        return False, f"Arrêt trop récent : j'attends {round(p.arret_min)} min entre deux allumages."
    if plan is not None and plan.echeance is not None and ignore_echeance is not None and abs(plan.echeance.timestamp() - ignore_echeance) < 1:
        return False, "Allumage annulé par vous pour cette échéance."
    return True, "Toutes les conditions sont réunies."


def _delta(sub: list, ech: list[tuple[float, float]], t0: float, t1: float) -> float | None:
    """Écart moyen intérieur − extérieur sur une fenêtre de l'historique (None sans mesure extérieure)."""
    ext = _moy([h[3] for h in sub if h[3] is not None and t0 <= h[0] <= t1])
    inte = _moy([v for t, v in ech if t0 <= t <= t1])
    return None if ext is None or inte is None else inte - ext


def amorcer_depuis_historique(historique: list, noms: list[str], cibles: dict[str, float | None],
                              appris: "Appris") -> dict[str, int]:
    """Apprend à partir de l'historique déjà enregistré (lignes [ts, état, eau, ext, [températures]]).

    Mêmes règles que l'apprentissage en direct : refroidissement mesuré sur les arrêts d'au moins 3 h
    (1 h de repos ignorée), montée mesurée sur les marches (30 min après le passage en marche) si la pièce
    n'était pas déjà au-dessus de sa cible, délai d'allumage entre l'état « allumage » et « marche ».
    """
    lignes = [h for h in historique if len(h) >= 5 and isinstance(h[4], list) and len(h[4]) == len(noms)]
    bilan = {"refroid": 0, "montee": 0, "delais": 0}
    if len(lignes) < 8:
        return bilan
    i_arret, i_allum, i_marche = ETATS.index("arret"), ETATS.index("allumage"), ETATS.index("marche")
    # découpe en segments d'état homogène (allumage et marche regroupés en « marche du poêle »)
    segs: list[list] = []   # [type, début, fin, i0, i1]
    for k, h in enumerate(lignes):
        e = h[1]
        t = "arret" if e == i_arret else ("on" if e in (i_allum, i_marche) else "autre")
        if segs and segs[-1][0] == t and h[0] - lignes[k - 1][0] < 1800:
            segs[-1][2], segs[-1][4] = h[0], k
        else:
            segs.append([t, h[0], h[0], k, k])
    for t, deb, fin, i0, i1 in segs:
        sub = lignes[i0:i1 + 1]
        if t == "arret" and fin - deb >= 3 * 3600:
            for j, nom in enumerate(noms):
                ech = [(h[0], h[4][j]) for h in sub if h[4][j] is not None]
                p = pente(ech, deb + 5400, fin)
                if p is not None and p < 0:
                    delta = _delta(sub, ech, deb + 5400, fin)
                    appris.maj_refroid(nom, -p, delta)
                    bilan["refroid"] += 1
        elif t == "on":
            tm = next((h[0] for h in sub if h[1] == i_marche), None)
            if tm is None:
                continue
            ta = next((h[0] for h in sub if h[1] == i_allum), None)
            if ta is not None and ta < tm:
                before = len(appris.delais)
                appris.ajouter_delai((tm - ta) / 60.0)
                bilan["delais"] += len(appris.delais) - before
            for j, nom in enumerate(noms):
                ech = [(h[0], h[4][j]) for h in sub if h[4][j] is not None]
                t0 = next((v for ts_, v in ech if ts_ >= tm), None)
                cible = cibles.get(nom)
                p = pente(ech, tm + 1800, fin)
                if p is not None and p > 0 and t0 is not None and (cible is None or t0 < cible - 0.2):
                    ext_m = _moy([h[3] for h in sub if h[3] is not None and tm + 1800 <= h[0] <= fin])
                    int_m = _moy([v for ts_, v in ech if tm + 1800 <= ts_ <= fin])
                    appris.maj_montee(nom, p, ext_m, int_m if ext_m is not None else None)
                    bilan["montee"] += 1
    return bilan
