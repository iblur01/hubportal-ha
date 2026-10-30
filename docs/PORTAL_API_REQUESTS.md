# API à demander au dépôt Qt

Cette liste recense les API Qt encore nécessaires pour aller au-delà du jumelage initial via `mobile_app`.

1. **API de contrôle authentifiée** distincte de la page d’appairage HTTP 8099 : appels TLS quand disponibles, autorisations par commande, révocation et stockage sûr des identifiants. Ne pas réutiliser le cookie web CSRF ni le jeton QR d’usage unique.
2. **État des commandes** : requête avec `command_id`, réponse corrélée acceptée/refusée/terminée, détail d’erreur non sensible, délai serveur et statut disponible à la reconnexion.
3. **Confirmation des actions sensibles** : protocole de défi/confirmation compatible avec `/security/confirmSensitive` et le PIN local, sans exposer ni journaliser le PIN. Permettre au moins le déverrouillage contrôlé et les autres actions sensibles.
4. **Battement explicite** : `last_seen` monotone et `boot_id`/uptime, publié à intervalle défini (p. ex. 15 s), indépendant de `last_interaction` et des changements de capteur. Documenter la tolérance de délai et la sémantique d’un Portal suspendu.
5. **Schéma public des états** : identifiants stables par appareil pour écran, verrou, luminosité, minuteur (état/fin/restant), version de schéma, unités, nullabilité et disponibilité. Inclure l’identifiant de foyer et l’identité Portal.
6. **Contrôle groupé** : commande média/API média du Portal ou préciser que l’intégration HA commande les lecteurs HA séparément. Pour minuteur partagé, préciser si chaque Portal dispose d’un minuteur indépendant ou d’une synchronisation partagée.
7. **Événements d’état** (WebSocket ou long polling) ou intervalle de polling recommandé, avec authentification et versionnement.

## Contrats actuels confirmés

`PortalBridge` reçoit les commandes `command_screen_on`, `command_screen_off`, `command_screen_brightness_level`, `command_action_lock`, `command_timer_start` et `command_timer_cancel` depuis le canal `mobile_app`. Il publie `portal_timer`, `portal_timer_end` et `portal_timer_remaining`, ainsi que `screen_on`, `action_lock` et `brightness`. Le push n’a pas d’accusé d’exécution. Le timestamp `last_interaction` n’est pas un heartbeat. Le serveur local 8099 est une interface de configuration HTTP appairée, pas une API de contrôle et ne propose pas HTTPS.

## Jumelage à consentement désormais implémenté

La branche de travail `qt-hubportal` ajoute `command_hubportal_pairing_request` et `command_hubportal_pairing_resolved`, une boîte de dialogue Accepter/Refuser, et le retour `hubportal_pairing_decision` par le webhook `mobile_app` de type `fire_event`. HA relaie la demande aux Portals et n’enregistre le foyer qu’après une acceptation corrélée. Aucun nouveau port ni échange pair-à-pair n’est utilisé. Cette version doit être intégrée et déployée côté Qt avant l’essai sur de vrais Portals.
