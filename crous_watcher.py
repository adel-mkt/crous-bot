#!/usr/bin/env python3
"""
Crous Watcher — surveille trouverunlogement.lescrous.fr et envoie une alerte
Telegram dès qu'une nouvelle offre correspond à l'une des résidences ciblées.

Conçu pour tourner via un cron GitHub Actions (voir .github/workflows/crous-watch.yml),
toutes les 5 minutes, sans que ton PC ait besoin d'être allumé.
"""

import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# CONFIGURATION — à adapter
# ----------------------------------------------------------------------------

# Code "tool" du site pour l'année visée. 45 = 2026-2027 (confirmé le 06/07/2026).
# Si ça change, va sur https://trouverunlogement.lescrous.fr/ et regarde le lien
# "Pour l'année prochaine" -> le nombre après /tools/ dans l'URL.
TOOL_CODE = 45

BASE_SEARCH_URL = f"https://trouverunlogement.lescrous.fr/tools/{TOOL_CODE}/search"

# Nombre max de pages à scanner à chaque passage (protection anti-boucle infinie
# si jamais la pagination totale explose). Le site indiquait ~60 pages début juillet 2026.
MAX_PAGES = 80

# Pause entre deux requêtes de pages, pour rester poli avec le serveur.
DELAY_BETWEEN_PAGES = 1.0

# Résidences ciblées. Normalisation faite automatiquement (accents/majuscules ignorés),
# donc "Résidence Vauban" matchera aussi "RESIDENCE VAUBAN" ou "residence vauban".
TARGET_RESIDENCES = [
    "Jacqueline de Romilly",
    "La Fresque",
    "Le 71",
    "Jacqueline Auriol",
    "Adrienne Bolland",
    "Vauban",
    "René Cassin",
    "Jean-Baptiste Lamarck",
    "Simone Weil",
    "Le Vieux Pozzo",
    "Les Écuries Malaquais",
]

# Filtres optionnels. Mets None pour désactiver.
MAX_PRICE = None       # ex: 450 pour ne garder que les loyers <= 450€
ALLOWED_TYPES = None   # ex: ["Individuel"] pour exclure colocation/couple

# Fichier qui garde la mémoire des offres déjà vues, pour ne pas re-notifier.
SEEN_FILE = Path(__file__).parent / "seen_offers.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ACCOMMODATION_RE = re.compile(rf"/tools/{TOOL_CODE}/accommodations/(\d+)")
PRICE_RE = re.compile(r"(\d[\d\s]*(?:,\d+)?)\s*€")


# ----------------------------------------------------------------------------
# Utilitaires
# ----------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Enlève accents, met en minuscule, pour comparer les noms sans piège d'accents."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


NORMALIZED_TARGETS = [normalize(r) for r in TARGET_RESIDENCES]


def matches_target(name: str) -> bool:
    norm_name = normalize(name)
    return any(target in norm_name for target in NORMALIZED_TARGETS)


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("!! TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant, message non envoyé :")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"!! Erreur Telegram ({resp.status_code}): {resp.text}")


# ----------------------------------------------------------------------------
# Scraping
# ----------------------------------------------------------------------------

def fetch_all_pages_html() -> list[str]:
    """
    Récupère le HTML de toutes les pages de résultats en pilotant un vrai
    navigateur headless (Chromium via Playwright). Nécessaire car le site
    exécute du JavaScript et bloque les simples requêtes HTTP brutes.
    """
    htmls = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            locale="fr-FR",
            user_agent=BROWSER_USER_AGENT,
        )
        page = context.new_page()

        page.goto(BASE_SEARCH_URL, wait_until="networkidle", timeout=30000)
        try:
            page.wait_for_selector(f"a[href*='/tools/{TOOL_CODE}/accommodations/']", timeout=15000)
        except Exception:
            print("Avertissement: aucun lien de logement détecté après l'attente initiale.")

        print(f"[diagnostic] Titre de la page chargée: {page.title()!r}")
        print(f"[diagnostic] URL finale après navigation: {page.url!r}")
        body_snippet = page.inner_text("body")[:500] if page.locator("body").count() else "(pas de <body>)"
        print(f"[diagnostic] Extrait du texte visible: {body_snippet!r}")

        first_html = page.content()
        htmls.append(first_html)

        total_pages = get_total_pages(first_html)
        print(f"{total_pages} pages à scanner.")

        for page_num in range(2, total_pages + 1):
            time.sleep(DELAY_BETWEEN_PAGES)
            try:
                page.goto(
                    f"{BASE_SEARCH_URL}?page={page_num}",
                    wait_until="networkidle",
                    timeout=30000,
                )
                htmls.append(page.content())
            except Exception as e:  # navigation timeout, etc.
                print(f"Erreur sur la page {page_num}: {e}")
                continue

        browser.close()
    return htmls


def parse_listings(html: str) -> list[dict]:
    """
    Extrait les logements d'une page de résultats.

    NOTE: le site peut changer sa structure HTML. Cette fonction cherche les liens
    vers /tools/{TOOL_CODE}/accommodations/{id} et remonte au conteneur parent pour
    en extraire le nom, l'adresse et le prix. Si le site change radicalement de
    structure, il faudra ajuster ici (dis-le moi, je peux la debug avec toi une fois
    que tu as accès à une vraie page).
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    seen_ids_this_page = set()

    for link in soup.find_all("a", href=ACCOMMODATION_RE):
        match = ACCOMMODATION_RE.search(link["href"])
        if not match:
            continue
        acc_id = match.group(1)
        if acc_id in seen_ids_this_page:
            continue
        seen_ids_this_page.add(acc_id)

        name = link.get_text(strip=True)
        if not name:
            continue

        # Remonte jusqu'à un conteneur raisonnable (article/li/div) pour choper
        # le prix et l'adresse à proximité du lien.
        container = link
        for _ in range(5):
            if container.parent is None:
                break
            container = container.parent
            if container.name in ("article", "li", "div") and len(container.get_text(strip=True)) > 20:
                break

        block_text = container.get_text(" ", strip=True)

        price = None
        price_match = PRICE_RE.search(block_text)
        if price_match:
            price = float(price_match.group(1).replace(" ", "").replace(",", "."))

        cohab_type = None
        for t in ("Colocation", "Couple", "Individuel"):
            if t in block_text:
                cohab_type = t
                break

        listings.append({
            "id": acc_id,
            "name": name,
            "url": f"https://trouverunlogement.lescrous.fr/tools/{TOOL_CODE}/accommodations/{acc_id}",
            "price": price,
            "type": cohab_type,
            "raw_text": block_text[:300],
        })

    return listings


PAGE_LINK_RE = re.compile(r"[?&]page=(\d+)")


def get_total_pages(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")

    # Stratégie 1 : chercher les liens de pagination (?page=N) et prendre le plus grand.
    page_numbers = []
    for link in soup.find_all("a", href=True):
        m = PAGE_LINK_RE.search(link["href"])
        if m:
            page_numbers.append(int(m.group(1)))
    if page_numbers:
        return min(max(page_numbers), MAX_PAGES)

    # Stratégie 2 : chercher un texte du type "page 1 sur 60" n'importe où sur la page.
    text = soup.get_text(" ", strip=True)
    m = re.search(r"page\s+1\s+sur\s+(\d+)", text, re.IGNORECASE)
    if m:
        return min(int(m.group(1)), MAX_PAGES)

    # Stratégie 3 : chercher un texte du type "1423 logements" pour au moins savoir
    # qu'il y a plusieurs pages, sans connaître le nombre exact (fallback prudent).
    m = re.search(r"(\d[\d\s]*)\s*logements?", text, re.IGNORECASE)
    if m:
        print(f"Avertissement: pagination non détectée, mais texte trouvé: {m.group(0)!r}")

    return 1


def passes_filters(listing: dict) -> bool:
    if MAX_PRICE is not None and listing["price"] is not None and listing["price"] > MAX_PRICE:
        return False
    if ALLOWED_TYPES is not None and listing["type"] is not None and listing["type"] not in ALLOWED_TYPES:
        return False
    return True


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> None:
    seen = load_seen()
    new_matches = []
    all_matching_ids_this_run = set()

    try:
        pages_html = fetch_all_pages_html()
    except Exception as e:
        print(f"Erreur lors de la récupération des pages: {e}")
        sys.exit(0)  # on ne fait pas planter le cron pour une erreur réseau ponctuelle

    all_listings_count = 0
    for html in pages_html:
        page_listings = parse_listings(html)
        all_listings_count += len(page_listings)
        for listing in page_listings:
            if not matches_target(listing["name"]):
                continue
            all_matching_ids_this_run.add(listing["id"])
            if not passes_filters(listing):
                continue
            if listing["id"] not in seen:
                new_matches.append(listing)

    print(f"{all_listings_count} logement(s) détecté(s) au total sur {len(pages_html)} page(s) scannée(s).")

    for listing in new_matches:
        price_txt = f"{listing['price']:.0f} €" if listing["price"] else "prix non détecté"
        type_txt = listing["type"] or "type non détecté"
        message = (
            f"🏠 <b>Nouvelle offre CROUS</b>\n"
            f"<b>{listing['name']}</b>\n"
            f"{price_txt} — {type_txt}\n"
            f"{listing['url']}"
        )
        send_telegram(message)
        seen[listing["id"]] = {"name": listing["name"], "seen_at": time.time()}
        print(f"Alerte envoyée: {listing['name']} ({listing['url']})")

    if not new_matches:
        print(f"Aucune nouvelle offre. {len(all_matching_ids_this_run)} offre(s) ciblée(s) déjà connue(s).")

    save_seen(seen)


if __name__ == "__main__":
    main()
