# API à demander au dépôt Qt

Cette liste prépare une évolution de transport; aucun changement n’est effectué dans `qt-hubportal`.

1. **API de contrôle authentifiée** distincte de la page d’appairage HTTP 8099 : appels TLS quand disponibles, autorisations par commande, révocation et stockage sûr des identifiants. Ne pas réutiliser le cookie web CSRF ni le jeton QR d’usage unique.
2. **État des commandes** : requête avec `command_id`, réponse corrélée acceptée/refusée/terminée, détail d’erreur non sensible, délai serveur et statut disponible à la reconnexion.
3. **Confirmation des actions sensibles** : protocole de défi/confirmation compatible avec `/security/confirmSensitive` et le PIN local, sans exposer ni journaliser le PIN. Permettre au moins le déverrouillage contrôlé et les autres actions sensibles.
4. **Battement explicite** : `last_seen` monotone et `boot_id`/uptime, publié à intervalle défini (p. ex. 15 s), indépendant de `last_interaction` et des changements de capteur. Documenter la tolérance de délai et la sémantique d’un Portal suspendu.
5. **Schéma public des états** : identifiants stables par appareil pour écran, verrou, luminosité, minuteur (état/fin/restant), version de schéma, unités, nullabilité et disponibilité. Inclure l’identifiant de foyer et l’identité Portal.
6. **Contrôle groupé** : commande média/API média du Portal ou préciser que l’intégration HA commande les lecteurs HA séparément. Pour minuteur partagé, préciser si chaque Portal dispose d’un minuteur indépendant ou d’une synchronisation partagée.
7. **Événements d’état** (WebSocket ou long polling) ou intervalle de polling recommandé, avec authentification et versionnement.
8. **Jumelage initié par HA, avec consentement Portal** : un événement à usage unique (`pairing_id`, date d’expiration, foyer HA) transmis au canal existant; afficher une boîte de dialogue explicite sur le Portal (« Autoriser la configuration Home Assistant ? »), avec Accepter/Refuser, puis publier une réponse corrélée. L’acceptation doit être liée au canal authentifié `mobile_app`, limitée au foyer et expirer rapidement. Ne jamais afficher de jeton ou de secret dans la boîte de dialogue; ne pas ouvrir de port entrant.

## Contrats actuels confirmés

`PortalBridge` reçoit les commandes `command_screen_on`, `command_screen_off`, `command_screen_brightness_level`, `command_action_lock`, `command_timer_start` et `command_timer_cancel` depuis le canal `mobile_app`. Il publie `portal_timer`, `portal_timer_end` et `portal_timer_remaining`, ainsi que `screen_on`, `action_lock` et `brightness`. Le push n’a pas d’accusé d’exécution. Le timestamp `last_interaction` n’est pas un heartbeat. Le serveur local 8099 est une interface de configuration HTTP appairée, pas une API de contrôle et ne propose pas HTTPS.

## Expérience de jumelage souhaitée

Le config flow actuel réduit déjà la saisie en listant les Portals et en mappant leurs capteurs depuis le registre `mobile_app`. Il ne peut pas encore déclencher une boîte de dialogue de consentement sur l’écran Portal : le protocole Qt n’a ni commande de jumelage ni réponse utilisateur. Appeler `notify` ne ferait qu’afficher une notification générique, sans preuve de consentement; le point 8 doit être implémenté côté Qt avant de promettre un jumelage façon WPS.
