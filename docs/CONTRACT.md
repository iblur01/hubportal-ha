# Contrat HubPortal / Home Assistant

Version du contrat : **2**. Deux transports coexistent :

- **v1 (notify)** : action du service `notify.mobile_app_<portal>` avec `message` et `data.command`, relayée au Portal par `mobile_app`. Il reste le seul transport des Portals **sans maillage**, c'est-à-dire sans clé `proto` dans le TXT mDNS. Tout ce qui suit jusqu'à « Extension transport HTTP » est le contrat v1, inchangé (l'enveloppe `contract_version` reste `1`).
- **v2 (maillage)** : WebSocket sur TLS direct entre HA et chaque Portal membre, décrit dans la partie « Contrat v2 : maillage » en fin de document. Spécification de référence : `docs/ARCHITECTURE.md` §16bis du dépôt Qt (D4, ADR-13 à ADR-15).

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

---

# Contrat v2 : maillage

## Rôle de HA

HA est un **membre authentifié et non votant**. Il ne lance jamais d'élection, ne compte pas dans le quorum et ne réécrit pas l'epoch (il émet l'epoch observée). Il découvre les Portals, s'y connecte, lit leurs états, commande un Portal précis (commande ciblée) ou envoie une commande de groupe routée au maître élu, et reflète l'état du maître. Son arrêt n'a aucun effet sur le maillage : à l'arrêt, il ferme ses WebSockets et n'envoie rien d'autre.

## Découverte

Service mDNS `_hubportal._tcp.local.` (déclaré dans `manifest.json`). TXT lus : `portal_id`, `network_id`, `proto`, `ver`. Un Portal **sans `proto`** n'est pas un membre de maillage : le flux de découverte s'interrompt (`not_mesh`) et le Portal reste servi par le transport v1. Un `proto` autre que `1` interrompt le flux (`proto_unsupported`). Le service ne publie aucun secret. Port TCP **8447**, chemin `/mesh`. Pour un membre déjà enregistré, HA essaie l'adresse annoncée si elle diffère : il ne la retient que si la connexion TLS présente l'empreinte épinglée (changement d'IP repris, adresse absente renseignée). Sinon l'ancienne adresse est conservée et l'annonce est signalée (événement `hubportal_mesh_announce`, journal). Une annonce ne modifie jamais l'empreinte épinglée. Le mDNS ne traverse pas un bridge Docker ni un VLAN : voir « Limites ».

## Format de fil

WebSocket sur TLS, chemin `/mesh`, port 8447. Enveloppe JSON :

```json
{"t": "command", "from": "<portal_id>", "seq": 1793395200000, "ts": 1793395200, "epoch": 4, "p": {}, "mac": "<hex>"}
```

`mac = HMAC-SHA256(clé_réseau, t|from|seq|ts|epoch|sha256hex(json_compact(p)))`, avec `json_compact = json.dumps(p, sort_keys=True, separators=(",", ":"))` (`ensure_ascii=False` : UTF-8 brut, sans échappement `\uXXXX`; les hash et HMAC portent sur les octets UTF-8). La clé HMAC est la clé réseau décodée de l'hexadécimal (32 octets).

| Type | `p` | Sens |
|---|---|---|
| `hello` | `proto` (1), `nonce` (hex, 32 caractères), `fp`, `auth` | Les deux sens |
| `join_commit` | `c` | HA vers Portal, `mac` vide |
| `join_ack` | `c` | Portal vers HA, `mac` vide |
| `join_confirm` | `{}` | HA vers Portal, `mac` vide, après confirmation du code |
| `join_request` | `nonce` (révélation de nHA), `name`, `kind: "ha"` | HA vers Portal, `mac` vide (HA n'a pas encore la clé) |
| `join_reveal` | `nonce` (nA) | Portal vers HA, `mac` vide |
| `join_accept` | `network_id`, `key` (hex), `epoch`, `members: [{portal_id, fp, name, ha}]` | Portal vers HA, `mac` calculé avec la clé qu'il transporte |
| `join_reject` | `reason` | Portal vers HA |
| `heartbeat` | `leader`, `index` | Un heartbeat dont `from == leader` est un heartbeat de maître. Les heartbeats d'acquittement des suiveurs sont ignorés par HA |
| `command` | `target` (`portal_id` ou `"*"`), `cmd`, `data` | HA vers Portal |
| `members` | `epoch`, `members: [{portal_id, fp, name, ha, priority}]` | Maître vers HA à chaque changement de membres |
| `state` | `screen`, `action_lock`, `brightness`, `timer`, `timer_end`, `timer_remaining` | Portal vers HA (extension, voir ci-dessous) |

`fp` est le SHA-256 en hexadécimal minuscule du certificat DER. HA présente un certificat client auto-signé (généré à l'admission) pour que le Portal obtienne son `fp`. `cmd` reprend les noms du tableau v1 (`command_screen_on`, `command_screen_off`, `command_screen_brightness_level` valeur 0–255, `command_action_lock`, `command_timer_start`, `command_timer_cancel`) et `data` leur valeur.

**`hello` en deux temps** (enveloppe avec `mac` vide : le champ `auth` lie la clé, les nonces et les empreintes; un mac valide est aussi toléré en réception). `auth = HMAC(clé, "hello|" + nonce_pair + "|" + portal_id + "|" + fp_local + "|" + fp_pair)` exige le nonce du pair. Chaque côté envoie donc d'abord un `hello` avec son `nonce` et `auth` vide, puis, en recevant celui du pair, un second `hello` avec `auth`. La session est établie quand HA a envoyé le sien et vérifié celui du pair. L'empreinte TLS observée doit égaler `fp` et l'empreinte épinglée.

**`state`** n'est pas défini par ARCHITECTURE §16bis : c'est une extension convenue avec le lot Qt. Les valeurs sont celles des capteurs v1 (`screen`/`action_lock` : `on` ou `off`; `timer` : `idle`, `active`, `ringing`; `timer_end` : ISO 8601; `timer_remaining` : secondes).

**Rejet** (le message est ignoré, la session reste ouverte, sauf identité) : `seq` non strictement croissant (par émetteur et par session), `ts` hors de ±60 s, `mac` invalide, `from` différent du pair connecté. Une empreinte TLS ou un `fp` différents de l'empreinte épinglée pour ce `portal_id` ferment la session (identité clonée) et déclenchent un journal et l'événement `hubportal_mesh_alert`.

## Admission

1. Découverte (ou saisie d'une adresse) puis confirmation dans le config flow.
2. HA ouvre le TLS, voit l'empreinte du Portal et se présente par un `hello` (`auth` vide, `mac` vide) : l'admetteur rejette (`identity`) tout `join_*` d'un pair qui ne s'est pas présenté. **Commit-reveal des nonces** (nonces de 16 octets, 32 caractères hex), messages non signés (`mac` vide) :
   `join_commit {c = SHA-256(octets(nHA))}` (HA) puis `join_ack {c = SHA-256(octets(nA))}` (admetteur), puis HA révèle `nHA` dans `join_request {nonce, name, kind: "ha"}`, et l'admetteur révèle `nA` dans `join_reveal {nonce}`. HA vérifie que `SHA-256(octets(nA))` égale le `c` du `join_ack`; sinon l'admission est annulée (`mesh_mismatch`). Aucun côté ne peut choisir son nonce après avoir vu celui de l'autre.
3. HA calcule et affiche le **code à 6 chiffres** (non secret) : `SHA-256(b"hubportal-sas" + octets(fp_HA) + octets(fp_admetteur) + octets(nHA) + octets(nA)) mod 10^6`, entier gros-boutiste, sur 6 chiffres. **L'utilisateur doit confirmer explicitement dans HA que le code est identique** à celui du Portal; sans cette confirmation, HA n'attend pas `join_accept`, n'en consomme aucun et ne crée aucune entrée. Un homme du milieu produit deux empreintes, donc deux codes : refuser si les codes diffèrent.
4. L'acceptation se fait **sur un Portal**. Après la confirmation, HA envoie `join_confirm {}` (`mac` vide; sans lui l'admetteur n'envoie jamais `join_accept`) puis attend `join_accept`, qui peut n'arriver qu'après validation de l'admission par le maître (jusqu'à environ 10 s) ou être remplacé par `join_reject` (délai total de 120 s depuis le début de la demande). Un `join_reject`, une fermeture, un délai ou une empreinte d'admetteur incohérente annulent l'admission sans créer d'entrée.
5. HA épingle l'empreinte de chaque Portal fournie par `join_accept`. Il rejette un `join_accept` dont l'émetteur n'est pas l'admetteur du commit-reveal, ou dont la liste contient HA de façon incohérente avec sa demande (HA doit y figurer, s'il y figure, avec `ha: true`, son `portal_id` et son `fp`; son `fp` ou son `portal_id` sous une autre identité est refusé). HA se déclare toujours `kind: "ha"`.
6. HA n'émet jamais `mesh.propose`, `mesh.remove_member` ni `mesh.rotate_key` : toute commande `mesh.*` est refusée côté HA.

Les vecteurs dorés (`json_compact` avec noms non ASCII et entiers, `mac`, `hello_auth`, SAS, commit) sont dans `tests/fixtures/mesh_vectors.json`, générés par `scripts/gen_mesh_vectors.py` (référence indépendante du code de l'intégration) et consommés par `tests/test_mesh.py`. Le dépôt Qt doit consommer le même fichier.

## Stockage de la clé réseau (compromis assumé)

La clé réseau, la clé privée du certificat de HA et son identité sont dans `config_entry.data` (stockage `.storage` de HA, en clair, comme les autres entrées). HA est un membre : il détient la clé, donc quiconque lit `.storage` peut agir comme un membre du maillage. Elle n'est jamais dans un état, un attribut d'entité, un journal ni un diagnostic (le diagnostic l'expurge). Rotation et retrait d'un membre : voir ARCHITECTURE §16bis.2 et « Rotation de clé » ci-dessous; HA suit la rotation sans ré-admission tant qu'il était en ligne.

## Entités et commandes

Un device par `portal_id` (`identifiers = (hubportal, portal_id)`), `unique_id` préfixé par `portal_id`. Les entités v1 (écran, luminosité, verrou, minuteur, disponibilité) sont créées pour chaque Portal. Un device « Maillage HubPortal » porte deux capteurs de diagnostic : maître courant et epoch observés.

- **Ciblée** : `command` avec `target = portal_id`, envoyée à ce Portal, même sans maître.
- **Groupe** : `command` avec `target = "*"`, envoyée au maître (service `hubportal.timer_start` / `timer_cancel`, champ `foyer` = `network_id` ou `maillage`). Sans maître (bail de 3 s expiré, minorité, aucun heartbeat), le service échoue avec `HomeAssistantError` « Pas de maître ».
- Aucune commande n'est rejouée, aucun accusé n'existe. Une commande vers un Portal injoignable échoue. Le déverrouillage (`command_action_lock` avec `off`) reste refusé par HA. Un verrou d'actions `on` bloque les autres commandes ciblées.

Suivi du maître : HA retient `leader` et l'epoch du dernier heartbeat de maître valide. Une epoch inférieure est ignorée (ancien maître). À epoch égale, le maître courant n'est pas remplacé tant que son bail (3 s) est valide. Sans heartbeat pendant 3 s, il n'y a plus de maître jusqu'au prochain heartbeat d'une epoch supérieure.

## Rotation de clé

Quand un membre est retiré, le maître envoie à chaque membre restant, HA compris, `t="command"` avec `p = {target: <portal_id de HA>, cmd: "mesh.rotate_key", data: {key: <hex>, epoch: N+1}}`, signé avec l'**ancienne** clé. HA l'accepte seulement si `from` est le maître courant (bail valide), si le mac est valide avec l'ancienne clé, si `target` est HA et si `epoch` n'est pas inférieure à l'epoch connue. Il remplace alors la clé et l'epoch dans `config_entry.data`, ferme toutes ses sessions et se reconnecte avec la nouvelle clé. Toute autre origine, un mac invalide ou une autre cible sont ignorés (compteurs `not_master`, `mac`, `format`). La nouvelle clé n'est jamais journalisée.

## Liste des membres

`t="members"`, `p = {epoch, members: [{portal_id, fp, name, ha, priority}]}`, diffusé par le maître à chaque changement de membres. HA le reçoit en lecture seule (il ne vote pas) et ignore tout `members` qui ne vient pas du maître courant ou dont l'epoch est inférieure à la sienne. Les entrées `ha: true` et HA lui-même sont ignorées. Un Portal nouveau est ajouté à `config_entry.data` (l'entrée est rechargée : nouveau device et entités sans ré-admission); un Portal absent de la liste est retiré, avec son device. L'empreinte déjà épinglée d'un membre n'est jamais remplacée par une liste reçue (protection contre le clonage); seuls le nom et la priorité sont mis à jour.

## Reconnexion

Backoff **full jitter** : attente aléatoire dans `[0, min(60 s, 1 s × 2^essai)]`, remis à zéro après une session établie.

## Migration v1 vers v2

1. Mettre à jour HubPortal et le Portal Qt (le Portal doit publier `proto`).
2. Ajouter l'intégration : le Portal découvert propose « Rejoindre le maillage ». Comparez le code, acceptez sur un Portal.
3. Les entités maillage ont de nouveaux `unique_id` (par `portal_id`) : elles ne remplacent pas les entités v1. Une fois les automatisations adaptées, supprimez les entrées v1 du même Portal (sinon deux jeux d'entités coexistent).
4. Les entrées v1 existantes restent fonctionnelles; un Portal sans `proto` reste sur `notify`.

## Limites

- Les adresses viennent de la découverte mDNS (ou de l'adresse saisie pour l'admetteur); sans mDNS (Docker en bridge, VLAN), la saisie d'adresse n'est proposée que par le formulaire utilisateur lorsqu'au moins un Portal mobile_app est détecté.
- Si HA est hors ligne pendant une rotation de clé, il ne reçoit pas `mesh.rotate_key` et doit être ré-admis.
- Un Portal ajouté via `members` apparaît dans HA (device, entités) mais sa connexion attend son adresse, fournie par la découverte mDNS (ou une nouvelle annonce). Un `members` ne fournit ni adresse ni certificat.
- `seq` est vérifié par session : un message capturé n'est rejeté sur une autre session que par la fenêtre `ts` de ±60 s.
- Aucune confirmation d'exécution des commandes; état et maître reflètent les derniers messages reçus.
