# HubPortal pour Home Assistant

Intégration personnalisée HACS pour exposer les appareils Portal Hub. Cette première version relaie les commandes via les services `notify.mobile_app_*` natifs du Portal. Elle n’ouvre aucun port et ne stocke pas de jeton HA.

## Installation HACS

1. Dans HACS, choisissez **Dépôts personnalisés** et ajoutez `https://github.com/iblur01/hubportal-ha` avec la catégorie **Integration**.
2. Installez **HubPortal**, puis redémarrez Home Assistant.
3. Ouvrez **Paramètres → Appareils et services → Ajouter une intégration → HubPortal**.
4. Choisissez le Portal détecté et donnez-lui un identifiant de foyer. HA remplit le service `mobile_app` et les entity_id des capteurs automatiquement. Pour les autres appareils du même foyer, ajoutez une entrée et réutilisez le même identifiant.

Le flow de configuration s’exécute dans HA : aucun identifiant ou mot de passe HA n’est demandé. Il détecte les Portals déjà inscrits dans `mobile_app`; la fenêtre d’acceptation sur le Portal façon WPS nécessite encore la commande et le retour d’API décrits dans [`docs/PORTAL_API_REQUESTS.md`](docs/PORTAL_API_REQUESTS.md). La sécurité du canal dépend de l’authentification native HA et de `mobile_app`.

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

Le build mobile_app actuel ne fournit pas de heartbeat dédié ni d’accusé de réception des commandes. `Disponibilité` est donc une estimation basée sur la fraîcheur des entités source, et un appel `notify` réussi prouve uniquement que HA a accepté l’appel. Les commandes médias directes et les commandes HTTP nécessitent une API Portal à ajouter ultérieurement; voir [`docs/PORTAL_API_REQUESTS.md`](docs/PORTAL_API_REQUESTS.md) et [`docs/CONTRACT.md`](docs/CONTRACT.md).

## Développement

```sh
python -m pip install 'pytest-homeassistant-custom-component>=0.13.200' 'ruff>=0.14'
ruff check .
pytest
```

Les tests utilisent `pytest-homeassistant-custom-component`; un vrai Home Assistant et un Portal restent requis pour valider le canal mobile_app réel, les capteurs publiés, la réception des push et l’action de l’écran.
