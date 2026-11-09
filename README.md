# Pilotage poêle (Home Assistant) — version 1.0.0

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

## Température extérieure (0.6)
Si vous avez indiqué un capteur extérieur (« Reconfigurer »), il entre dans les calculs :
- **Refroidissement** de chaque pièce proportionnel à l'écart intérieur − extérieur (appris sur les arrêts de plus de 3 h, en sautant les 90 premières minutes où la pièce est encore chauffée par le poêle). Un refroidissement mesuré avant l'ajout du capteur extérieur est ignoré tant que le nouveau n'est pas appris ; « Ce que j'ai appris » indique ce qui est utilisé.
- **Montée** apprise **par tranche d'extérieur** (froid < 5 °C, frais 5–12 °C, doux ≥ 12 °C). Tant qu'une tranche n'a pas été vue, la valeur apprise est corrigée selon l'écart ; sans rien d'appris, la valeur par défaut est ajustée. La carte indique l'origine de chaque valeur.
- L'extérieur actuel est utilisé pour toute la prévision (pas de prévision météo). Sans capteur extérieur, le comportement est inchangé.

## Têtes thermostatiques (0.6)
Dans « Configurer », choisissez les têtes (entités `climate`) de la zone **Chambre** et de la zone **Maison**, et un décalage facultatif (par exemple +1 °C pour une tête mal placée). L'intégration envoie la consigne de la zone à ses têtes **à chaque changement** (tranche du planning, dérogation, retour au planning) et au démarrage, en respectant le minimum et le maximum de chaque tête. Interrupteur « Piloter les têtes » pour suspendre. Une tête indisponible est réessayée chaque minute. Ne la pilotez pas aussi depuis une autre intégration (par exemple Confort ressenti) : elles se disputeraient les mêmes têtes.

## Synthèse en haut de la carte (0.6.1)
Une zone repliable de six tuiles, calculées avec les données déjà enregistrées (24 h) :
- **Extérieur** : valeur actuelle, minimum, maximum et tendance.
- **Intérieur** : moyenne des pièces et écart moyen à la consigne.
- **Poêle sur 24 h** : durée de marche et nombre d'allumages observés.
- **Besoin de chauffage** : degrés-jours unifiés (DJU) base 18, soit 18 − (Tmin + Tmax) / 2, la méthode utilisée par les chauffagistes pour comparer la rigueur du climat.
- **Perte de la maison** : °C/h perdus sans chauffage à l'écart actuel avec l'extérieur (appris sur vos arrêts, estimé tant que rien n'est appris).
- **Économie possible** : les pertes étant proportionnelles à l'écart intérieur − extérieur, baisser la consigne d'1 °C économise environ 1 ÷ écart de l'énergie (≈ 6 % pour 17 °C d'écart). C'est un ordre de grandeur, pas une mesure.

## Loi d'eau (0.7)
Avec un capteur extérieur, l'intégration calcule la **consigne d'eau conseillée** : une courbe entre deux points que vous réglez dans « Configurer » (eau voulue à 0 °C dehors, eau voulue à 15 °C dehors et au-delà), plus 2 ou 4 °C quand le besoin est pressant (une pièce manque 1 °C ou plus alors qu'un allumage est imminent). Elle s'affiche sur la carte avec la raison, et dans la synthèse. Par défaut : rien n'est envoyé.

L'interrupteur **« Régler l'eau du poêle »** (désactivé par défaut) envoie la consigne à l'entité `climate` du poêle (`climate.set_temperature`), avec ces garde-fous :
- uniquement pendant la marche du poêle ;
- jamais en dessous de 58 °C par défaut (la pompe de circulation démarre à 55 °C pour éviter la condensation) ni au-dessus de 75 °C, ni hors des limites de l'entité du poêle ;
- 5 °C au plus par changement, 30 min minimum entre deux changements, 6 changements par jour au plus, rien pour un écart de moins de 2 °C ;
- relecture 90 s après chaque envoi : si le poêle n'a pas pris la consigne 3 fois de suite, l'interrupteur se désactive et une notification est envoyée.
L'ajustement « besoin pressant » est une règle fixe, pas encore un apprentissage.

## Sans planning : estimation du seuil (0.8)
Quand aucun changement de consigne n'est prévu, l'intégration estime quand chaque pièce passera sous **consigne − écart de déclenchement** (écart global de « Configurer », ou celui de la pièce), d'après sa vitesse de refroidissement (appris, ou estimé d'après l'extérieur). Elle recule ensuite de la durée d'allumage + 30 min de latence + 25 % de marge pour conseiller l'heure d'allumage, et l'affiche sur la carte (« Estimé : … passera sous son seuil vers … »). En mode automatique, elle allume à cette heure. Le bouton « Annuler » ignore cette estimation pendant 2 h.

## Traçabilité de la consigne (0.8.1)
- La carte affiche « Commandes envoyées » : les 15 dernières commandes (allumer, éteindre, consigne, têtes) avec heure, entité, origine et raison. Si une consigne change sans y figurer, elle ne vient pas de cette intégration.
- Si la consigne d'eau du poêle sort des limites (min/max configurés), elle est ramenée **directement** dans les limites (et non par paliers de 5 °C), au plus toutes les 5 min.
- Si la consigne passe hors limites sans que l'intégration l'ait envoyée, une notification est envoyée et l'événement est inscrit au journal (origine « externe »).

## Consigne sans planning (0.8.3)
Dans une zone **sans planning**, la consigne réglée (boutons +/− ou entité « Consigne … ») reste en place **sans limite de durée** jusqu'à ce que vous la changiez ou l'effaciez (bouton « Effacer la consigne »). Avec un planning, elle s'arrête toujours à la prochaine tranche. Les anciennes dérogations de 12 h sans planning deviennent sans limite au démarrage.

## Décision par zone (0.9)
Option « Décider par zone plutôt que par pièce » (⚙ Configurer, **activée par défaut**). Pour chaque zone (Chambre, Maison) ayant au moins 2 pièces, la température retenue est la **moyenne simple des sondes** de ses pièces, comparée à la consigne de la zone : une pièce qui perd quelques dixièmes ne déclenche plus rien tant que la moyenne tient. Les pièces sans zone restent traitées une par une. L'apprentissage, l'historique et les courbes continuent d'utiliser les vraies sondes. La carte affiche la moyenne de chaque zone avec un menu déroulant montrant chaque pièce. Désactivez l'option pour retrouver l'ancien comportement.

## Consigne d'eau sans lecture du poêle (0.9.2)
Le réglage de l'eau ne lit **plus jamais** la consigne du poêle pour décider (une coupure du cloud peut renvoyer une valeur par défaut, par exemple 25 °C). L'intégration calcule la consigne conseillée (limites min/max), l'envoie si elle diffère d'au moins 2 °C de **sa dernière valeur envoyée** (gardée en mémoire), au plus toutes les 30 min et 6 fois par jour, uniquement pendant la marche. Un envoi refusé est retenté 30 min plus tard ; après 3 refus de suite, le réglage se désactive. La détection de « consigne étrangère » et la vérification par relecture sont supprimées. Le journal des commandes reste.

## Arrêt si l'eau est trop chaude (1.0)
Option « Arrêt si l'eau dépasse la consigne d'eau de (°C) » (0 = désactivé, par défaut). Si l'eau mesurée (capteur d'eau configuré) reste au moins cet écart au-dessus de la consigne d'eau pendant 3 min, le poêle est éteint (notification + journal, origine « eau trop chaude »). Consigne de référence : ma dernière valeur envoyée si le réglage de l'eau est actif, sinon celle du poêle, ignorée si elle sort des limites min/max (valeur aberrante).

## Comment ça décide
Heure d'allumage = échéance − (délai d'allumage + latence + besoin ÷ vitesse de montée de la pièce la plus lente) × marge. Délai d'allumage et vitesse de montée de chaque pièce sont **appris** à chaque cycle ; tant qu'ils ne le sont pas, des valeurs par défaut prudentes sont utilisées et signalées comme telles.

## Garde-fous (allumage automatique)
Désactivé par défaut (interrupteur « Allumage automatique »). Il n'allume que si : plage horaire autorisée ; poêle à l'arrêt et sans problème ; pas en mode absent ; limite journalière non atteinte ; attente minimale respectée ; échéance non annulée ; au moins 20 min avant l'échéance. Arrêt de sécurité après une durée maximale ; échec d'allumage (rien après 40 min) = **verrouillage** + notification ; une commande en attente n'est jamais renvoyée. Chaque allumage est notifié et annulable (bouton « Annuler »).
L'arrêt automatique est un interrupteur séparé, désactivé par défaut.

## Limites de cette version
- Testée en simulation (74 tests), **jamais sur votre poêle réel**. L'état du poêle est déduit de ses textes (« Heating », « Standby »…) : vérifiez sur la carte, section « Derniers cycles », l'état brut lu. Les textes non reconnus donnent « état inconnu » et bloquent l'allumage automatique.
- N'agit que par marche/arrêt du poêle (allumage avec sa puissance et sa consigne par défaut). La consigne d'eau peut être réglée automatiquement (loi d'eau, 0.7), mais cette fonction est désactivée par défaut et n'a jamais été essayée sur un vrai poêle.
- Un poêle ne devrait jamais dépendre d'un seul logiciel : gardez ses sécurités propres et vérifiez avec votre assurance/installateur.
