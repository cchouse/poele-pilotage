"""Exécution : lit les entités, apprend, planifie, et (si autorisé) commande le poêle."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CIBLE, CONF_ECART, CONF_EAU, CONF_EXTERIEUR, CONF_NOTIF, CONF_POELE, CONF_STATUT, CONF_TEMPERATURE, CONF_ZONE,
    COMMANDES_MAX, DOMAIN, EAU_ECART_MIN, EAU_INTERVALLE_S, EAU_MAX_PAR_JOUR, EAU_PAS_MAX, ECHANTILLON_S, GRACE_FIN_CYCLE_S, HISTO_MAX, JOURNAL_MAX, OPTIONS_DEFAUT, TICK_S,
)
from .model import (
    ETATS, ETATS_LIBELLES, Appris, Params, PieceMesure, Plan, amorcer_depuis_historique, classer, echeances,
    garde_fous, pente, planifier,
)
from .model import LATENCE_MIN, _v, note_montee

from .planning import ZONES, changements, consigne_a, prochain_changement, valider

_LOGGER = logging.getLogger(__name__)
SIGNAL = f"{DOMAIN}_maj_{{}}"


def _float(hass: HomeAssistant, eid: str | None) -> float | None:
    if not eid:
        return None
    st = hass.states.get(eid)
    if st is None or st.state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(st.state)
    except ValueError:
        return None


def _sans_paren(n: str) -> str:
    n = n.strip()
    return f", {n[1:-1]}" if n.startswith("(") and n.endswith(")") else ""


def _heure(s: str) -> time:
    h = str(s).split(":")
    return time(int(h[0]), int(h[1]) if len(h) > 1 else 0)


class Pilote:
    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.appris = Appris()
        self.drapeaux = {"auto": False, "arret_auto": False, "absent": False, "verrou": False, "tetes": True, "eau": False}
        self._tetes_envoye: dict[str, float] = {}     # tête -> dernière consigne envoyée
        self.tetes_info: dict[str, dict[str, Any]] = {}
        self.eau_changes: list[float] = []          # horodatages des changements de consigne d'eau
        self.eau_info: dict[str, Any] | None = None  # eau conseillée (courbe + ajustement)
        self.eau_envoi: dict[str, Any] | None = None  # dernier envoi : c, ts, verifie, erreur
        self._eau_echecs = 0
        self.journal: list[dict[str, Any]] = []
        self.historique: list[list[Any]] = []
        self.dernier_arret: float | None = None
        self.allumages: list[float] = []
        self.ignore_echeance: float | None = None
        self.cmd: dict[str, Any] | None = None      # commande envoyée par nous, pas encore confirmée
        self.run: dict[str, Any] | None = None
        self.etat = "inconnu"
        self.statut_brut: str | None = None
        self.plan: Plan | None = None
        self.autorisation: tuple[bool, str] = (False, "")
        self.derniere_action: dict[str, Any] | None = None
        self.commandes: list[dict[str, Any]] = []       # journal de tout ce que j'envoie au poêle et aux têtes
        self._eau_vue: float | None = None             # dernière consigne du poêle observée (détection d'une valeur étrangère)
        self.echantillons: dict[str, list[tuple[float, float]]] = {}
        self._froid_appris_pour: float | None = None
        self._cmd_off_ts: float | None = None
        self._stable_depuis: float | None = None
        self._dernier_echantillon = 0.0
        self.planning: dict[str, dict[str, Any]] = {}
        self.derogations: dict[str, dict[str, float]] = {}
        self.ignore_immediat = 0.0
        self.amorce = False
        self.bilan_amorce: dict[str, int] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._verrou_tick = asyncio.Lock()

    # ---------- configuration ----------
    @property
    def cfg(self) -> dict[str, Any]:
        return {**self.entry.data}

    @property
    def opt(self) -> dict[str, Any]:
        return {**OPTIONS_DEFAUT, **self.entry.options}

    def pieces_cfg(self) -> list[tuple[str, dict[str, Any]]]:
        return [(s.title, dict(s.data)) for s in self.entry.subentries.values()]

    @property
    def exterieur(self) -> float | None:
        return _float(self.hass, self.cfg.get(CONF_EXTERIEUR))

    @property
    def signal(self) -> str:
        return SIGNAL.format(self.entry.entry_id)

    # ---------- cycle de vie ----------
    async def async_demarrer(self) -> None:
        d = await self.store.async_load() or {}
        self.appris = Appris.from_dict(d.get("appris"))
        self.drapeaux.update(d.get("drapeaux", {}))
        self.journal = d.get("journal", [])[-JOURNAL_MAX:]
        self.historique = d.get("historique", [])[-HISTO_MAX:]
        self.dernier_arret = d.get("dernier_arret")
        self.allumages = [t for t in d.get("allumages", []) if t > dt_util.utcnow().timestamp() - 172800]
        self.ignore_echeance = d.get("ignore_echeance")
        self.commandes = list(d.get("commandes", []))[-COMMANDES_MAX:]
        self.eau_changes = [t for t in d.get("eau_changes", []) if t > dt_util.utcnow().timestamp() - 86400]
        self.cmd = d.get("cmd")
        self.run = d.get("run")
        self._cmd_off_ts = d.get("cmd_off")
        self.amorce = bool(d.get("amorce", False))
        self.planning = {z: valider(p) for z, p in (d.get("planning") or {}).items() if z in ZONES}
        self.derogations = {z: v for z, v in (d.get("derogations") or {}).items() if z in ZONES}
        for z, v in self.derogations.items():
            if not self.planning.get(z):                  # anciennes dérogations de 12 h : sans planning, elles n'ont plus de fin
                v["jusqu"] = None
        self.ignore_immediat = float(d.get("ignore_immediat") or 0.0)
        ids = [self.cfg[CONF_POELE]] + [self.cfg.get(k) for k in (CONF_STATUT, CONF_EAU, CONF_EXTERIEUR)]
        for _, c in self.pieces_cfg():
            ids += [c.get(CONF_TEMPERATURE), c.get(CONF_CIBLE)]
        ids = [i for i in ids if i]
        self._unsubs.append(async_track_state_change_event(self.hass, ids, self._changement))
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=TICK_S)))
        self._reconstruire_echantillons()
        self._amorcer()
        self.etat = self._lire_etat()
        await self._tick(None)

    def _reconstruire_echantillons(self) -> None:
        """Remet en mémoire les températures déjà enregistrées (sinon un redémarrage fait perdre les cycles en cours)."""
        noms = [n for n, _ in self.pieces_cfg()]
        for j, nom in enumerate(noms):
            self.echantillons[nom] = [(float(h[0]), float(h[4][j])) for h in self.historique
                                      if len(h) >= 5 and isinstance(h[4], list) and len(h[4]) == len(noms)
                                      and h[4][j] is not None]

    def _amorcer(self) -> None:
        """Une seule fois : apprend ce qui est déjà visible dans l'historique (refroidissement, montées, délai)."""
        if self.amorce or not self.historique:
            return
        self.amorce = True
        noms = [n for n, _ in self.pieces_cfg()]
        cibles = {m.nom: m.cible for m in self._mesures()}
        self.bilan_amorce = amorcer_depuis_historique(self.historique, noms, cibles, self.appris)
        _LOGGER.info("Pilotage poêle : apprentissage initial depuis l'historique : %s", self.bilan_amorce)
        self._sauver()

    async def async_arreter(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self.store.async_save(self._export())

    def _export(self) -> dict[str, Any]:
        return {
            "appris": self.appris.to_dict(), "drapeaux": self.drapeaux, "journal": self.journal[-JOURNAL_MAX:],
            "historique": self.historique[-HISTO_MAX:], "dernier_arret": self.dernier_arret,
            "allumages": self.allumages, "ignore_echeance": self.ignore_echeance, "cmd": self.cmd, "run": self.run,
            "cmd_off": self._cmd_off_ts, "amorce": self.amorce, "planning": self.planning,
            "derogations": self.derogations, "ignore_immediat": self.ignore_immediat, "eau_changes": self.eau_changes,
            "commandes": self.commandes[-COMMANDES_MAX:],
        }

    def _sauver(self) -> None:
        self.store.async_delay_save(self._export, 30)

    @callback
    def _changement(self, _event) -> None:
        self.hass.async_create_task(self._tick(None))

    # ---------- lecture ----------
    def _lire_etat(self) -> str:
        st = self.hass.states.get(self.cfg[CONF_POELE])
        if st is None or st.state in ("unavailable",):
            self.statut_brut = None
            return "inconnu"
        statut = None
        if self.cfg.get(CONF_STATUT):
            s2 = self.hass.states.get(self.cfg[CONF_STATUT])
            statut = None if s2 is None else s2.state
        self.statut_brut = statut or st.attributes.get("hvac_action") or st.state
        return classer(statut, st.state, st.attributes.get("hvac_action"), True)

    # ---------- consignes par zone ----------
    def _zone_active(self, zone: str | None, quand: datetime) -> bool:
        if not zone or zone not in ZONES:
            return False
        d = self.derogations.get(zone)
        return bool(self.planning.get(zone)) or bool(d and self._derogation_active(d, quand.timestamp()))

    @staticmethod
    def _derogation_active(d: dict[str, Any], ts: float) -> bool:
        """Sans fin (jusqu = None) : la consigne reste jusqu'à ce qu'on la change ou l'efface."""
        return d.get("jusqu") is None or ts < d["jusqu"]

    def consigne_zone(self, zone: str, quand: datetime) -> tuple[float, str] | None:
        """(consigne, source) d'une zone à un instant : dérogation en cours, sinon planning."""
        d = self.derogations.get(zone)
        if d and self._derogation_active(d, quand.timestamp()):
            return float(d["c"]), "derogation"
        cp = consigne_a(self.planning.get(zone), quand)
        return (cp[0], "planning") if cp else None

    def _cible_piece(self, c: dict[str, Any], quand: datetime) -> float | None:
        z = c.get(CONF_ZONE)
        if self._zone_active(z, quand):
            r = self.consigne_zone(z, quand)
            return None if r is None else r[0]
        return _float(self.hass, c.get(CONF_CIBLE))

    def _mesures(self, quand: datetime | None = None, garder: Callable[[dict[str, Any]], bool] | None = None,
                 brut: bool = False) -> list[PieceMesure]:
        """Mesures des pièces. Avec « décider par zone » (sauf brut=True), les pièces d'une zone active
        partagent la moyenne des sondes de leur zone : c'est elle qui est comparée à la consigne de la zone."""
        quand = quand or dt_util.now()
        out = []
        zones: dict[str, list[float]] = {}
        zone_de: dict[str, str] = {}
        for nom, c in self.pieces_cfg():
            cible = self._cible_piece(c, quand)
            if garder is not None and not garder(c):
                cible = None
            t = _float(self.hass, c.get(CONF_TEMPERATURE))
            out.append(PieceMesure(nom, t, cible))
            z = c.get(CONF_ZONE)
            if t is not None and self._zone_active(z, quand):
                zones.setdefault(z, []).append(t)
                zone_de[nom] = z
        if brut or not self.opt.get("decision_zone", True):
            return out
        moy = {z: sum(v) / len(v) for z, v in zones.items() if len(v) >= 2}
        return [replace(m, temperature=moy[zone_de[m.nom]], zone=ZONES[zone_de[m.nom]], nb_zone=len(zones[zone_de[m.nom]]))
                if m.nom in zone_de and zone_de[m.nom] in moy else m for m in out]

    def _piece_classique(self, c: dict[str, Any], now: datetime) -> bool:
        """Pièce suivie par les échéances fixes du matin et du soir (pas de planning de zone)."""
        return not self._zone_active(c.get(CONF_ZONE), now)

    def _fin_derogation(self, zone: str, now: datetime) -> float | None:
        """Fin de la dérogation : prochaine tranche du planning ; sans planning, aucune fin."""
        if not self.planning.get(zone):
            return None
        pc = prochain_changement(self.planning.get(zone), now)
        return pc[0].timestamp() if pc else now.timestamp() + 12 * 3600

    async def definir_planning(self, zone: str, planning: Any) -> None:
        if zone not in ZONES:
            raise ValueError(f"Zone inconnue : {zone}")
        propre = valider(planning)
        if propre:
            self.planning[zone] = propre
        else:
            self.planning.pop(zone, None)
        d = self.derogations.get(zone)
        if d and d.get("jusqu") is None and propre:      # un planning vient d'être saisi : la consigne fixe s'arrête à la prochaine tranche
            d["jusqu"] = self._fin_derogation(zone, dt_util.now())
        self._sauver()
        await self._tick(None)

    async def definir_derogation(self, zone: str, consigne: float) -> None:
        if zone not in ZONES:
            raise ValueError(f"Zone inconnue : {zone}")
        now = dt_util.now()
        self.derogations[zone] = {"c": round(float(consigne) * 2) / 2, "jusqu": self._fin_derogation(zone, now)}
        self.ignore_immediat = 0.0
        self._sauver()
        await self._tick(None)

    async def retour_planning(self, zone: str) -> None:
        self.derogations.pop(zone, None)
        self._sauver()
        await self._tick(None)

    def _params(self) -> Params:
        o = self.opt
        return Params(
            auto=self.drapeaux["auto"], absent=self.drapeaux["absent"], verrou=self.drapeaux["verrou"],
            fenetre_debut=_heure(o["fenetre_debut"]), fenetre_fin=_heure(o["fenetre_fin"]),
            max_allumages=int(o["max_allumages"]), arret_min=float(o["arret_min"]),
        )

    # ---------- boucle ----------
    async def _tick(self, _now) -> None:
        async with self._verrou_tick:
            await self._tick_interne()

    async def _tick_interne(self) -> None:
        now = dt_util.now()
        ts = now.timestamp()
        for z in [z for z, d in self.derogations.items() if not self._derogation_active(d, ts)]:
            self.derogations.pop(z)            # dérogation terminée : retour au planning
            self._sauver()
        brut = self._mesures(brut=True)            # sondes réelles : échantillons, historique, apprentissage
        mesures = self._mesures()                  # décisions : moyennes de zone si l'option est active
        # échantillons pour l'apprentissage et l'historique
        if ts - self._dernier_echantillon >= ECHANTILLON_S:
            self._dernier_echantillon = ts
            for m in brut:
                if m.temperature is not None:
                    self.echantillons.setdefault(m.nom, []).append((ts, m.temperature))
                    self.echantillons[m.nom] = [e for e in self.echantillons[m.nom] if e[0] > ts - 43200]
            self.historique.append([int(ts), ETATS.index(self.etat), _float(self.hass, self.cfg.get(CONF_EAU)),
                                    _float(self.hass, self.cfg.get(CONF_EXTERIEUR)),
                                    [None if m.temperature is None else round(m.temperature, 2) for m in brut]])
            self.historique = self.historique[-HISTO_MAX:]
            self._sauver()
        # état du poêle et transitions
        nouveau = self._lire_etat()
        if nouveau != self.etat:
            await self._transition(self.etat, nouveau, ts, brut)
            self.etat = nouveau
        if self.run and self.run.get("sorti") and self.etat in ("arret", "extinction") \
                and ts - self.run["sorti"] >= GRACE_FIN_CYCLE_S:
            self._terminer_run(self.run["sorti"], brut)   # vraie fin : le poêle n'est pas revenu en marche
        self._apprendre_refroidissement(ts, brut)
        # sécurités d'abord (elles peuvent verrouiller), puis plan, garde-fous et décision
        await self._securites(now, ts, mesures)
        self.plan = self._calculer_plan(now, mesures)
        self.autorisation = garde_fous(now, self.etat, self._params(), self._allumages_aujourdhui(now),
                                       self.dernier_arret, self.plan, self.ignore_echeance)
        await self._decider(now, ts)
        await self._piloter_tetes(now, ts)
        self.eau_info = self._eau_conseillee(now)
        await self._regler_eau(now, ts)
        async_dispatcher_send(self.hass, self.signal)

    # ---------- loi d'eau ----------
    def _bornes_eau(self) -> tuple[float, float]:
        o = self.opt
        lo, hi = float(o["eau_min"]), float(o["eau_max"])
        st = self.hass.states.get(self.cfg[CONF_POELE])
        if st is not None:
            a, b = st.attributes.get("min_temp"), st.attributes.get("max_temp")
            if isinstance(a, (int, float)):
                lo = max(lo, float(a))
            if isinstance(b, (int, float)):
                hi = min(hi, float(b))
        return lo, max(lo, hi)

    def _eau_conseillee(self, now: datetime) -> dict[str, Any] | None:
        """Consigne d'eau conseillée : courbe selon l'extérieur, plus un coup de pouce si le besoin est pressant."""
        ext = self.exterieur
        if ext is None:
            return None
        o = self.opt
        t0, t15 = float(o["eau_t0"]), float(o["eau_t15"])
        base = t0 + (t15 - t0) * min(15.0, max(0.0, ext)) / 15.0
        pl = self.plan
        besoin = 0.0
        if pl and pl.besoin and pl.pieces and (pl.immediat or pl.en_retard
                                                or (pl.allumage and (pl.allumage - now) <= timedelta(hours=2))):
            besoin = max(p["besoin"] for p in pl.pieces)
        aj = 4.0 if besoin >= 2 else (2.0 if besoin >= 1 else 0.0)
        lo, hi = self._bornes_eau()
        c = round(min(hi, max(lo, base + aj)))
        raison = f"{_v(ext)} °C dehors : la courbe donne {_v(base, 0)} °C"
        if aj:
            raison += f", plus {_v(aj, 0)} °C car il manque {_v(besoin)} °C dans la pièce la plus lente"
        if c != round(base + aj):
            raison += f" ; ramené à {c} °C par les limites ({round(lo)}–{round(hi)} °C)"
        return {"c": c, "base": round(base, 1), "ajust": aj, "raison": raison + ".", "min": lo, "max": hi}

    async def _regler_eau(self, now: datetime, ts: float) -> None:
        """Envoie la consigne d'eau au poêle, par petits pas et seulement pendant la marche. Désactivé par défaut."""
        poele = self.cfg[CONF_POELE]
        e = self.eau_envoi
        if e and not e.get("verifie") and ts - e["ts"] >= 90:        # le poêle a-t-il bien pris la consigne ?
            st = self.hass.states.get(poele)
            cur = None if st is None else st.attributes.get("temperature")
            if isinstance(cur, (int, float)) and abs(float(cur) - e["c"]) <= 0.6:
                e["verifie"], e["erreur"], self._eau_echecs = True, None, 0
            else:
                e["verifie"], e["erreur"] = True, "le poêle n'a pas pris la consigne"
                self._eau_echecs += 1
                if self._eau_echecs >= 3:
                    self.drapeaux["eau"] = False
                    self._sauver()
                    await self._notifier("Poêle : le réglage automatique de l'eau est désactivé, le poêle n'a pas pris "
                                         "la consigne 3 fois de suite. Vérifiez son entité.")
        await self._surveiller_consigne_eau(ts)
        if not self.drapeaux.get("eau") or self.eau_info is None or self.etat != "marche":
            return
        st = self.hass.states.get(poele)
        cur = None if st is None else st.attributes.get("temperature")
        if not isinstance(cur, (int, float)):
            return
        cur = float(cur)
        lo, hi = self._bornes_eau()
        cible = float(self.eau_info["c"])
        hors_limites = not lo <= cur <= hi
        if not hors_limites and abs(cible - cur) < EAU_ECART_MIN:
            return
        if e and ts - e["ts"] < (300 if hors_limites else EAU_INTERVALLE_S):
            return
        self.eau_changes = [t for t in self.eau_changes if t > ts - 86400]
        if len(self.eau_changes) >= EAU_MAX_PAR_JOUR:
            return
        if hors_limites:                               # valeur aberrante : retour direct à la consigne conseillée
            nouveau = round(min(hi, max(lo, cible)))
        else:
            nouveau = round(min(hi, max(lo, cur + max(-EAU_PAS_MAX, min(EAU_PAS_MAX, cible - cur)))))
        if nouveau == round(cur):
            return
        raison = (f"correction : consigne de {round(cur)} °C hors des limites {round(lo)}–{round(hi)} °C" if hors_limites
                  else f"loi d'eau : {round(cur)} → {nouveau} °C")
        try:
            async with asyncio.timeout(20):
                await self.hass.services.async_call("climate", "set_temperature",
                                                    {"entity_id": poele, "temperature": nouveau}, blocking=True)
            self.eau_envoi = {"c": nouveau, "ts": ts, "verifie": False, "erreur": None, "avant": cur}
            self._noter_commande(poele, "set_temperature", "eau", nouveau, raison)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Pilotage poêle : consigne d'eau refusée : %s", err)
            self.eau_envoi = {"c": nouveau, "ts": ts, "verifie": True, "erreur": str(err), "avant": cur}
            self._noter_commande(poele, "set_temperature", "eau", nouveau, raison, str(err))
            self._eau_echecs += 1
        self.eau_changes.append(ts)
        self._sauver()

    async def _surveiller_consigne_eau(self, ts: float) -> None:
        """Signale une consigne du poêle qui saute hors des limites sans que je l'aie envoyée."""
        st = self.hass.states.get(self.cfg[CONF_POELE])
        cur = None if st is None else st.attributes.get("temperature")
        if not isinstance(cur, (int, float)):
            return
        cur = float(cur)
        avant, self._eau_vue = self._eau_vue, cur
        if avant is None or cur == avant:
            return
        lo, hi = self._bornes_eau()
        envoye = self.eau_envoi and abs(self.eau_envoi["c"] - cur) < 0.6 and ts - self.eau_envoi["ts"] < 600
        if lo <= avant <= hi and not lo <= cur <= hi and not envoye:
            self._noter_commande(self.cfg[CONF_POELE], "set_temperature", "externe", cur,
                                 f"consigne reçue d'ailleurs, hors des limites {round(lo)}–{round(hi)} °C")
            await self._notifier(f"Poêle : la consigne est passée de {_v(avant, 0)} à {_v(cur, 0)} °C sans que le pilotage "
                                 f"poêle l'ait envoyée (limites {round(lo)}–{round(hi)} °C). Origine à chercher : programmation "
                                 "du poêle ou de l'application Duepi, automatisation, autre intégration.")

    # ---------- têtes thermostatiques ----------
    def _tetes_zone(self, zone: str) -> list[str]:
        return [e for e in (self.opt.get(f"tetes_{zone}") or []) if e]

    async def _piloter_tetes(self, now: datetime, ts: float) -> None:
        """Envoie la consigne de chaque zone à ses têtes, uniquement quand elle change (ou au démarrage)."""
        if not self.drapeaux.get("tetes", True):
            return
        decalage = float(self.opt["tetes_decalage"])
        for z in ZONES:
            ents = self._tetes_zone(z)
            cs = self.consigne_zone(z, now) if ents else None
            if not ents or cs is None:
                continue
            cible = round((cs[0] + decalage) * 2) / 2
            erreurs: list[str] = []
            for e in ents:
                if self._tetes_envoye.get(e) == cible:
                    continue
                st = self.hass.states.get(e)
                if st is None or st.state in ("unavailable", "unknown"):
                    erreurs.append(f"{e} indisponible")
                    continue
                c = cible
                lo, hi = st.attributes.get("min_temp"), st.attributes.get("max_temp")
                if isinstance(lo, (int, float)):
                    c = max(c, float(lo))
                if isinstance(hi, (int, float)):
                    c = min(c, float(hi))
                try:
                    async with asyncio.timeout(20):
                        await self.hass.services.async_call("climate", "set_temperature",
                                                            {"entity_id": e, "temperature": c}, blocking=True)
                    self._tetes_envoye[e] = cible
                    self._noter_commande(e, "set_temperature", "tetes", c, f"consigne de la zone {ZONES[z]}")
                except Exception as err:  # noqa: BLE001 - une tête en panne ne doit jamais bloquer le pilotage
                    erreurs.append(f"{e} : {err}")
                    self._noter_commande(e, "set_temperature", "tetes", c, f"consigne de la zone {ZONES[z]}", str(err))
            avant = self.tetes_info.get(z, {})
            if erreurs:
                if avant.get("erreur") != erreurs[0]:
                    _LOGGER.warning("Pilotage poêle : têtes de la zone %s non réglées : %s", z, "; ".join(erreurs))
                self.tetes_info[z] = {"c": cible, "ts": avant.get("ts"), "ok": False, "erreur": erreurs[0]}
            elif avant.get("c") != cible or not avant.get("ok") or avant.get("ts") is None:
                self.tetes_info[z] = {"c": cible, "ts": int(ts), "ok": True, "erreur": None}

    def _allumages_aujourdhui(self, now: datetime) -> int:
        debut = datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo).timestamp()
        return sum(1 for t in self.allumages if t >= debut)

    def _heures_echeance(self) -> list[time]:
        o = self.opt
        h = []
        if o["matin_actif"]:
            h.append(_heure(o["matin_heure"]))
        if o["soir_actif"]:
            h.append(_heure(o["soir_heure"]))
        return h

    def _calculer_plan(self, now: datetime, mesures: list[PieceMesure], avec_immediat: bool = True) -> Plan | None:
        plan = self._calculer_plan_interne(now, mesures, avec_immediat)
        if plan is None or not plan.limitante:
            return plan
        m = next((x for x in mesures if x.nom == plan.limitante and x.zone), None)
        if m is None:
            return plan
        lib = f"la zone « {m.zone} » (moyenne de {m.nb_zone} pièces)"
        raison = (plan.raison or "").replace(f"prévu. « {m.nom} »", f"prévu. La zone « {m.zone} » (moyenne de {m.nb_zone} pièces)") \
            .replace(f"« {m.nom} »", lib)
        pieces = [dict(d, libelle=f"Zone {m.zone}") if d.get("nom") == m.nom else d for d in (plan.pieces or [])]
        return replace(plan, raison=raison, pieces=pieces)

    def _calculer_plan_interne(self, now: datetime, mesures: list[PieceMesure], avec_immediat: bool = True) -> Plan | None:
        marge = float(self.opt["marge"])
        marche = self.etat in ("allumage", "marche")
        # 1. besoin immédiat : une pièce est nettement sous la consigne en cours
        if avec_immediat and self.opt["immediat_actif"] and not marche and now.timestamp() >= self.ignore_immediat:
            imm = self._plan_immediat(now, mesures, marge)
            if imm is not None:
                return imm
        # 2. prochaines échéances : changements de consigne des zones, puis échéances fixes pour les autres pièces
        instants: dict[datetime, dict[str, Any]] = {}
        for z in ZONES:
            for dt, _c in changements(self.planning.get(z), now, 36.0)[:6]:
                instants.setdefault(dt, {"zones": set(), "classique": False})["zones"].add(z)
        if any(self._piece_classique(c, now) for _, c in self.pieces_cfg()):
            ech = echeances(now, self._heures_echeance())
            if ech:
                instants.setdefault(ech[0], {"zones": set(), "classique": False})["classique"] = True
        candidats: list[Plan] = []
        for dt in sorted(instants)[:8]:
            info = instants[dt]
            ms = self._mesures(dt, lambda c, info=info: (c.get(CONF_ZONE) in info["zones"] and self._zone_active(c.get(CONF_ZONE), dt))
                               or (info["classique"] and self._piece_classique(c, now)))
            candidats.append(planifier(now, dt, ms, self.appris, marge, deja_en_marche=marche, exterieur=self.exterieur))
        # 3. sans changement de consigne : quand une pièce va-t-elle passer sous son seuil en refroidissant ?
        seuil = None
        if self.opt["immediat_actif"] and not marche and now.timestamp() >= self.ignore_immediat:
            seuil = self._plan_seuil(now, mesures, marge)
        a_chauffer = [p for p in candidats + ([seuil] if seuil else []) if p.besoin and p.allumage is not None]
        if a_chauffer:
            return min(a_chauffer, key=lambda p: p.allumage)
        premier = candidats[0] if candidats else None
        if premier and premier.pieces:
            return premier
        return seuil or premier

    def _plan_seuil(self, now: datetime, mesures: list[PieceMesure], marge: float) -> Plan | None:
        """Estime l'heure où une pièce passera sous (consigne − écart de déclenchement) selon sa vitesse de baisse,
        puis recule de la durée d'allumage + latence pour conseiller l'heure d'allumage."""
        ext = self.exterieur
        delai, delai_appris = self.appris.delai_allumage()
        avance = (delai + LATENCE_MIN) * marge
        det: list[dict[str, Any]] = []
        meilleur = None
        for nom, c in self.pieces_cfg():
            mes = next((x for x in mesures if x.nom == nom), None)
            if mes is None or mes.temperature is None or mes.cible is None:
                continue
            s = self._seuil_piece(c)
            limite = mes.cible - s
            rate, rate_appris = self.appris.refroid(nom, None if ext is None else mes.temperature - ext)
            dm = self.appris.montee_detail(nom, ext, (mes.temperature + mes.cible) / 2.0)
            det.append({"nom": nom, "temperature": round(mes.temperature, 2), "cible": round(mes.cible, 2),
                        "prevue": round(mes.temperature, 2), "besoin": 0.0, "montee": round(dm["v"], 2),
                        "montee_apprise": dm["appris"], "montee_source": dm["source"], "tranche": dm["tranche"],
                        "duree_min": 0, "refroid_appris": rate_appris, "baisse": round(rate, 2), "limite": round(limite, 2)})
            if mes.temperature <= limite or rate < 0.02:
                continue                       # déjà sous le seuil (c'est le besoin immédiat) ou ne refroidit pas
            heures = (mes.temperature - limite) / rate
            if heures <= 36 and (meilleur is None or heures < meilleur[0]):
                meilleur = (heures, mes, s, rate, rate_appris, limite)
        if not det:
            return Plan(False, raison="Aucune pièce n'a de température et de consigne en cours : je ne peux rien anticiper. "
                                      "Choisissez une zone et un planning, ou réglez la consigne avec + / −.")
        if meilleur is None:
            return Plan(False, pieces=det, raison="Aucun changement de consigne prévu et aucune pièce ne passera sous son seuil "
                                                  "dans les 36 prochaines heures.", exterieur=ext)
        heures, mes, s, rate, rate_appris, limite = meilleur
        det.sort(key=lambda d: d["nom"] != mes.nom)
        t_cross = now + timedelta(hours=heures)
        allumage = t_cross - timedelta(minutes=avance)
        retard = allumage < now
        allumage = max(allumage, now)
        raison = (f"Aucun changement de consigne prévu. « {mes.nom} » est à {_v(mes.temperature)} °C pour une consigne de "
                  f"{_v(mes.cible)} °C et perd environ {_v(rate, 2)} °C/h"
                  f"{'' if ext is None else f' avec {_v(ext)} °C dehors'} ({'appris' if rate_appris else 'estimé'}) : "
                  f"elle passera sous {_v(limite)} °C (consigne − {_v(s)} °C) vers {t_cross.strftime('%H:%M')}. "
                  f"Allumage conseillé à {allumage.strftime('%H:%M')} : {round(delai)} min d'allumage"
                  f"{'' if delai_appris else ' (par défaut)'} + {round(LATENCE_MIN)} min de latence, avec "
                  f"{round((marge - 1) * 100)} % de marge.")
        return Plan(True, t_cross, allumage, round(avance), mes.nom, det, raison, retard, False, ext, True)

    def _seuil_piece(self, c: dict[str, Any]) -> float:
        """Écart sous la consigne qui déclenche l'allumage : celui de la pièce, sinon la valeur globale."""
        try:
            v = float(c.get(CONF_ECART))
        except (TypeError, ValueError):
            return float(self.opt["seuil_immediat"])
        return v if v > 0 else float(self.opt["seuil_immediat"])

    def _plan_immediat(self, now: datetime, mesures: list[PieceMesure], marge: float) -> Plan | None:
        seuils = {nom: self._seuil_piece(c) for nom, c in self.pieces_cfg()}
        manque = [m for m in mesures if m.temperature is not None and m.cible is not None
                  and m.cible - m.temperature > seuils.get(m.nom, float(self.opt["seuil_immediat"]))]
        if not manque:
            return None
        pl = planifier(now, now, mesures, self.appris, marge, deja_en_marche=False, exterieur=self.exterieur)
        if not pl.besoin or not pl.pieces:
            return None
        d0 = pl.pieces[0]
        seuil = seuils.get(d0["nom"], float(self.opt["seuil_immediat"]))
        delai, delai_appris = self.appris.delai_allumage()
        raison = (f"Consigne en cours : {_v(d0['cible'])} °C pour « {d0['nom']} », qui est à {_v(d0['temperature'])} °C "
                  f"(il manque {_v(d0['besoin'])} °C, écart supérieur à {_v(seuil)} °C). Allumage immédiat conseillé : "
                  f"environ {round(pl.duree_min)} min avant d'être à la consigne "
                  f"(montée {_v(d0['montee'])} °C/h{_sans_paren(note_montee(d0))}, "
                  f"allumage {round(delai)} min{'' if delai_appris else ' par défaut'})."
                  + ("" if pl.exterieur is None else f" Extérieur : {_v(pl.exterieur)} °C pris en compte."))
        return replace(pl, echeance=now + timedelta(minutes=max(30.0, pl.duree_min)), raison=raison, immediat=True,
                       en_retard=True)

    # ---------- transitions et apprentissage ----------
    async def _transition(self, ancien: str, nouveau: str, ts: float, mesures: list[PieceMesure]) -> None:
        _LOGGER.debug("Poêle : %s -> %s", ancien, nouveau)
        if nouveau in ("allumage", "marche") and self.run is None:
            source = "auto" if self.cmd and self.cmd.get("source") == "auto" and ts - self.cmd["ts"] < 1800 else (
                "force" if self.cmd and ts - self.cmd["ts"] < 1800 else "manuel")
            self.run = {"debut": ts, "source": source, "marche": None, "pieces0": {m.nom: m.temperature for m in mesures}}
        if nouveau == "marche" and self.run is not None and self.run["marche"] is None:
            self.run["marche"] = ts
            if self.cmd:
                self.appris.ajouter_delai((ts - self.cmd["ts"]) / 60.0)
                self.cmd = None
        if self.run is not None and nouveau in ("allumage", "marche"):
            self.run.pop("sorti", None)        # retour en marche : simple nettoyage de brasero, le cycle continue
        if self.run is not None and nouveau in ("arret", "extinction") and ancien in ("allumage", "marche", "inconnu"):
            self.run.setdefault("sorti", ts)   # on ne clôt le cycle que si la sortie dure (voir _tick_interne)
        if nouveau == "arret" and ancien in ("allumage", "marche", "extinction"):
            self.dernier_arret = ts   # arrêt complet constaté
        if nouveau == "arret":
            self._froid_appris_pour = None
        self._sauver()

    def _terminer_run(self, ts: float, mesures: list[PieceMesure]) -> None:
        run = self.run
        self.run = None
        self.dernier_arret = ts
        resume = {"debut": int(run["debut"]), "fin": int(ts), "source": run["source"],
                  "duree_min": round((ts - run["debut"]) / 60), "pieces": {}}
        if run["marche"] is not None:
            resume["allumage_min"] = round((run["marche"] - run["debut"]) / 60)
            for m in mesures:
                p = pente(self.echantillons.get(m.nom, []), run["marche"] + 1800, ts)
                if p is not None and p > 0:
                    # on n'apprend que si la pièce n'était pas déjà saturée (au-dessus de sa cible)
                    cible = m.cible
                    t0 = run["pieces0"].get(m.nom)
                    if cible is None or t0 is None or t0 < cible - 0.2:
                        ext_m, int_m = self._moy_fenetre(m.nom, run["marche"] + 1800, ts)
                        self.appris.maj_montee(m.nom, p, ext_m, int_m if ext_m is not None else None)
                        resume["pieces"][m.nom] = round(p, 2)
        self.journal = (self.journal + [resume])[-JOURNAL_MAX:]

    def _moy_fenetre(self, nom: str, t0: float, t1: float) -> tuple[float | None, float | None]:
        """(extérieur moyen, température moyenne de la pièce) sur une fenêtre ; extérieur = valeur actuelle à défaut d'historique."""
        noms = [n for n, _ in self.pieces_cfg()]
        exts = [h[3] for h in self.historique if len(h) >= 4 and h[3] is not None and t0 <= h[0] <= t1]
        ext = sum(exts) / len(exts) if exts else self.exterieur
        ints = [v for t, v in self.echantillons.get(nom, []) if t0 <= t <= t1]
        return ext, (sum(ints) / len(ints) if ints else None)

    def _apprendre_refroidissement(self, ts: float, mesures: list[PieceMesure]) -> None:
        if self.etat != "arret" or self.dernier_arret is None or self._froid_appris_pour == self.dernier_arret:
            return
        if ts - self.dernier_arret < 3 * 3600:
            return
        self._froid_appris_pour = self.dernier_arret
        for m in mesures:
            p = pente(self.echantillons.get(m.nom, []), self.dernier_arret + 5400, ts)
            if p is not None and p < 0:
                ext_m, int_m = self._moy_fenetre(m.nom, self.dernier_arret + 5400, ts)
                self.appris.maj_refroid(m.nom, -p, None if ext_m is None or int_m is None else int_m - ext_m)
        self._sauver()

    # ---------- sécurités et décisions ----------
    async def _securites(self, now: datetime, ts: float, mesures: list[PieceMesure]) -> None:
        o = self.opt
        # échec d'allumage
        if self.cmd and ts - self.cmd["ts"] > 40 * 60 and self.etat != "marche":
            self.drapeaux["verrou"], self.drapeaux["auto"] = True, False
            self.cmd = None
            await self._notifier("Poêle : l'allumage commandé n'a pas abouti en 40 minutes. L'allumage automatique est "
                                 "verrouillé : vérifiez le poêle, puis réactivez-le.")
            self._sauver()
        # durée maximale d'un allumage automatique
        if self.run and self.run["source"] == "auto" and self.etat in ("allumage", "marche") \
                and ts - self.run["debut"] > float(o["marche_max"]) * 60:
            if self._off_recent(ts):
                return
            await self._commande("turn_off", "securite")
            await self._notifier(f"Poêle : arrêt de sécurité après {round(float(o['marche_max']))} minutes de marche.")
        # arrêt automatique (si activé) : toutes les pièces au-dessus de cible + hystérésis depuis 15 min
        c = self.conditions_arret(ts, mesures)
        if self.drapeaux["arret_auto"] and self.run and self.run.get("marche"):
            if self.etat == "marche":      # une courte interruption (nettoyage) ne remet pas les compteurs à zéro
                self._stable_depuis = (self._stable_depuis or ts) if c["pieces_ok"] else None
            c = self.conditions_arret(ts, mesures)
            if c["declenche"]:
                await self._commande("turn_off", "arret_auto")
                await self._notifier("Poêle éteint automatiquement : toutes les pièces ont atteint leur température.")
        else:
            self._stable_depuis = None

    def conditions_arret(self, ts: float, mesures: list[PieceMesure]) -> dict[str, Any]:
        """Toutes les conditions de l'arrêt automatique, avec leur état — sert à la décision ET à l'affichage."""
        o = self.opt
        marche_min = float(o["marche_min"])
        hyst = float(o["hysteresis"])
        cycle = bool(self.run and self.run.get("marche"))
        en_marche = self.etat == "marche" and cycle
        t_marche = (ts - self.run["marche"]) if cycle else None
        # si la consigne baisse dans les 45 prochaines minutes, on vise déjà la plus basse : l'inertie finira le travail
        plus_tard = {m.nom: m.cible for m in self._mesures(dt_util.as_local(dt_util.utc_from_timestamp(ts + 45 * 60)))}
        mesurables = [m for m in mesures if m.temperature is not None and m.cible is not None]
        pieces = []
        for m in mesurables:
            cible = min(m.cible, plus_tard.get(m.nom) if plus_tard.get(m.nom) is not None else m.cible)
            pieces.append({"nom": m.nom, "temperature": round(m.temperature, 2), "requis": round(cible + hyst, 2),
                           "ok": m.temperature >= cible + hyst, "anticipe": cible < m.cible})
        pieces_ok = bool(pieces) and all(p["ok"] for p in pieces)
        zone_de = {m.nom: (m.zone, m.nb_zone) for m in mesurables if m.zone}
        vues: set[str] = set()
        groupees = []
        for p in pieces:                       # une seule ligne par zone décidée sur sa moyenne
            z = zone_de.get(p["nom"])
            if z is None:
                groupees.append(p)
            elif z[0] not in vues:
                vues.add(z[0])
                groupees.append({**p, "nom": f"Zone {z[0]} (moyenne de {z[1]} pièces)"})
        pieces = groupees
        stable = (ts - self._stable_depuis) if (self._stable_depuis and pieces_ok) else 0.0
        off_recent = self._off_recent(ts)
        duree_ok = t_marche is not None and t_marche >= marche_min * 60
        stable_ok = pieces_ok and stable >= 15 * 60
        declenche = bool(self.drapeaux["arret_auto"] and en_marche and duree_ok and pieces_ok and stable_ok and not off_recent)
        prevu = None
        if self.drapeaux["arret_auto"] and cycle and pieces_ok:
            prevu = max(self.run["marche"] + marche_min * 60, (self._stable_depuis or ts) + 15 * 60)
        return {
            "declenche": declenche, "pieces_ok": pieces_ok, "arret_prevu": None if prevu is None else int(prevu),
            "conditions": [
                {"id": "actif", "ok": bool(self.drapeaux["arret_auto"]), "libelle": "Arrêt automatique activé"},
                {"id": "marche", "ok": en_marche, "libelle": "Poêle en marche (allumage terminé)"},
                {"id": "duree", "ok": duree_ok, "libelle": f"Au moins {round(marche_min)} min de marche",
                 "detail": None if t_marche is None else f"{round(t_marche / 60)} min"},
                {"id": "pieces", "ok": pieces_ok, "libelle": f"Toutes les pièces à la cible + {hyst:g} °C",
                 "pieces": pieces},
                {"id": "stable", "ok": stable_ok, "libelle": "Maintenu pendant 15 min",
                 "detail": f"{round(stable / 60)} min" if pieces_ok else "pas commencé"},
            ],
            "pas_recent": not off_recent,
            "securite_min": round(float(o["marche_max"])) if self.run and self.run.get("source") == "auto" else None,
        }

    def _off_recent(self, ts: float) -> bool:
        return self._cmd_off_ts is not None and ts - self._cmd_off_ts < 900

    async def _decider(self, now: datetime, ts: float) -> None:
        plan = self.plan
        ok, _ = self.autorisation
        if not ok or plan is None or not plan.besoin or plan.allumage is None:
            return
        if self.cmd:
            return  # une commande est en attente de confirmation : on ne la renvoie jamais
        if plan.allumage > now:
            return
        if not plan.seuil and (plan.echeance - now) < timedelta(minutes=20):
            return  # trop tard pour que ce soit utile : on ne rallume pas juste avant l'échéance
        await self._commande("turn_on", "auto", plan)
        self.allumages.append(ts)
        if plan.immediat:
            msg = (f"Poêle allumé automatiquement à {now.strftime('%H:%M')} : « {plan.limitante} » est sous la consigne en cours. "
                   "Pour annuler : bouton « Annuler » du pilotage poêle.")
        elif plan.seuil:
            msg = (f"Poêle allumé automatiquement à {now.strftime('%H:%M')} : « {plan.limitante} » se refroidit et passera "
                   f"sous son seuil vers {plan.echeance.strftime('%H:%M')}. Pour annuler : bouton « Annuler » du pilotage poêle.")
        else:
            msg = (f"Poêle allumé automatiquement à {now.strftime('%H:%M')} pour que « {plan.limitante} » soit à température à "
                   f"{plan.echeance.strftime('%H:%M')}. Pour annuler : bouton « Annuler » du pilotage poêle.")
        await self._notifier(msg)

    def _noter_commande(self, entite: str, service: str, source: str, valeur: float | None = None,
                        raison: str | None = None, erreur: str | None = None) -> None:
        """Garde la trace de chaque ordre envoyé (ou reçu d'ailleurs), pour savoir qui a fait quoi."""
        self.commandes.append({"ts": int(dt_util.utcnow().timestamp()), "entite": entite, "service": service,
                               "source": source, "valeur": valeur, "raison": raison, "erreur": erreur})
        del self.commandes[:-COMMANDES_MAX]

    async def _commande(self, service: str, source: str, plan: Plan | None = None) -> None:
        ts = dt_util.utcnow().timestamp()
        self.derniere_action = {"ts": ts, "service": service, "source": source,
                                "raison": plan.raison if plan else None}
        if service == "turn_on":
            self.cmd = {"ts": ts, "source": source}
        else:
            self._cmd_off_ts = ts
        try:
            await self.hass.services.async_call("climate", service, {"entity_id": self.cfg[CONF_POELE]}, blocking=True)
        except Exception as err:  # noqa: BLE001 — on ne casse jamais la boucle
            _LOGGER.error("Commande %s refusée : %s", service, err)
            self.derniere_action["erreur"] = str(err)
            self._noter_commande(self.cfg[CONF_POELE], service, source, raison=plan.raison if plan else None, erreur=str(err))
            if service == "turn_on":
                self.cmd = None
        if not self.derniere_action.get("erreur"):
            self._noter_commande(self.cfg[CONF_POELE], service, source, raison=plan.raison if plan else None)
        self._sauver()

    async def _notifier(self, message: str) -> None:
        try:
            if self.cfg.get(CONF_NOTIF):
                await self.hass.services.async_call("notify", "send_message",
                                                    {"entity_id": self.cfg[CONF_NOTIF], "message": message}, blocking=True)
            else:
                await self.hass.services.async_call("persistent_notification", "create",
                                                    {"title": "Pilotage poêle", "message": message}, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Notification impossible : %s", err)

    # ---------- actions utilisateur ----------
    async def allumer_maintenant(self) -> None:
        await self._commande("turn_on", "force")
        await self._tick(None)

    async def annuler(self) -> None:
        """Annule l'allumage prévu pour la prochaine échéance ; éteint si le poêle a été allumé par l'auto/force."""
        if self.plan and (self.plan.immediat or self.plan.seuil):
            self.ignore_immediat = dt_util.utcnow().timestamp() + 2 * 3600     # j'ignore le besoin immédiat pendant 2 h
            suite = self._calculer_plan(dt_util.now(), self._mesures(), avec_immediat=False)
            if suite and suite.echeance:
                self.ignore_echeance = suite.echeance.timestamp()     # et la prochaine échéance aussi
        elif self.plan and self.plan.echeance:
            self.ignore_echeance = self.plan.echeance.timestamp()
        if self.etat in ("allumage", "marche") and self.run and self.run["source"] in ("auto", "force"):
            await self._commande("turn_off", "annulation")
        self._sauver()
        await self._tick(None)

    async def definir_drapeau(self, nom: str, valeur: bool) -> None:
        self.drapeaux[nom] = valeur
        if nom == "tetes":
            self._tetes_envoye.clear()            # réactivé : je renvoie les consignes à toutes les têtes
        if nom == "auto" and valeur:
            self.drapeaux["verrou"] = False
        self._sauver()
        await self._tick(None)

    def _zones_snapshot(self, now: datetime) -> dict[str, Any]:
        ts = now.timestamp()
        out = {}
        for z, nom in ZONES.items():
            cs = self.consigne_zone(z, now)
            pc = prochain_changement(self.planning.get(z), now)
            der = self.derogations.get(z)
            out[z] = {
                "nom": nom, "consigne": None if cs is None else cs[0], "source": "aucune" if cs is None else cs[1],
                "prochain": None if pc is None else {"ts": int(pc[0].timestamp()), "c": pc[1]},
                "planning": self.planning.get(z, {}),
                "derogation": None if not der or not self._derogation_active(der, ts) else {"c": der["c"], "jusqu": None if der.get("jusqu") is None else int(der["jusqu"])},
                "pieces": [n for n, c in self.pieces_cfg() if c.get(CONF_ZONE) == z],
                "pieces_temp": [{"nom": n, "temperature": _float(self.hass, c.get(CONF_TEMPERATURE))}
                                for n, c in self.pieces_cfg() if c.get(CONF_ZONE) == z],
                "moyenne": self._moyenne_zone(z),
            }
        return out

    def _moyenne_zone(self, zone: str) -> float | None:
        t = [_float(self.hass, c.get(CONF_TEMPERATURE)) for _, c in self.pieces_cfg() if c.get(CONF_ZONE) == zone]
        t = [v for v in t if v is not None]
        return round(sum(t) / len(t), 2) if t else None

    def _pieces_etat(self, now: datetime) -> list[dict[str, Any]]:
        """Toutes les pièces, y compris celles que l'objectif en cours ne concerne pas."""
        out = []
        for nom, c in self.pieces_cfg():
            z = c.get(CONF_ZONE)
            cible = self._cible_piece(c, now)
            out.append({
                "nom": nom, "temperature": _float(self.hass, c.get(CONF_TEMPERATURE)),
                "consigne": None if cible is None else round(cible, 2),
                "zone": ZONES.get(z) if self._zone_active(z, now) else None,
                "ecart": self._seuil_piece(c), "ecart_propre": c.get(CONF_ECART) is not None,
            })
        return out

    def _eau_snapshot(self) -> dict[str, Any]:
        st = self.hass.states.get(self.cfg[CONF_POELE])
        cur = None if st is None else st.attributes.get("temperature")
        e = self.eau_envoi or {}
        return {"actif": bool(self.drapeaux.get("eau")), "conseil": self.eau_info,
                "actuelle": float(cur) if isinstance(cur, (int, float)) else None,
                "dernier": None if not e else {"c": e["c"], "ts": int(e["ts"]), "verifie": e.get("verifie"), "erreur": e.get("erreur")},
                "changements_24h": len([t for t in self.eau_changes if t > dt_util.utcnow().timestamp() - 86400])}

    def _synthese(self, now: datetime, mesures: list[PieceMesure]) -> dict[str, Any]:
        """Indicateurs de synthèse (extérieur, intérieur, poêle sur 24 h, degrés-jours, pertes, économie)."""
        ts = now.timestamp()
        ext = self.exterieur
        rows = [h for h in self.historique if h[0] >= ts - 86400]
        out: dict[str, Any] = {"ext": ext}
        exts = [h[3] for h in rows if len(h) >= 4 and h[3] is not None] + ([ext] if ext is not None else [])
        if exts:
            tmin, tmax = min(exts), max(exts)
            out.update({"ext_min": round(tmin, 1), "ext_max": round(tmax, 1),
                        "dju": round(max(0.0, 18.0 - (tmin + tmax) / 2.0), 1),     # DJU base 18 : 18 − (Tmin+Tmax)/2
                        "heures_donnees": round(((rows[-1][0] if rows else ts) - (rows[0][0] if rows else ts)) / 3600.0, 1)})
            anc = [h[3] for h in rows if len(h) >= 4 and h[3] is not None and ts - 4 * 3600 <= h[0] <= ts - 2 * 3600]
            if ext is not None and anc:
                d = ext - sum(anc) / len(anc)
                out["tendance"] = "hausse" if d >= 0.5 else ("baisse" if d <= -0.5 else "stable")
        temps = [m.temperature for m in mesures if m.temperature is not None]
        if temps:
            out["int_moy"] = round(sum(temps) / len(temps), 1)
        ecarts = [m.cible - m.temperature for m in mesures if m.temperature is not None and m.cible is not None]
        if ecarts:
            out["ecart_moy"] = round(sum(ecarts) / len(ecarts), 2)
            out["nb_sous"] = sum(1 for e in ecarts if e > 0.3)
        on = (ETATS.index("allumage"), ETATS.index("marche"))
        out["marche_min"] = round(sum(1 for h in rows if h[1] in on) * ECHANTILLON_S / 60)
        out["allumages"] = sum(1 for i, h in enumerate(rows) if h[1] in on and (i == 0 or rows[i - 1][1] not in on) and i > 0)
        if ext is not None:
            pertes = [(self.appris.refroid(m.nom, m.temperature - ext)[0], self.appris.k_perte(m.nom)[1])
                      for m in mesures if m.temperature is not None]
            if pertes:
                out["perte"] = round(sum(p for p, _ in pertes) / len(pertes), 2)
                out["perte_apprise"] = all(a for _, a in pertes)
            cons = [m.cible for m in mesures if m.cible is not None] or temps
            if cons:
                dt = sum(cons) / len(cons) - ext
                if dt >= 3:
                    out["eco_pct"] = round(100.0 / dt, 1)     # pertes ∝ écart : 1 °C en moins ≈ 1/écart d'énergie en moins
        return out

    # ---------- état pour l'interface ----------
    def snapshot(self) -> dict[str, Any]:
        now = dt_util.now()
        o = self.opt
        noms = [n for n, _ in self.pieces_cfg()]
        plan = self.plan
        ok, raison_gf = self.autorisation
        return {
            "etat": self.etat, "etat_libelle": ETATS_LIBELLES[self.etat], "statut_brut": self.statut_brut,
            "drapeaux": dict(self.drapeaux),
            "plan": None if plan is None else {
                "besoin": plan.besoin, "echeance": int(plan.echeance.timestamp()) if plan.echeance else None,
                "allumage": int(plan.allumage.timestamp()) if plan.allumage else None, "duree_min": plan.duree_min,
                "limitante": plan.limitante, "raison": plan.raison, "en_retard": plan.en_retard, "pieces": plan.pieces,
                "immediat": plan.immediat, "seuil": plan.seuil,
            },
            "autorisation": {"ok": ok, "raison": raison_gf},
            "arret": self.conditions_arret(now.timestamp(), self._mesures()),
            "zones": self._zones_snapshot(now),
            "pieces_etat": self._pieces_etat(now),
            "synthese": self._synthese(now, self._mesures(brut=True)),
            "decision_zone": bool(self.opt.get("decision_zone", True)),
            "eau_poele": self._eau_snapshot(),
            "commandes": list(reversed(self.commandes[-15:])),
            "tetes": {"actif": bool(self.drapeaux.get("tetes", True)), "decalage": float(self.opt["tetes_decalage"]),
                      "zones": {z: {"nb": len(self._tetes_zone(z)), **self.tetes_info.get(z, {})}
                                for z in ZONES if self._tetes_zone(z)}},
            "garde_fous": {
                "fenetre": f"{o['fenetre_debut'][:5]}–{o['fenetre_fin'][:5]}", "max_allumages": o["max_allumages"],
                "allumages_aujourdhui": self._allumages_aujourdhui(now), "marche_min": o["marche_min"],
                "marche_max": o["marche_max"], "arret_min": o["arret_min"],
            },
            "appris": {
                "delais": self.appris.delais[-5:], "delai_p80": self.appris.delai_allumage()[0],
                "delai_appris": self.appris.delai_allumage()[1],
                "pieces": {n: {"montee": self.appris.montee(n)[0], "appris": self.appris.montee(n)[1],
                               "n": self.appris.pieces.get(n, {}).get("n_montee", 0),
                               "tranches": {t: v["n"] for t, v in (self.appris.pieces.get(n, {}).get("montee_t") or {}).items()},
                               "k": round(self.appris.k_perte(n)[0], 4), "k_appris": self.appris.k_perte(n)[1],
                               "n_k": self.appris.pieces.get(n, {}).get("n_k", 0),
                               "refroid_ancien": round(self.appris.pieces.get(n, {}).get("refroid", 0), 2)
                               if self.appris.pieces.get(n, {}).get("n_refroid", 0) > 0 else None}
                          for n in noms},
                "confiance": self.appris.confiance(noms),
            },
            "eau": _float(self.hass, self.cfg.get(CONF_EAU)), "exterieur": _float(self.hass, self.cfg.get(CONF_EXTERIEUR)),
            "pieces_noms": noms, "historique": self.historique[-HISTO_MAX:], "journal": self.journal[-8:][::-1],
            "derniere_action": self.derniere_action, "run": None if not self.run else {
                "debut": int(self.run["debut"]), "source": self.run["source"]},
        }
