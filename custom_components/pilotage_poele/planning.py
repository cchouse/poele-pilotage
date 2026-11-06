"""Planning de consignes par zone (Chambre, Maison) : fonctions pures, sans Home Assistant."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

ZONES = {"chambre": "Chambre", "maison": "Maison"}
CONSIGNE_MIN, CONSIGNE_MAX = 10.0, 28.0
MAX_TRANCHES_JOUR = 8
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def _hm(s: Any) -> tuple[int, int] | None:
    try:
        h, m = str(s).split(":")[:2]
        h, m = int(h), int(m)
    except (ValueError, TypeError):
        return None
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def valider(planning: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalise un planning venant de l'interface : jours « 0 » (lundi) à « 6 », tranches triées, consignes bornées."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(planning, dict):
        return out
    for j in range(7):
        brut = planning.get(str(j)) or planning.get(j) or []
        vus: dict[str, float] = {}
        for t in brut if isinstance(brut, list) else []:
            if not isinstance(t, dict):
                continue
            hm = _hm(t.get("h"))
            try:
                c = float(t.get("c"))
            except (ValueError, TypeError):
                continue
            if hm is None or not CONSIGNE_MIN <= c <= CONSIGNE_MAX:
                continue
            vus[f"{hm[0]:02d}:{hm[1]:02d}"] = round(c * 2) / 2
        if vus:
            out[str(j)] = [{"h": h, "c": vus[h]} for h in sorted(vus)][:MAX_TRANCHES_JOUR]
    return out


def actif(planning: dict[str, Any] | None) -> bool:
    return bool(planning)


def _debuts(planning: dict[str, Any], jour: datetime) -> list[tuple[datetime, float]]:
    """Débuts de tranche d'un jour donné (datetime locaux)."""
    res = []
    for t in planning.get(str(jour.weekday()), []):
        h, m = _hm(t["h"]) or (0, 0)
        res.append((datetime.combine(jour.date(), time(h, m), tzinfo=jour.tzinfo), float(t["c"])))
    return res


def consigne_a(planning: dict[str, Any] | None, quand: datetime) -> tuple[float, datetime] | None:
    """(consigne, début de la tranche) en vigueur à `quand` : la dernière tranche commencée, jusqu'à 7 jours en arrière."""
    if not planning:
        return None
    for retour in range(8):
        jour = quand - timedelta(days=retour)
        passes = [(d, c) for d, c in _debuts(planning, jour) if d <= quand]
        if passes:
            return max(passes, key=lambda x: x[0])[1], max(passes, key=lambda x: x[0])[0]
    return None


def changements(planning: dict[str, Any] | None, depuis: datetime, heures: float = 36.0) -> list[tuple[datetime, float]]:
    """Débuts de tranche strictement après `depuis` et jusqu'à `heures` plus tard, triés."""
    if not planning:
        return []
    fin = depuis + timedelta(hours=heures)
    out = []
    for k in range(0, int(heures // 24) + 2):
        for d, c in _debuts(planning, depuis + timedelta(days=k)):
            if depuis < d <= fin:
                out.append((d, c))
    return sorted(out)


def prochain_changement(planning: dict[str, Any] | None, depuis: datetime) -> tuple[datetime, float] | None:
    ch = changements(planning, depuis, 24 * 7)
    return ch[0] if ch else None
