# Crous Watcher

Bot qui surveille trouverunlogement.lescrous.fr et t'envoie une alerte Telegram
dès qu'une offre apparaît dans l'une des résidences que tu vises. Tourne sur
GitHub Actions (gratuit), pas besoin de garder ton PC allumé.

## 1. Créer le bot Telegram

1. Ouvre Telegram, cherche **@BotFather**.
2. Envoie `/newbot`, choisis un nom et un username (doit finir par "bot").
3. BotFather te donne un **token** du genre `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx`.
   Garde-le, tu en auras besoin à l'étape 3.
4. Envoie n'importe quel message à ton nouveau bot (ex: "salut") — c'est
   nécessaire pour qu'il puisse ensuite t'écrire.

## 2. Récupérer ton chat ID

1. Va sur cette URL dans ton navigateur (remplace `<TOKEN>` par ton token) :
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
2. Cherche `"chat":{"id":123456789,...}` dans la réponse JSON — ce nombre est
   ton **chat ID**.
   (Si la réponse est vide, renvoie un message au bot sur Telegram puis
   recharge la page.)

## 3. Créer le repo GitHub

1. Crée un nouveau repo GitHub (peut être **privé**, ça marche pareil).
2. Mets-y tous les fichiers de ce dossier (`crous_watcher.py`,
   `requirements.txt`, `seen_offers.json`, `.github/workflows/crous-watch.yml`,
   ce README).
3. Dans le repo : **Settings → Secrets and variables → Actions → New repository
   secret**, ajoute :
   - `TELEGRAM_TOKEN` = le token de l'étape 1
   - `TELEGRAM_CHAT_ID` = le chat ID de l'étape 2
4. Onglet **Actions** du repo : GitHub peut demander de confirmer l'activation
   des workflows la première fois — clique pour activer.

C'est tout. Le bot tourne automatiquement toutes les 5 minutes. Tu peux aussi
le lancer manuellement depuis l'onglet Actions → "Crous Watch" → "Run workflow"
pour tester tout de suite sans attendre.

## 4. Ajuster les critères

Tout se règle en haut de `crous_watcher.py` :

- `TARGET_RESIDENCES` : liste des noms de résidences (déjà pré-remplie avec
  les tiennes).
- `MAX_PRICE` : loyer max, ou `None` pour ne pas filtrer.
- `ALLOWED_TYPES` : `["Individuel"]` par exemple pour exclure colocation/couple,
  ou `None` pour tout accepter.

## 5. Point important — à vérifier demain

Ce script a été écrit le 6 juillet 2026, avant l'ouverture de la phase
complémentaire (7 juillet). La structure de la page de résultats peut changer
légèrement une fois que l'offre réelle de logements sera en ligne. Si le bot
ne détecte aucune offre alors qu'il y en a manifestement sur le site, montre-moi
une capture ou l'URL exacte de la page de résultats et j'ajuste le parsing en
quelques minutes.

## Limites à connaître

- Le bot ne réserve rien à ta place — il t'alerte, à toi d'aller confirmer vite
  sur le site (c'est premier arrivé premier servi).
- GitHub Actions peut avoir quelques minutes de retard sur les cron en période
  de forte charge sur leur plateforme (rare, mais possible).
- Le scraping HTML est un peu fragile par nature : si le CROUS refond son site,
  il faudra retoucher `parse_listings()`.
