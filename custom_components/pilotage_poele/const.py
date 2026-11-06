"""Constantes de l'intégration Pilotage poêle."""
DOMAIN = "pilotage_poele"
SUBENTRY_PIECE = "piece"
VERSION = "0.5.1"
URL_CARTE = "/pilotage_poele/pilotage-poele-card.js"

# configuration principale
CONF_POELE = "poele"            # entité climate du poêle (intégration Duepi)
CONF_STATUT = "statut"          # capteur d'état texte (facultatif)
CONF_EAU = "temperature_eau"    # capteur de température d'eau (facultatif)
CONF_EXTERIEUR = "exterieur"    # capteur de température extérieure (facultatif)
CONF_NOTIF = "notification"     # entité notify (facultatif)

# pièce (sous-entrée)
CONF_TEMPERATURE = "capteur_temperature"
CONF_CIBLE = "cible"            # entité qui donne la température visée (facultatif si la pièce est dans une zone)
CONF_ZONE = "zone"              # zone de planning : « chambre » ou « maison » (facultatif)

# options (valeurs par défaut sûres)
OPTIONS_DEFAUT = {
    "matin_actif": True,
    "matin_heure": "07:00:00",
    "soir_actif": True,
    "soir_heure": "18:00:00",
    "fenetre_debut": "05:00:00",
    "fenetre_fin": "22:30:00",
    "max_allumages": 3,
    "marche_min": 90,        # minutes : durée minimale de marche avant tout arrêt automatique
    "marche_max": 480,       # minutes : arrêt de sécurité pour un allumage automatique
    "arret_min": 45,         # minutes d'arrêt minimum entre deux allumages automatiques
    "marge": 1.25,           # marge de prudence sur la durée de chauffe
    "hysteresis": 0.3,       # °C au-dessus de la cible pour l'arrêt automatique
    "immediat_actif": True,  # allumage conseillé tout de suite si la pièce est sous la consigne en cours
    "seuil_immediat": 0.5,   # °C d'écart sous la consigne en cours avant de déclencher
}

TICK_S = 60
ECHANTILLON_S = 300
HISTO_MAX = 288              # 24 h à 5 min
JOURNAL_MAX = 30
