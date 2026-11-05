# Pilotage poêle (Home Assistant) — version 0.1.0

Calcule **quand allumer** le poêle à granulés pour que les pièces soient à la bonne température à l'heure voulue, apprend les inerties de la maison, et explique chaque décision. Par défaut : **mode conseil**, rien n'est envoyé au poêle.

## Installation
1. Marketplace → « Ajouter grâce à un lien » → `https://github.com/cchouse/poele-pilotage` → installer, redémarrer Home Assistant.
2. Ajouter l'intégration « Pilotage poêle » : choisir le poêle (entité climat de l'intégration Duepi) et, si vous les avez, l'état (capteur « Status »), la température d'eau, l'extérieur, et où recevoir les notifications.
3. Sur la fiche : **Ajouter une pièce** → nom, capteur de température, **température visée** (par exemple le capteur « Consigne recommandée » de Confort ressenti, ou un `input_number`).
4. Ajouter la carte « Pilotage poêle » au tableau de bord.
5. « Configurer » : heures de confort (07:00 et 18:00 par défaut), plage horaire autorisée, limites. Les valeurs par défaut sont prudentes.

## Comment ça décide
Heure d'allumage = échéance − (délai d'allumage + latence + besoin ÷ vitesse de montée de la pièce la plus lente) × marge. Délai d'allumage et vitesse de montée de chaque pièce sont **appris** à chaque cycle ; tant qu'ils ne le sont pas, des valeurs par défaut prudentes sont utilisées et signalées comme telles.

## Garde-fous (allumage automatique)
Désactivé par défaut (interrupteur « Allumage automatique »). Il n'allume que si : plage horaire autorisée ; poêle à l'arrêt et sans problème ; pas en mode absent ; limite journalière non atteinte ; attente minimale respectée ; échéance non annulée ; au moins 20 min avant l'échéance. Arrêt de sécurité après une durée maximale ; échec d'allumage (rien après 40 min) = **verrouillage** + notification ; une commande en attente n'est jamais renvoyée. Chaque allumage est notifié et annulable (bouton « Annuler »).
L'arrêt automatique est un interrupteur séparé, désactivé par défaut.

## Limites de cette version
- Testée en simulation (24 tests), **jamais sur votre poêle réel**. L'état du poêle est déduit de ses textes (« Heating », « Standby »…) : vérifiez sur la carte, section « Derniers cycles », l'état brut lu. Les textes non reconnus donnent « état inconnu » et bloquent l'allumage automatique.
- N'agit que par marche/arrêt du poêle (allumage avec sa puissance et sa consigne par défaut). La consigne d'eau (loi d'eau) n'est pas encore pilotée.
- Un poêle ne devrait jamais dépendre d'un seul logiciel : gardez ses sécurités propres et vérifiez avec votre assurance/installateur.
