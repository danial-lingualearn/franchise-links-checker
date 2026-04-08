#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Franchise Broken Links Checker – Advanced Classification + Email Report"""

import csv
import sys
import time
import threading
import argparse
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional, Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class Config:
    DEFAULT_URL = "https://lingua-learn.com/franchise/"
    DEFAULT_OUTPUT_BASE = "Franchise_Broken_Links_Report"
    DEFAULT_TIMEOUT = 15
    DEFAULT_MAX_WORKERS = 5
    DEFAULT_RETRIES = 3
    DEFAULT_RETRY_DELAY = 2
    DEFAULT_RATE_LIMIT = 0.5
    MIN_CONTENT_LENGTH = 200
    BRAND_KEYWORDS = ["lingua", "learn", "language"]
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }

config = Config()


class RateLimiter:
    def __init__(self, rate: float):
        self.rate = rate
        self.lock = threading.Lock()
        self.last_time = time.monotonic()
        self.tokens = 1.0

    def wait(self):
        if self.rate <= 0:
            return
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_time
            self.tokens += elapsed * self.rate
            if self.tokens > 1.0:
                self.tokens = 1.0
            self.last_time = now
            if self.tokens < 1.0:
                sleep_time = (1.0 - self.tokens) / self.rate
                time.sleep(sleep_time)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


def extract_urls(page_url: str) -> List[Dict[str, Any]]:
    # Try httpx first
    html = None
    try:
        with httpx.Client(timeout=config.DEFAULT_TIMEOUT, headers=config.HEADERS, follow_redirects=True) as client:
            resp = client.get(page_url)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        print(f"HTTPX request failed: {e}")

    if html:
        entries = _parse_links_from_html(page_url, html)
        if entries:
            print(f"Found {len(entries)} links via HTTPX")
            return entries
        else:
            print("HTTPX fetched page but found no links. Falling back to Playwright.")

    # Fallback to Playwright (JavaScript rendering)
    if not PLAYWRIGHT_AVAILABLE:
        sys.exit("Playwright is not installed but required to extract dynamic links.")

    print("Fetching page with Playwright (headless Chromium)...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=config.HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(page_url, timeout=config.DEFAULT_TIMEOUT * 1000, wait_until="networkidle")
            page.wait_for_timeout(2000)  # extra wait for lazy content
            html = page.content()
            browser.close()
    except Exception as e:
        sys.exit(f"Playwright failed to load page: {e}")

    entries = _parse_links_from_html(page_url, html)
    if not entries:
        # Debug: print the page title and first 20 links for inspection
        print("No links found even with Playwright. Page title:", page.title() if 'page' in locals() else 'unknown')
        print("First 20 anchor texts and hrefs:")
        soup = BeautifulSoup(html, "html.parser")
        for i, a in enumerate(soup.find_all("a", href=True)[:20]):
            print(f"  {i+1}. Text: '{a.get_text(strip=True)[:60]}' -> href: {a.get('href')}")
        sys.exit("No franchise links found. Please check the HTML structure.")
    else:
        print(f"Found {len(entries)} links via Playwright")
    return entries

def _parse_links_from_html(page_url: str, html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    entries = []

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        # Look for "visit website" or similar
        if "visit website" in text:
            href = a["href"].strip()
            full_url = urljoin(page_url, href)
            if not full_url.startswith(("http://", "https://")):
                continue
            if "lingua-learn.com" in full_url:
                continue
            country = _extract_country(a)
            if href == "#":
                entries.append({"country": country, "url": full_url, "status": "COMING_SOON", "code": None, "note": "COMING_SOON"})
                continue
            parsed = urlparse(full_url)
            clean_url = urlunparse(parsed._replace(fragment=""))
            if clean_url not in seen:
                seen.add(clean_url)
                entries.append({"country": country, "url": clean_url, "status": None, "code": None, "note": ""})

    if entries:
        return entries

    # Fallback: any external link not on lingua-learn.com
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(page_url, href)
        if not full_url.startswith(("http://", "https://")):
            continue
        if "lingua-learn.com" in full_url:
            continue
        if full_url.rstrip("/") == page_url.rstrip("/"):
            continue
        country = _extract_country(a)
        parsed = urlparse(full_url)
        clean_url = urlunparse(parsed._replace(fragment=""))
        if clean_url not in seen:
            seen.add(clean_url)
            entries.append({"country": country, "url": clean_url, "status": None, "code": None, "note": ""})

    return entries

    # Fallback: collect all external links (not on lingua-learn.com)
    print("No 'visit website' links found. Falling back to all external links.")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(page_url, href)
        if not full_url.startswith(("http://", "https://")):
            continue
        # Exclude links that stay on the same domain
        if "lingua-learn.com" in full_url:
            continue
        # Exclude page itself
        if full_url.rstrip("/") == page_url.rstrip("/"):
            continue
        country = _extract_country(a)
        text_preview = a.get_text(strip=True)[:50]
        print(f"Found external link: {text_preview} -> {full_url}")
        parsed = urlparse(full_url)
        clean_url = urlunparse(parsed._replace(fragment=""))
        if clean_url not in seen:
            seen.add(clean_url)
            entries.append({"country": country, "url": clean_url, "status": None, "code": None, "note": ""})

    if not entries:
        # Last resort: print some HTML context for debugging
        print("No external links found. Printing first 10 anchor texts:")
        for i, a in enumerate(soup.find_all("a", href=True)[:10]):
            print(f"  {i+1}. Text: '{a.get_text(strip=True)}' -> href: {a.get('href')}")
    else:
        print(f"Found {len(entries)} external links as fallback.")

    return entries


def _extract_country(tag) -> str:
    for parent in tag.parents:
        h3 = parent.find("h3")
        if h3:
            return h3.get_text(strip=True)
    prev = tag.find_previous_sibling("h3")
    if prev:
        return prev.get_text(strip=True)
    return "Unknown"


def is_parked(text: str) -> bool:
    low = text.lower()
    parked_phrases = ["domain for sale", "parked", "this domain is for sale", "buy this domain", "domain name is for sale"]
    return any(phrase in low for phrase in parked_phrases)


def check_with_playwright(url: str, timeout: int) -> Tuple[Optional[int], str, str]:
    if not PLAYWRIGHT_AVAILABLE:
        return None, "PLAYWRIGHT_NOT_INSTALLED", ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=config.HEADERS["User-Agent"])
            page = context.new_page()
            response = page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            status = response.status if response else 0
            title = page.title()
            body_text = page.locator("body").inner_text()
            if is_parked(body_text) or is_parked(title):
                browser.close()
                return status, "PARKED", f"Parked domain (title: {title})"
            browser.close()
            if status < 400:
                return status, "OK", f"Browser-rendered, title: {title}"
            return status, f"HTTP_{status}", f"Playwright fallback, title: {title}"
    except Exception as e:
        return None, f"BROWSER_ERROR: {str(e)[:50]}", ""


def classify_response(entry: Dict, url: str, resp: httpx.Response, final_url: str, title: str, body_text: str) -> Dict:
    original_domain = urlparse(url).netloc
    final_domain = urlparse(final_url).netloc
    code = resp.status_code

    is_redirect = final_domain != original_domain
    redirect_note = ""
    status_label = "OK"

    if is_redirect:
        if final_domain.endswith(".com"):
            status_label = "REDIRECT_MAIN"
            redirect_note = f"Redirected to .com ({final_domain})"
        else:
            status_label = "REDIRECT_OTHER"
            redirect_note = f"Redirected to {final_domain}"

    content_length = len(body_text)
    is_empty = content_length < config.MIN_CONTENT_LENGTH
    if is_empty and status_label == "OK":
        status_label = "EMPTY_PAGE"

    notes = []
    if redirect_note:
        notes.append(redirect_note)
    if title:
        notes.append(f"Title: {title}")
    else:
        notes.append("No title")
    if is_empty:
        notes.append(f"Low content length ({content_length} chars)")

    if title and not any(kw in title.lower() for kw in config.BRAND_KEYWORDS):
        notes.append("Brand mismatch (title doesn't contain 'lingua/learn')")
        if status_label == "OK":
            status_label = "BRAND_MISMATCH"

    final_note = " | ".join(notes) if notes else "No additional info"

    return {**entry, "status": status_label, "code": code, "note": final_note}


def check_url_accurate(entry: Dict[str, Any], client: httpx.Client,
                       args: argparse.Namespace, rate_limiter: RateLimiter) -> Dict[str, Any]:
    url = entry["url"]
    if entry.get("status") == "COMING_SOON":
        return entry

    rate_limiter.wait()

    parsed = urlparse(url)
    urls_to_try = [url]
    if parsed.scheme == "http":
        urls_to_try.append(f"https://{parsed.netloc}{parsed.path}")
    elif parsed.scheme == "https":
        urls_to_try.append(f"http://{parsed.netloc}{parsed.path}")

    last_code = None
    last_label = "ERROR"
    last_note = ""

    for attempt in range(args.retries + 1):
        for try_url in urls_to_try:
            try:
                resp = client.get(try_url, timeout=args.timeout, follow_redirects=True)
                code = resp.status_code
                final_url = str(resp.url)

                if code < 400:
                    content_type = resp.headers.get("content-type", "")
                    if "text/html" in content_type:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        title = soup.title.string.strip() if soup.title else ""
                        body_text = soup.get_text(strip=True)

                        if is_parked(body_text) or is_parked(title):
                            return {**entry, "status": "PARKED", "code": code, "note": "Domain parked / for sale"}

                        return classify_response(entry, try_url, resp, final_url, title, body_text)
                    else:
                        return {**entry, "status": "OK", "code": code, "note": f"Content-Type: {content_type}"}

                last_code = code
                if code == 403:
                    last_label = "FORBIDDEN"
                elif code == 404:
                    last_label = "NOT_FOUND"
                elif code >= 500:
                    last_label = f"SERVER_ERROR_{code}"
                else:
                    last_label = f"CLIENT_ERROR_{code}"
                last_note = f"Status {code}"
                if code >= 500 and attempt < args.retries:
                    time.sleep(args.retry_delay * (attempt + 1))
                    continue
                break

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_label = "TIMEOUT" if isinstance(e, httpx.TimeoutException) else "CONNECTION_ERROR"
                last_note = str(e)[:50]
                if attempt < args.retries:
                    time.sleep(args.retry_delay * (attempt + 1))
                    continue
                break
            except httpx.RequestError as e:
                last_label = "REQUEST_ERROR"
                last_note = str(e)[:50]
                break
        else:
            continue
        break

    if args.use_browser and last_label in ("TIMEOUT", "FORBIDDEN", "CONNECTION_ERROR"):
        browser_status, browser_label, browser_note = check_with_playwright(url, args.timeout)
        if browser_label == "OK":
            return {**entry, "status": "OK", "code": browser_status, "note": browser_note}
        elif browser_label != "PLAYWRIGHT_NOT_INSTALLED":
            return {**entry, "status": browser_label, "code": browser_status, "note": browser_note}

    if args.fallback_path and last_label not in ("OK", "DNS_ERROR", "PARKED"):
        parsed = urlparse(url)
        fallback_url = f"{parsed.scheme}://{parsed.netloc}{args.fallback_path}"
        try:
            fb_resp = client.get(fallback_url, timeout=args.timeout)
            if fb_resp.status_code < 400:
                return {**entry, "status": "OK", "code": fb_resp.status_code, "note": f"resolved via fallback {args.fallback_path}"}
        except httpx.RequestError:
            pass

    return {**entry, "status": last_label, "code": last_code, "note": last_note or entry.get("note", "")}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check franchise links – advanced classification.")
    parser.add_argument("--url", default=config.DEFAULT_URL)
    parser.add_argument("--output", default=None, help="Output CSV filename (timestamped if not given)")
    parser.add_argument("--timeout", type=int, default=config.DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=config.DEFAULT_MAX_WORKERS)
    parser.add_argument("--retries", type=int, default=config.DEFAULT_RETRIES)
    parser.add_argument("--rate-limit", type=float, default=config.DEFAULT_RATE_LIMIT)
    parser.add_argument("--retry-delay", type=float, dest="retry_delay", default=config.DEFAULT_RETRY_DELAY)
    parser.add_argument("--use-browser", action="store_true")
    parser.add_argument("--fallback-path", type=str, default=None)
    args, _ = parser.parse_known_args()
    return args


def send_email(file_path: str) -> None:
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    receiver = os.environ.get("EMAIL_TO")

    if not sender or not password or not receiver:
        print("Email not sent: environment variables missing")
        return

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = f"Franchise Link Report - {datetime.now().strftime('%d %b %Y')}"

    body = "Hello, please find attached the daily franchise link check report (CSV file)."
    msg.attach(MIMEText(body, "plain"))

    try:
        with open(file_path, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(file_path)}")
            msg.attach(part)
    except Exception as e:
        print(f"Failed to attach file: {e}")
        return

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print(f"Email sent to {receiver}")
    except Exception as e:
        print(f"Failed to send email: {e}")


def run() -> None:
    args = parse_args()
    if args.use_browser and not PLAYWRIGHT_AVAILABLE:
        sys.exit("ERROR: --use-browser requested but Playwright not installed.")

    entries = extract_urls(args.url)
    if not entries:
        sys.exit("No 'Visit Website' links found.")

    live = [e for e in entries if e.get("status") != "COMING_SOON"]
    soon = [e for e in entries if e.get("status") == "COMING_SOON"]
    results = list(soon)
    rate_limiter = RateLimiter(args.rate_limit)

    with httpx.Client(http2=True, follow_redirects=True, headers=config.HEADERS,
                      limits=httpx.Limits(max_keepalive_connections=5)) as client:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(check_url_accurate, e, client, args, rate_limiter): e for e in live}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    entry = futures[future]
                    result = {**entry, "status": "UNHANDLED_ERROR", "code": None, "note": str(exc)[:100]}
                results.append(result)

    if args.output:
        output_file = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"{config.DEFAULT_OUTPUT_BASE}_{timestamp}.csv"

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["country", "url", "status", "code", "note"])
        writer.writeheader()
        writer.writerows(results)

    print(f"CSV saved: {output_file}")
    send_email(output_file)


if __name__ == "__main__":
    run()