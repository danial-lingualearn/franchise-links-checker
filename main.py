#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Franchise Broken Links Checker – Playwright‑based + Email Report"""

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
from playwright.sync_api import sync_playwright

class Config:
    DEFAULT_URL = "https://lingua-learn.com/franchise/"
    DEFAULT_OUTPUT_BASE = "Franchise_Broken_Links_Report"
    DEFAULT_TIMEOUT = 15
    DEFAULT_MAX_WORKERS = 5
    DEFAULT_RETRIES = 2
    DEFAULT_RETRY_DELAY = 2
    DEFAULT_RATE_LIMIT = 0.5
    MIN_CONTENT_LENGTH = 200
    BRAND_KEYWORDS = ["lingua", "learn", "language"]
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

config = Config()
PLAYWRIGHT_AVAILABLE = True

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
    print(f"Loading page with Playwright: {page_url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=config.HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(page_url, timeout=30000, wait_until="domcontentloaded")
            
            # Scroll to bottom to trigger lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(3000)
            
            # Wait for any element containing "Visit Website" to appear
            try:
                page.wait_for_selector("a:has-text('Visit Website')", timeout=10000)
            except Exception:
                print("Timeout waiting for 'Visit Website' links, but continuing...")
            
            # Get the full HTML after scrolling
            html = page.content()
            browser.close()
    except Exception as e:
        sys.exit(f"Playwright failed to load page: {e}")

    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    entries = []

    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() != "visit website":
            continue
        href = a["href"].strip()
        full_url = urljoin(page_url, href)
        if not full_url.startswith(("http://", "https://")):
            continue
        if "lingua-learn.com" in full_url:
            continue

        # Find country from the nearest preceding <h3>
        country = "Unknown"
        parent = a.parent
        while parent:
            h3 = parent.find_previous_sibling("h3")
            if h3:
                country = h3.get_text(strip=True)
                break
            parent = parent.parent
        if country == "Unknown":
            # Fallback: look for any <h3> that is near this anchor
            for h3 in soup.find_all("h3"):
                if a in h3.find_next_siblings():
                    country = h3.get_text(strip=True)
                    break

        parsed = urlparse(full_url)
        clean_url = urlunparse(parsed._replace(fragment=""))
        if clean_url not in seen:
            seen.add(clean_url)
            entries.append({
                "country": country,
                "url": clean_url,
                "status": None,
                "code": None,
                "note": ""
            })

    print(f"Found {len(entries)} franchise links.")
    if not entries:
        # Print more of the page to debug
        print("No 'Visit Website' links found. Printing all anchor texts (first 50):")
        for i, a in enumerate(soup.find_all("a", href=True)[:50]):
            text = a.get_text(strip=True)[:50]
            print(f"  {i+1}. Text: '{text}' -> href: {a.get('href')}")
        sys.exit("No franchise links extracted. Check the page structure.")
    return entries

def is_parked(text: str) -> bool:
    low = text.lower()
    parked_phrases = ["domain for sale", "parked", "this domain is for sale", "buy this domain"]
    return any(phrase in low for phrase in parked_phrases)

def classify_response(entry: Dict, url: str, resp: httpx.Response, final_url: str, title: str, body_text: str) -> Dict:
    original_domain = urlparse(url).netloc
    final_domain = urlparse(final_url).netloc
    code = resp.status_code

    is_redirect = final_domain != original_domain
    status_label = "OK"
    notes = []

    if is_redirect:
        if final_domain.endswith(".com"):
            status_label = "REDIRECT_MAIN"
            notes.append(f"Redirected to .com ({final_domain})")
        else:
            status_label = "REDIRECT_OTHER"
            notes.append(f"Redirected to {final_domain}")

    content_length = len(body_text)
    if content_length < config.MIN_CONTENT_LENGTH and status_label == "OK":
        status_label = "EMPTY_PAGE"
        notes.append(f"Low content length ({content_length} chars)")

    if title:
        notes.append(f"Title: {title}")
    else:
        notes.append("No title")

    if title and not any(kw in title.lower() for kw in config.BRAND_KEYWORDS):
        notes.append("Brand mismatch (title doesn't contain 'lingua/learn')")
        if status_label == "OK":
            status_label = "BRAND_MISMATCH"

    final_note = " | ".join(notes) if notes else "No additional info"
    return {**entry, "status": status_label, "code": code, "note": final_note}

def check_url_accurate(entry: Dict[str, Any], client: httpx.Client,
                       args: argparse.Namespace, rate_limiter: RateLimiter) -> Dict[str, Any]:
    url = entry["url"]
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

    return {**entry, "status": last_label, "code": last_code, "note": last_note or entry.get("note", "")}

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check franchise links – advanced classification.")
    parser.add_argument("--url", default=config.DEFAULT_URL)
    parser.add_argument("--output", default=None)
    parser.add_argument("--timeout", type=int, default=config.DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=config.DEFAULT_MAX_WORKERS)
    parser.add_argument("--retries", type=int, default=config.DEFAULT_RETRIES)
    parser.add_argument("--rate-limit", type=float, default=config.DEFAULT_RATE_LIMIT)
    parser.add_argument("--retry-delay", type=float, dest="retry_delay", default=config.DEFAULT_RETRY_DELAY)
    args, _ = parser.parse_known_args()
    return args

def send_email(file_path: str) -> None:
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    receiver = os.environ.get("EMAIL_TO")

    if not sender or not password or not receiver:
        print("Email not sent: environment variables missing.")
        return

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = f"Franchise Link Report - {datetime.now().strftime('%d %b %Y')}"

    body = "Please find attached the daily franchise link check report (CSV)."
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
    entries = extract_urls(args.url)

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