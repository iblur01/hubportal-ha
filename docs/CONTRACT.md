# Contrat HubPortal / Home Assistant

Version du contrat : **1**. Transport v1 : action du service `notify.mobile_app_<portal>` avec `message` et `data.command`, relayée au Portal par `mobile_app`.

## Commandes

| Commande | `message` | `data.command` | Résultat attendu côté Portal |
|---|---|---|---|
| Allumer l’écran | `command_screen_on` | absent | Réveil de l’écran |
| Éteindre l’écran | `command_screen_off` | absent | Mise en veille |
| Luminosité | `command_screen_brightness_level` | entier 0–255 | Réglage converti par le Portal en 0–100 % |
| Verrouiller les actions | `command_action_lock` | `"on"` | Activation du verrou |
| Déverrouiller | `command_action_lock` | `"off"` | **Refusé par l’intégration** ; confirmation locale requise |
| Démarrer le minuteur | `command_timer_start` | secondes, ou `[H:]MM:SS` | Remplace le minuteur actif ; plage Portal 1–86 399 s |
| Annuler le minuteur | `command_timer_cancel` | absent | Arrête le décompte ou la sonnerie |
| Demander le jumelage du foyer | `command_hubportal_pairing_request` | objet `request_id`, `foyer`, `expires_at` (secondes Unix) | Affiche une boîte de consentement sur ce Portal |
| Résoudre le jumelage | `command_hubportal_pairing_resolved` | objet `request_id`, `device_id`, `decision` | Ferme les boîtes correspondantes après la décision HA |

Enveloppe émise :

```json
{
  "message": "command_timer_start",
  "data": { "contract_version": 1, "command": 300 }
}
```

`contract_version` versionne l’enveloppe du contrat. L’intégration n’effectue aucun retry automatique. L’appel de service a un délai configurable (10 s par défaut) ; elle remonte `HomeAssistantError` si le service est absent, expire ou échoue. Le retour réussi de `notify` confirme uniquement l’acceptation par HA, **pas l’exécution par le Portal**.

## Jumelage de groupe avec consentement

À la validation du config flow, HA génère un identifiant aléatoire à usage unique et diffuse la demande à tous les Portals détectés qui disposent d’un service `notify.mobile_app_*`. Chaque Portal affiche le foyer et le délai restant, avec **Accepter** et **Refuser**. La demande expire après 120 secondes.

```json
{
  "message": "command_hubportal_pairing_request",
  "data": {
    "contract_version": 1,
    "command": {
      "request_id": "identifiant-aleatoire",
      "foyer": "maison",
      "expires_at": 1793395200
    }
  }
}
```

Un refus ferme uniquement la boîte de ce Portal; HA continue d’attendre un accord d’un autre Portal. Le premier accord valide crée une entrée par Portal détecté du foyer; le Portal qui a accepté reçoit la priorité de base, puis les autres reçoivent une priorité croissante. HA diffuse ensuite la résolution à tous. Les Portals n’échangent aucun message entre eux : HA relaie la demande et la décision.

Le Portal publie le choix par son webhook `mobile_app` avec `type: fire_event`, `event_type: hubportal_pairing_decision` et `event_data` contenant `contract_version`, `request_id`, `device_id` et `decision` (`accepted` ou `refused`). Le webhook est authentifié par l’identifiant secret attribué par `mobile_app`; ce secret n’est jamais inclus dans le message push, la boîte de dialogue ou les journaux.

Un `notify` réussi ne garantit pas l’affichage de la boîte, et l’API `mobile_app` peut répondre HTTP 200 même si un identifiant webhook est inconnu. L’acceptation effective est établie uniquement par l’événement HA corrélé au `request_id` actif. Si aucun Portal n’accepte avant l’expiration, le config flow s’arrête sans créer d’entrée.

## États lus

Les identifiants d’entités sont fournis au config flow, puisque leurs entity_id sont définis par HA et le nom du téléphone. Les capteurs Qt actuels sont `screen_on`, `action_lock`, `brightness`, `portal_timer`, `portal_timer_end` et `portal_timer_remaining`. Les états de minuteur sont `idle`, `active`, `ringing`; fin ISO 8601; temps restant en secondes.

La disponibilité et l’arbitrage estiment une activité à partir de `last_updated` le plus récent parmi les capteurs mappés. Ce n’est **pas un battement Portal**. Le capteur `disponibilite` expose cette estimation et son horodatage pour éviter de la présenter comme une garantie de connexion.

## Erreurs et sécurité

- Un verrou d’actions `on` refuse les commandes émises par cette intégration, sauf `command_action_lock: on`.
- Déverrouiller depuis HA échoue explicitement : le protocole mobile_app ne transporte ni défi, ni PIN, ni confirmation Portal.
- Toute panne/absence du service remonte une erreur au service ou à l’entité appelante. Une commande sensible n’est jamais rejouée automatiquement.
- Les commandes ordinaires n’ont aucun identifiant de corrélation ni accusé d’exécution; HA ne peut pas distinguer leur réception de leur exécution. Le jumelage a son propre identifiant et retour de consentement.

## Extension transport HTTP

`PortalCoordinator.async_send_command` centralise la transmission et peut être remplacé par un client HTTP. Le transport direct reste désactivé tant que le Portal ne propose pas une API d’authentification, d’état, de réponse et de contrôle sécurisée. Le serveur de configuration local actuel (port 8099) n’est pas une API de contrôle.
