"""Exécution : lit les entités, apprend, planifie, et (si autorisé) commande le poêle."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CIBLE, CONF_EAU, CONF_EXTERIEUR, CONF_NOTIF, CONF_POELE, CONF_STATUT, CONF_TEMPERATURE,
    DOMAIN, ECHANTILLON_S, HISTO_MAX, JOURNAL_MAX, OPTIONS_DEFAUT, TICK_S,
)
from .model import (
    ETATS, ETATS_LIBELLES, Appris, Params, PieceMesure, Plan, amorcer_depuis_historique, classer, echeances,
    garde_fous, pente, planifier,
)

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


def _heure(s: str) -> time:
    h = str(s).split(":")
    return time(int(h[0]), int(h[1]) if len(h) > 1 else 0)


class Pilote:
    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.appris = Appris()
        self.drapeaux = {"auto": False, "arret_auto": False, "absent": False, "verrou": False}
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
        self.echantillons: dict[str, list[tuple[float, float]]] = {}
        self._froid_appris_pour: float | None = None
        self._cmd_off_ts: float | None = None
        self._stable_depuis: float | None = None
        self._dernier_echantillon = 0.0
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
        self.cmd = d.get("cmd")
        self.run = d.get("run")
        self._cmd_off_ts = d.get("cmd_off")
        self.amorce = bool(d.get("amorce", False))
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
            "cmd_off": self._cmd_off_ts, "amorce": self.amorce,
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

    def _mesures(self) -> list[PieceMesure]:
        return [PieceMesure(nom, _float(self.hass, c.get(CONF_TEMPERATURE)), _float(self.hass, c.get(CONF_CIBLE)))
                for nom, c in self.pieces_cfg()]

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
        mesures = self._mesures()
        # échantillons pour l'apprentissage et l'historique
        if ts - self._dernier_echantillon >= ECHANTILLON_S:
            self._dernier_echantillon = ts
            for m in mesures:
                if m.temperature is not None:
                    self.echantillons.setdefault(m.nom, []).append((ts, m.temperature))
                    self.echantillons[m.nom] = [e for e in self.echantillons[m.nom] if e[0] > ts - 43200]
            self.historique.append([int(ts), ETATS.index(self.etat), _float(self.hass, self.cfg.get(CONF_EAU)),
                                    _float(self.hass, self.cfg.get(CONF_EXTERIEUR)),
                                    [None if m.temperature is None else round(m.temperature, 2) for m in mesures]])
            self.historique = self.historique[-HISTO_MAX:]
            self._sauver()
        # état du poêle et transitions
        nouveau = self._lire_etat()
        if nouveau != self.etat:
            await self._transition(self.etat, nouveau, ts, mesures)
            self.etat = nouveau
        self._apprendre_refroidissement(ts, mesures)
        # sécurités d'abord (elles peuvent verrouiller), puis plan, garde-fous et décision
        await self._securites(now, ts, mesures)
        self.plan = self._calculer_plan(now, mesures)
        self.autorisation = garde_fous(now, self.etat, self._params(), self._allumages_aujourdhui(now),
                                       self.dernier_arret, self.plan, self.ignore_echeance)
        await self._decider(now, ts)
        async_dispatcher_send(self.hass, self.signal)

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

    def _calculer_plan(self, now: datetime, mesures: list[PieceMesure]) -> Plan | None:
        ech = echeances(now, self._heures_echeance())
        if not ech:
            return None
        return planifier(now, ech[0], mesures, self.appris, float(self.opt["marge"]),
                         deja_en_marche=self.etat in ("allumage", "marche"))

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
        if self.run is not None and nouveau in ("arret", "extinction") and ancien in ("allumage", "marche", "inconnu"):
            self._terminer_run(ts, mesures)
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
                        self.appris.maj_montee(m.nom, p)
                        resume["pieces"][m.nom] = round(p, 2)
        self.journal = (self.journal + [resume])[-JOURNAL_MAX:]

    def _apprendre_refroidissement(self, ts: float, mesures: list[PieceMesure]) -> None:
        if self.etat != "arret" or self.dernier_arret is None or self._froid_appris_pour == self.dernier_arret:
            return
        if ts - self.dernier_arret < 3 * 3600:
            return
        self._froid_appris_pour = self.dernier_arret
        for m in mesures:
            p = pente(self.echantillons.get(m.nom, []), self.dernier_arret + 3600, ts)
            if p is not None and p < 0:
                self.appris.maj_refroid(m.nom, -p)
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
        if self.drapeaux["arret_auto"] and self.etat == "marche" and self.run and self.run.get("marche"):
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
        en_marche = self.etat == "marche" and bool(self.run and self.run.get("marche"))
        t_marche = (ts - self.run["marche"]) if en_marche else None
        mesurables = [m for m in mesures if m.temperature is not None and m.cible is not None]
        pieces = [{"nom": m.nom, "temperature": round(m.temperature, 2), "requis": round(m.cible + hyst, 2),
                   "ok": m.temperature >= m.cible + hyst} for m in mesurables]
        pieces_ok = bool(pieces) and all(p["ok"] for p in pieces)
        stable = (ts - self._stable_depuis) if (self._stable_depuis and pieces_ok) else 0.0
        off_recent = self._off_recent(ts)
        duree_ok = t_marche is not None and t_marche >= marche_min * 60
        stable_ok = pieces_ok and stable >= 15 * 60
        declenche = bool(self.drapeaux["arret_auto"] and en_marche and duree_ok and pieces_ok and stable_ok and not off_recent)
        prevu = None
        if self.drapeaux["arret_auto"] and en_marche and pieces_ok:
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
        if (plan.echeance - now) < timedelta(minutes=20):
            return  # trop tard pour que ce soit utile : on ne rallume pas juste avant l'échéance
        await self._commande("turn_on", "auto", plan)
        self.allumages.append(ts)
        await self._notifier(
            f"Poêle allumé automatiquement à {now.strftime('%H:%M')} pour que « {plan.limitante} » soit à température à "
            f"{plan.echeance.strftime('%H:%M')}. Pour annuler : bouton « Annuler » du pilotage poêle.")

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
            if service == "turn_on":
                self.cmd = None
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
        if self.plan and self.plan.echeance:
            self.ignore_echeance = self.plan.echeance.timestamp()
        if self.etat in ("allumage", "marche") and self.run and self.run["source"] in ("auto", "force"):
            await self._commande("turn_off", "annulation")
        self._sauver()
        await self._tick(None)

    async def definir_drapeau(self, nom: str, valeur: bool) -> None:
        self.drapeaux[nom] = valeur
        if nom == "auto" and valeur:
            self.drapeaux["verrou"] = False
        self._sauver()
        await self._tick(None)

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
            },
            "autorisation": {"ok": ok, "raison": raison_gf},
            "arret": self.conditions_arret(now.timestamp(), self._mesures()),
            "garde_fous": {
                "fenetre": f"{o['fenetre_debut'][:5]}–{o['fenetre_fin'][:5]}", "max_allumages": o["max_allumages"],
                "allumages_aujourdhui": self._allumages_aujourdhui(now), "marche_min": o["marche_min"],
                "marche_max": o["marche_max"], "arret_min": o["arret_min"],
            },
            "appris": {
                "delais": self.appris.delais[-5:], "delai_p80": self.appris.delai_allumage()[0],
                "delai_appris": self.appris.delai_allumage()[1],
                "pieces": {n: {"montee": self.appris.montee(n)[0], "appris": self.appris.montee(n)[1],
                               "n": self.appris.pieces.get(n, {}).get("n_montee", 0)} for n in noms},
                "confiance": self.appris.confiance(noms),
            },
            "eau": _float(self.hass, self.cfg.get(CONF_EAU)), "exterieur": _float(self.hass, self.cfg.get(CONF_EXTERIEUR)),
            "pieces_noms": noms, "historique": self.historique[-HISTO_MAX:], "journal": self.journal[-8:][::-1],
            "derniere_action": self.derniere_action, "run": None if not self.run else {
                "debut": int(self.run["debut"]), "source": self.run["source"]},
        }
