#!/usr/bin/env python3
"""
Idealista Aste Scraper - Headless CI Version
Adapted for GitHub Actions: no input(), headless Chrome, parametric URL construction.
Original: idealista_scraper_aste.py (untouched)
"""

import os
import re
import sys
import json
import time
import random
import argparse
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException
)


class HeadlessIdealistaScraper:
    """Headless scraper for Idealista auction listings, designed for CI/CD."""

    # Mapping tipo_immobile → URL slug
    TIPO_SLUG = {
        'case': 'case',
        'appartamenti': 'appartamenti',
        'ville': 'ville',
        'attici': 'attici',
        'rustici': 'rustici',
        'garage': 'garage',
        'terreni': 'terreni',
        'uffici': 'uffici',
        'locali': 'locali-commerciali',
    }

    def __init__(self, email, password, output_dir="docs/data"):
        self.email = email
        self.password = password
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.base_url = "https://www.idealista.it"
        self.driver = None
        self.wait = None
        self.listings = []

    # ──────────────────────────────────────────────
    #  SETUP
    # ──────────────────────────────────────────────

    def setup_driver(self):
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument(
            '--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        )
        chrome_options.add_argument('--log-level=3')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        self.driver = webdriver.Chrome(options=chrome_options)
        self.driver.execute_cdp_cmd(
            'Page.addScriptToEvaluateOnNewDocument',
            {'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'}
        )
        self.wait = WebDriverWait(self.driver, 15)
        print("[OK] Browser headless inizializzato")

    def handle_cookie_consent(self):
        try:
            accept_btn = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    "#didomi-notice-agree-button, "
                    "button[id*='accept'], "
                    "button[class*='accept']"
                ))
            )
            accept_btn.click()
            time.sleep(1)
            print("[OK] Cookie consent accettato")
        except TimeoutException:
            pass

    # ──────────────────────────────────────────────
    #  LOGIN
    # ──────────────────────────────────────────────

    def login(self):
        print(f"[..] Login su {self.base_url}...")
        self.driver.get(f"{self.base_url}/login")
        time.sleep(3)
        self.handle_cookie_consent()
        time.sleep(2)

        try:
            email_input = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    "input[type='email'], input[name='email'], #email"
                ))
            )
            email_input.clear()
            email_input.send_keys(self.email)
            time.sleep(random.uniform(0.5, 1.5))

            password_input = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']"))
            )
            password_input.clear()
            password_input.send_keys(self.password)
            time.sleep(random.uniform(0.5, 1.5))

            login_btn = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    "button[type='submit'], input[type='submit']"
                ))
            )
            login_btn.click()
            time.sleep(5)

            # CAPTCHA check
            if self._has_captcha():
                print("[WARN] CAPTCHA rilevato al login, retry con refresh...")
                self.driver.refresh()
                time.sleep(3)
                if self._has_captcha():
                    print("[ERROR] CAPTCHA persistente al login. Uscita.")
                    return False

            print("[OK] Login effettuato")
            return True

        except Exception as e:
            print(f"[ERROR] Login fallito: {str(e)}")
            return False

    def _has_captcha(self):
        try:
            return self.driver.execute_script(
                "return document.body.innerText.toLowerCase().indexOf('captcha') !== -1"
            )
        except Exception:
            return False

    # ──────────────────────────────────────────────
    #  URL CONSTRUCTION
    # ──────────────────────────────────────────────

    def build_search_url(self, zona, tipo_immobile='case', prezzo_min=None,
                         prezzo_max=None, solo_aste=True):
        """Build Idealista search URL from parameters."""
        tipo_slug = self.TIPO_SLUG.get(tipo_immobile, 'case')

        # Base URL
        url = f"{self.base_url}/vendita-{tipo_slug}/{zona}/"

        # Price filters in path
        filters = []
        if prezzo_min:
            filters.append(f"prezzo_min_{prezzo_min}")
        if prezzo_max:
            filters.append(f"prezzo_max_{prezzo_max}")
        if filters:
            url += f"con-{','.join(filters)}/"

        # Query params
        params = []
        if solo_aste:
            params.append("asta=si")
        if params:
            url += "?" + "&".join(params)

        return url

    # ──────────────────────────────────────────────
    #  SCRAPING PAGINE RISULTATI
    # ──────────────────────────────────────────────

    def collect_listing_urls(self, max_pages=50):
        all_urls = []
        page_num = 1

        while page_num <= max_pages:
            print(f"[..] Pagina {page_num}...")
            urls = self._scrape_results_page()
            all_urls.extend(urls)
            print(f"     {len(urls)} annunci (totale: {len(all_urls)})")

            if not self._go_to_next_page():
                print(f"[OK] Ultima pagina: {page_num}")
                break

            page_num += 1
            time.sleep(random.uniform(2, 4))

        # Deduplica
        seen = set()
        unique = []
        for url in all_urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)

        print(f"[OK] Annunci unici: {len(unique)}")
        return unique

    def _scrape_results_page(self):
        urls = []
        try:
            articles = self.wait.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "article.item"))
            )
            for article in articles:
                try:
                    link = article.find_element(By.CSS_SELECTOR, "a.item-link")
                    href = link.get_attribute('href')
                    if href:
                        urls.append(urljoin(self.base_url, href))
                except NoSuchElementException:
                    continue
        except TimeoutException:
            print("     [WARN] Nessun risultato nella pagina")
        return urls

    def _go_to_next_page(self):
        try:
            next_btn = self.driver.find_element(By.CSS_SELECTOR,
                "a.icon-arrow-right-after, li.next a, a[class*='next']"
            )
            next_btn.click()
            time.sleep(random.uniform(2, 3))
            return True
        except NoSuchElementException:
            return False

    # ──────────────────────────────────────────────
    #  CAPTCHA HANDLING
    # ──────────────────────────────────────────────

    def handle_captcha(self, max_retries=3):
        """Auto-refresh to bypass CAPTCHA. Returns True if resolved."""
        if not self._has_captcha():
            return True

        for attempt in range(max_retries):
            print(f"     [WARN] CAPTCHA rilevato, refresh ({attempt+1}/{max_retries})...")
            self.driver.refresh()
            time.sleep(random.uniform(2, 4))
            if not self._has_captcha():
                print("     [OK] CAPTCHA risolto dopo refresh")
                return True

        print("     [ERROR] CAPTCHA persistente, skip listing")
        return False

    # ──────────────────────────────────────────────
    #  SCRAPING DETTAGLIO ANNUNCIO
    # ──────────────────────────────────────────────

    def scrape_listing_detail(self, url):
        listing = {'url': url}

        self.driver.get(url)
        time.sleep(random.uniform(1, 1.5))

        if not self.handle_captcha():
            listing['skipped_captcha'] = True
            return listing

        # Estrai dati in una singola chiamata JavaScript
        data = self.driver.execute_script("""
            var result = {};

            var titleEl = document.querySelector('h1 span.main-info__title-main, span.main-info__title-main');
            result.titolo = titleEl ? titleEl.textContent.trim() : '/';

            var locEl = document.querySelector('span.main-info__title-minor');
            result.ubicazione = locEl ? locEl.textContent.trim() : '/';

            var priceEl = document.querySelector('.info-data-price .txt-bold');
            var priceContainer = document.querySelector('.info-data-price');
            result.prezzo_raw = priceEl ? priceEl.textContent.trim() : '';
            result.prezzo_display = priceContainer ? priceContainer.textContent.trim() : '/';

            var tagEl = document.querySelector('div.detail-info-tags span.tag.tag__prominent');
            result.data_asta_raw = tagEl ? tagEl.textContent.trim() : '/';

            var featContainer = document.querySelector('.info-features');
            if (featContainer) {
                var spans = featContainer.querySelectorAll('span');
                var feats = [];
                spans.forEach(function(s) { if (s.textContent.trim()) feats.push(s.textContent.trim()); });
                result.caratteristiche_rapide = feats.join(' | ');
            } else {
                result.caratteristiche_rapide = '/';
            }

            var featureDivs = document.querySelectorAll('div.details-property-feature-one, div.details-property-feature-two');
            var allFeatures = [];
            featureDivs.forEach(function(div) {
                var h3 = div.querySelector('h3');
                if (h3) allFeatures.push('[' + h3.textContent.trim() + ']');
                var lis = div.querySelectorAll('li');
                lis.forEach(function(li) { if (li.textContent.trim()) allFeatures.push(li.textContent.trim()); });
            });
            result.caratteristiche = allFeatures.length > 0 ? allFeatures.join('\\n') : '/';

            var descEl = document.querySelector("div.comment div[class*='lang']") ||
                         document.querySelector('div.comment') ||
                         document.querySelector('div.adCommentsLanguage');
            result.descrizione_raw = descEl ? descEl.textContent.trim() : '';

            return result;
        """)

        if not data:
            listing['skipped_captcha'] = True
            return listing

        listing['titolo'] = data.get('titolo', '/')

        # Tipologia
        title = listing['titolo']
        tipo_match = re.match(r'^(\S+)\s+(?:all\'asta|in\s)', title, re.IGNORECASE)
        listing['tipologia'] = tipo_match.group(1) if tipo_match else (title.split()[0] if title and title != '/' else '/')

        listing['ubicazione'] = data.get('ubicazione', '/')

        # Prezzo
        price_raw = data.get('prezzo_raw', '')
        listing['prezzo'] = data.get('prezzo_display', '/')
        if price_raw:
            price_clean = price_raw.replace('.', '').replace(',', '.').strip()
            try:
                listing['prezzo_numerico'] = float(price_clean)
            except ValueError:
                listing['prezzo_numerico'] = 0.0
        else:
            listing['prezzo_numerico'] = 0.0

        # Data asta
        data_asta_raw = data.get('data_asta_raw', '/')
        date_match = re.search(r'(\d{2}/\d{2}/\d{2,4})', data_asta_raw)
        if date_match:
            parts = date_match.group(1).split('/')
            if len(parts[2]) == 2:
                parts[2] = '20' + parts[2]
            listing['data_asta'] = '/'.join(parts)
        else:
            listing['data_asta'] = data_asta_raw if data_asta_raw else '/'

        listing['caratteristiche_rapide'] = data.get('caratteristiche_rapide', '/')
        listing['caratteristiche'] = data.get('caratteristiche', '/')

        # Descrizione sintetica
        listing['descrizione'] = self._extract_description_keypoints(data.get('descrizione_raw', ''))

        # Immagini (solo URL, no download)
        listing['image_urls'] = self._extract_image_urls()

        return listing

    def _extract_description_keypoints(self, full_text):
        if not full_text:
            return '/'

        keypoints = []

        mq_match = re.search(r'(\d+)\s*(?:mq|m2|m²|metri\s*quadr)', full_text, re.IGNORECASE)
        keypoints.append(f"Superficie: {mq_match.group(1)} mq" if mq_match else "Superficie: /")

        piano_match = re.search(r'(?:piano|pian[oi])\s*[:\-]?\s*(\w+)', full_text, re.IGNORECASE)
        keypoints.append(f"Piano: {piano_match.group(1)}" if piano_match else "Piano: /")

        stato_patterns = [
            r'(?:buono stato|ottimo stato|da ristrutturare|ristrutturato|nuovo|abitabile)',
            r'(?:stato)\s*[:\-]?\s*(.+?)(?:\n|,|\.|$)',
        ]
        stato_found = False
        for pattern in stato_patterns:
            stato_match = re.search(pattern, full_text, re.IGNORECASE)
            if stato_match:
                stato_text = stato_match.group(0).strip() if not stato_match.lastindex else stato_match.group(1).strip()
                keypoints.append(f"Stato: {stato_text}")
                stato_found = True
                break
        if not stato_found:
            keypoints.append("Stato: /")

        anno_match = re.search(r'(?:anno|costrui\w+|realizzat\w+)\s*[:\-]?\s*(?:nel\s+)?(\d{4})', full_text, re.IGNORECASE)
        if not anno_match:
            anno_match = re.search(r'\b(19\d{2}|20[0-2]\d)\b', full_text)
        keypoints.append(f"Anno: {anno_match.group(1)}" if anno_match else "Anno: /")

        return ' | '.join(keypoints)

    # ──────────────────────────────────────────────
    #  IMMAGINI (solo URL, no download)
    # ──────────────────────────────────────────────

    def _extract_image_urls(self):
        images = {'foto': [], 'planimetrie': []}

        try:
            # Scrolla alla sezione multimedia
            self.driver.execute_script("""
                var mm = document.querySelector('#main-multimedia');
                if (mm) mm.scrollIntoView({behavior: 'instant', block: 'start'});
            """)
            time.sleep(0.3)

            # Rivela immagini nascoste
            self.driver.execute_script("""
                var moreButtons = document.querySelectorAll(
                    '#main-multimedia .more, #secondary-multimedia .more, '
                    + '.photos-container .more, .plans-container .more'
                );
                moreButtons.forEach(function(btn) {
                    if (btn.offsetParent !== null) btn.click();
                });
                document.querySelectorAll('.hide_multimedia').forEach(function(el) {
                    el.classList.remove('hide_multimedia');
                    el.style.display = '';
                });
            """)
            time.sleep(0.5)

            # Raccogli foto
            for selector in ['#main-multimedia .photos-container', '#main-multimedia', 'div.photos-container']:
                foto_urls = self._collect_urls_via_js(selector)
                if foto_urls:
                    images['foto'] = [self._transform_image_url(u) for u in foto_urls]
                    break

            # Raccogli planimetrie
            for selector in ['#secondary-multimedia .plans-container', '#secondary-multimedia', 'div.plans-container']:
                plan_urls = self._collect_urls_via_js(selector)
                if plan_urls:
                    images['planimetrie'] = [self._transform_image_url(u) for u in plan_urls]
                    break

        except Exception as e:
            print(f"     [WARN] Errore immagini: {str(e)[:60]}")

        return images

    def _collect_urls_via_js(self, container_selector):
        urls = self.driver.execute_script("""
            var container = document.querySelector(arguments[0]);
            if (!container) return [];
            var urls = new Set();
            var imgs = container.querySelectorAll('picture img, img');
            imgs.forEach(function(img) {
                var src = img.getAttribute('src') ||
                          img.getAttribute('data-src') ||
                          img.getAttribute('data-service') || '';
                if (src && src.indexOf('idealista') !== -1 && src.indexOf('image.master') !== -1) {
                    urls.add(src);
                }
            });
            var sources = container.querySelectorAll('picture source[srcset]');
            sources.forEach(function(source) {
                var srcset = source.getAttribute('srcset') || '';
                srcset.split(',').forEach(function(part) {
                    var src = part.trim().split(' ')[0];
                    if (src && src.indexOf('idealista') !== -1 &&
                        src.indexOf('image.master') !== -1 && src.indexOf('.jpg') !== -1) {
                        urls.add(src);
                    }
                });
            });
            return Array.from(urls);
        """, container_selector)
        return urls or []

    def _transform_image_url(self, url):
        if not url:
            return url
        return re.sub(r'(idealista\.it)/(?:blur/)?[A-Z][A-Z0-9_-]+/', r'\1/', url)

    # ──────────────────────────────────────────────
    #  SALVATAGGIO
    # ──────────────────────────────────────────────

    def save_results(self, search_name):
        if not self.listings:
            print("[WARN] Nessun dato da salvare.")
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', search_name)
        filename = f"{clean_name}_{timestamp}.json"
        json_path = self.output_dir / filename

        json_data = {
            'metadata': {
                'search_name': search_name,
                'timestamp': datetime.now().isoformat(),
                'total_listings': len(self.listings),
                'skipped_captcha': sum(1 for l in self.listings if l.get('skipped_captcha')),
            },
            'listings': []
        }

        for listing in self.listings:
            entry = dict(listing)
            img_urls = entry.pop('image_urls', {'foto': [], 'planimetrie': []})
            entry['urls_foto'] = img_urls.get('foto', [])
            entry['urls_planimetrie'] = img_urls.get('planimetrie', [])
            json_data['listings'].append(entry)

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

        print(f"[OK] JSON salvato: {json_path}")

        # Aggiorna indice ricerche
        self._update_search_index(search_name, filename, json_data['metadata'])

        return json_path

    def _update_search_index(self, search_name, filename, metadata):
        """Aggiorna docs/data/index.json con la lista di tutte le ricerche."""
        index_path = self.output_dir / "index.json"

        if index_path.exists():
            with open(index_path, 'r', encoding='utf-8') as f:
                index = json.load(f)
        else:
            index = {'searches': []}

        index['searches'].append({
            'name': search_name,
            'file': filename,
            'timestamp': metadata['timestamp'],
            'total': metadata['total_listings'],
        })

        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    # ──────────────────────────────────────────────
    #  DEDUPLICA
    # ──────────────────────────────────────────────

    def _remove_duplicates(self):
        seen = set()
        unique = []
        for listing in self.listings:
            titolo = listing.get('titolo', '').strip()
            if titolo and titolo != '/':
                key = titolo
            else:
                key = (listing.get('ubicazione', ''), listing.get('caratteristiche', ''))
            if key in seen:
                continue
            seen.add(key)
            unique.append(listing)
        removed = len(self.listings) - len(unique)
        if removed > 0:
            print(f"[OK] Rimossi {removed} duplicati")
        self.listings = unique

    # ──────────────────────────────────────────────
    #  ORCHESTRATORE
    # ──────────────────────────────────────────────

    def run(self, search_url=None, zona=None, tipo_immobile='case',
            prezzo_min=None, prezzo_max=None, solo_aste=True,
            max_pages=10, search_name='ricerca'):
        try:
            print("=" * 60)
            print("Idealista Scraper - Headless CI Mode")
            print("=" * 60)

            self.setup_driver()

            if not self.login():
                print("[ERROR] Login fallito, uscita.")
                return False

            # Determina URL di ricerca
            if search_url:
                url = search_url
                print(f"[OK] Uso URL fornito: {url}")
            else:
                url = self.build_search_url(zona, tipo_immobile, prezzo_min,
                                            prezzo_max, solo_aste)
                print(f"[OK] URL costruito: {url}")

            self.driver.get(url)
            time.sleep(3)

            if not self.handle_captcha():
                print("[ERROR] CAPTCHA sulla pagina di ricerca, uscita.")
                return False

            # Raccolta URL
            print("\n[..] Raccolta URL annunci...")
            listing_urls = self.collect_listing_urls(max_pages=max_pages)

            if not listing_urls:
                print("[ERROR] Nessun annuncio trovato.")
                return False

            # Scraping dettagli
            print(f"\n[..] Scraping dettagli ({len(listing_urls)} annunci)...")
            for idx, url in enumerate(listing_urls, 1):
                print(f"\n[{idx}/{len(listing_urls)}] Scraping...")
                try:
                    listing = self.scrape_listing_detail(url)
                    self.listings.append(listing)

                    if not listing.get('skipped_captcha'):
                        print(f"     {listing.get('titolo', '/')[:60]}")
                        print(f"     {listing.get('prezzo', '/')}")
                    else:
                        print("     [SKIP] CAPTCHA")

                except Exception as e:
                    print(f"     [ERROR] {str(e)[:80]}")
                    continue

                time.sleep(random.uniform(1.5, 3))

            # Salva
            self._remove_duplicates()
            result_path = self.save_results(search_name)

            print("\n" + "=" * 60)
            print(f"[DONE] Annunci raccolti: {len(self.listings)}")
            if result_path:
                print(f"[DONE] Output: {result_path}")
            print("=" * 60)
            return True

        except Exception as e:
            print(f"\n[FATAL] {str(e)}")
            import traceback
            traceback.print_exc()
            if self.listings:
                self.save_results(search_name)
            return False

        finally:
            if self.driver:
                print("[..] Chiusura browser...")
                self.driver.quit()


def main():
    parser = argparse.ArgumentParser(description='Idealista Headless Scraper')
    parser.add_argument('--zona', type=str, help='Zona di ricerca (es. milano, roma)')
    parser.add_argument('--tipo', type=str, default='case',
                        choices=list(HeadlessIdealistaScraper.TIPO_SLUG.keys()),
                        help='Tipo immobile')
    parser.add_argument('--prezzo-min', type=int, default=None, help='Prezzo minimo')
    parser.add_argument('--prezzo-max', type=int, default=None, help='Prezzo massimo')
    parser.add_argument('--no-aste', action='store_true', help='Non filtrare solo aste')
    parser.add_argument('--max-pages', type=int, default=10, help='Max pagine risultati')
    parser.add_argument('--search-name', type=str, default='ricerca', help='Nome ricerca')
    parser.add_argument('--search-url', type=str, default=None,
                        help='URL completo Idealista (sovrascrive zona/tipo/prezzo)')
    parser.add_argument('--output-dir', type=str, default='docs/data', help='Directory output')

    args = parser.parse_args()

    email = os.environ.get('IDEALISTA_EMAIL', '')
    password = os.environ.get('IDEALISTA_PASSWORD', '')

    if not email or not password:
        print("[ERROR] Impostare IDEALISTA_EMAIL e IDEALISTA_PASSWORD come variabili d'ambiente")
        sys.exit(1)

    if not args.search_url and not args.zona:
        print("[ERROR] Specificare --zona o --search-url")
        sys.exit(1)

    scraper = HeadlessIdealistaScraper(
        email=email,
        password=password,
        output_dir=args.output_dir
    )

    success = scraper.run(
        search_url=args.search_url,
        zona=args.zona,
        tipo_immobile=args.tipo,
        prezzo_min=args.prezzo_min,
        prezzo_max=args.prezzo_max,
        solo_aste=not args.no_aste,
        max_pages=args.max_pages,
        search_name=args.search_name,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
