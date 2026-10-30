# HubPortal pour Home Assistant

Intégration personnalisée HACS pour exposer les appareils Portal Hub. Elle requiert Home Assistant 2024.8 ou plus récent, relaie les commandes via les services `notify.mobile_app_*` natifs du Portal, n’ouvre aucun port et ne stocke pas de jeton HA.

## Installation HACS

1. Dans HACS, choisissez **Dépôts personnalisés** et ajoutez `https://github.com/iblur01/hubportal-ha` avec la catégorie **Integration**.
2. Installez **HubPortal**, puis redémarrez Home Assistant.
3. Ouvrez **Paramètres → Appareils et services → Ajouter une intégration → HubPortal**.
4. Saisissez l’identifiant du foyer, la priorité initiale et le délai de bascule. Home Assistant envoie une demande de consentement à tous les Portals détectés. Acceptez-la sur l’un d’eux; HA crée alors une entrée pour chaque Portal du foyer et donne la priorité initiale à celui qui a accepté.

Le flow de configuration s’exécute dans HA : aucun identifiant ou mot de passe HA n’est demandé. Il détecte les Portals déjà inscrits dans `mobile_app`. Le jumelage groupé requiert un build Qt du Portal qui prend en charge `command_hubportal_pairing_request`; sur un ancien build, la fenêtre ne peut pas s’afficher.

## Entités

Chaque Portal fournit :

- `switch.<portal>_ecran`
- `number.<portal>_luminosite` (0–100 %)
- `switch.<portal>_verrou_actions`
- `number.<portal>_minuteur_duree` (1–86 399 secondes)
- `button.<portal>_minuteur_demarrer` et `button.<portal>_minuteur_annuler`
- `sensor.<portal>_minuteur`, `sensor.<portal>_minuteur_fin`, `sensor.<portal>_minuteur_restant`
- `sensor.<portal>_disponibilite`

Un sélecteur `select.hubportal_maitre` (suffixé par HA s’il y a plusieurs foyers) est créé par foyer. La priorité la plus basse passe en premier; en cas d’égalité, la date d’enregistrement HA est utilisée. Par défaut, le premier Portal disponible est choisi. Après le silence configuré (90 s par défaut), le suivant disponible devient maître.

La sélection manuelle reprend lorsque le Portal choisi redevient disponible. Si tous les Portals semblent indisponibles, le sélecteur garde un candidat, mais les services groupés refusent l’action avec une erreur explicite. Pendant une panne HA, aucune synchronisation ou commande n’est possible. Les minuteurs propres aux Portal ne sont pas répliqués entre appareils.

## Services

```yaml
service: hubportal.timer_start
data:
  foyer: maison
  seconds: 600
```

```yaml
service: hubportal.timer_cancel
data:
  foyer: maison
```

Ces services ciblent le maître courant. Aucun service média de groupe n’est activé : le protocole Qt ne définit pas de commande média vers un Portal. Le déverrouillage depuis HA est refusé; confirmez-le sur l’écran Portal.

## Limites connues

Le build mobile_app actuel ne fournit pas de heartbeat dédié ni d’accusé de réception des commandes ordinaires. `Disponibilité` est donc une estimation basée sur la fraîcheur des entités source, et un appel `notify` réussi prouve uniquement que HA a accepté l’appel. La demande de jumelage expire après 120 secondes; si tous les Portals refusent ou restent silencieux, aucune entrée n’est créée. Les commandes médias directes et les commandes HTTP nécessitent une API Portal à ajouter ultérieurement; voir [`docs/PORTAL_API_REQUESTS.md`](docs/PORTAL_API_REQUESTS.md) et [`docs/CONTRACT.md`](docs/CONTRACT.md).

## Maillage Portal (contrat v2)

Les Portals qui publient `_hubportal._tcp` avec la clé `proto` forment un maillage pair à pair. HA y est un membre authentifié **non votant** : il n'élit pas, ne compte pas dans le quorum et son arrêt n'a aucun effet sur le maillage.

1. HA découvre le Portal (ou saisissez son adresse dans le formulaire, port 8447) et propose « Rejoindre le maillage ».
2. Le config flow affiche un **code à 6 chiffres**. Comparez-le avec celui du Portal et confirmez dans HA qu'il est identique (sans cette confirmation, rien n'est créé), puis acceptez sur un Portal. Si les codes diffèrent, ne confirmez pas.
3. HA crée un device par Portal (entités v1 par Portal) et un device « Maillage HubPortal » avec le maître courant et l'epoch en diagnostic.

Les services `hubportal.timer_start` et `hubportal.timer_cancel` acceptent `foyer: maillage` (ou le `network_id`) et sont envoyés au maître; sans maître, ils échouent avec « Pas de maître ». Les commandes d'une entité vont au Portal concerné.

La clé réseau et l'identité de HA sont dans les données de l'entrée de configuration (jamais dans les états, journaux ou diagnostics) : quiconque lit le stockage de HA peut agir comme membre du maillage.

**Migration depuis v1.** Les entrées `mobile_app`/`notify` existantes continuent de fonctionner et un Portal sans `proto` reste sur ce transport. Après avoir rejoint le maillage, les entités ont de nouveaux identifiants : adaptez vos automatisations puis supprimez l'entrée v1 du Portal. Détails, format de fil et limites : [`docs/CONTRACT.md`](docs/CONTRACT.md). Le mDNS ne traverse pas un réseau Docker en bridge ni un VLAN.

## Développement

```sh
python -m pip install 'pytest-homeassistant-custom-component>=0.13.200' 'ruff>=0.14'
ruff check .
pytest
```

Les tests utilisent `pytest-homeassistant-custom-component`; un vrai Home Assistant et un Portal restent requis pour valider le canal mobile_app réel, les capteurs publiés, la réception des push et l’action de l’écran.
