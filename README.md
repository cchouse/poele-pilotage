# Pilotage poêle (Home Assistant) — version 0.5.2

Calcule **quand allumer** le poêle à granulés pour que les pièces soient à la bonne température à l'heure voulue, apprend les inerties de la maison, et explique chaque décision. Par défaut : **mode conseil**, rien n'est envoyé au poêle.

## Installation
1. Marketplace → « Ajouter grâce à un lien » → `https://github.com/cchouse/poele-pilotage` → installer, redémarrer Home Assistant.
2. Ajouter l'intégration « Pilotage poêle » : choisir le poêle (entité climat de l'intégration Duepi) et, si vous les avez, l'état (capteur « Status »), la température d'eau, l'extérieur, et où recevoir les notifications.
3. Sur la fiche : **Ajouter une pièce** → nom, capteur de température et **zone de planning** (Chambre ou Maison). Sans zone, indiquez l'entité qui donne la température visée (comme avant).
4. Ajouter la carte « Pilotage poêle » au tableau de bord.
5. « Configurer » : heures de confort (07:00 et 18:00 par défaut), plage horaire autorisée, limites. Les valeurs par défaut sont prudentes.

## Planning des consignes par zone (0.5)
Deux zones, **Chambre** et **Maison**, chacune avec son planning par jour de la semaine : des tranches « à partir de HH:MM, consigne X °C ». Tout se règle depuis la carte (onglets de zone, jours, ajout, suppression, copie sur la semaine).
- **Chaque changement de consigne est une échéance** : l'heure d'allumage est calculée pour que la zone soit à la nouvelle consigne à l'heure de la tranche.
- **Besoin immédiat** : si une pièce est à plus de 0,5 °C (réglable) sous la consigne en cours, l'allumage est conseillé tout de suite (ou lancé en mode automatique), par exemple quand vous relevez la consigne en journée.
- **Dérogation** : les boutons + / − de la carte (ou l'entité `number` « Consigne Chambre / Maison ») changent la consigne jusqu'à la prochaine tranche, puis le planning reprend. Bouton « Revenir au planning » pour annuler.
- **Arrêt anticipé** : si la consigne va baisser dans les 45 prochaines minutes, l'arrêt automatique vise déjà la valeur plus basse.
- Services utilisables dans vos automatisations : `pilotage_poele.definir_planning`, `pilotage_poele.derogation`, `pilotage_poele.retour_planning`.
- Les pièces sans zone gardent l'ancien fonctionnement (entité de température visée, échéances du matin et du soir).

## Comment ça décide
Heure d'allumage = échéance − (délai d'allumage + latence + besoin ÷ vitesse de montée de la pièce la plus lente) × marge. Délai d'allumage et vitesse de montée de chaque pièce sont **appris** à chaque cycle ; tant qu'ils ne le sont pas, des valeurs par défaut prudentes sont utilisées et signalées comme telles.

## Garde-fous (allumage automatique)
Désactivé par défaut (interrupteur « Allumage automatique »). Il n'allume que si : plage horaire autorisée ; poêle à l'arrêt et sans problème ; pas en mode absent ; limite journalière non atteinte ; attente minimale respectée ; échéance non annulée ; au moins 20 min avant l'échéance. Arrêt de sécurité après une durée maximale ; échec d'allumage (rien après 40 min) = **verrouillage** + notification ; une commande en attente n'est jamais renvoyée. Chaque allumage est notifié et annulable (bouton « Annuler »).
L'arrêt automatique est un interrupteur séparé, désactivé par défaut.

## Limites de cette version
- Testée en simulation (24 tests), **jamais sur votre poêle réel**. L'état du poêle est déduit de ses textes (« Heating », « Standby »…) : vérifiez sur la carte, section « Derniers cycles », l'état brut lu. Les textes non reconnus donnent « état inconnu » et bloquent l'allumage automatique.
- N'agit que par marche/arrêt du poêle (allumage avec sa puissance et sa consigne par défaut). La consigne d'eau (loi d'eau) n'est pas encore pilotée.
- Un poêle ne devrait jamais dépendre d'un seul logiciel : gardez ses sécurités propres et vérifiez avec votre assurance/installateur.
