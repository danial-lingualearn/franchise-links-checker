#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Franchise Links Checker – Dynamic Playwright Extraction + Email Report"""

import csv
import sys
import time
import random
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
from typing import Dict, List, Optional, Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


class Config:
    DEFAULT_URL = "https://lingua-learn.com/franchise/"
    DEFAULT_OUTPUT_BASE = "Franchise_Links_Report"
    DEFAULT_TIMEOUT = 20
    DEFAULT_MAX_WORKERS = 2
    DEFAULT_RETRIES = 2
    DEFAULT_RETRY_DELAY = 3
    DEFAULT_RATE_LIMIT = 0.3
    MIN_CONTENT_LENGTH = 200
    BRAND_KEYWORDS = ["lingua", "learn", "language"]
    BOT_DETECTION_PHRASES = [
        "bot verification", "captcha", "access denied",
        "please verify", "are you human", "robot challenge",
    ]
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]
    # Full browser-like headers (reduces bot-detection on httpx requests)
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


# ---------------------------------------------------------------------------
# Maintenance phrases (from notebook – missing in original main.py)
# ---------------------------------------------------------------------------
MAINTENANCE_PHRASES = [
    "scheduled maintenance",
    "down for maintenance",
    "back online shortly",
    "under maintenance",
    "working to make things better",
    "site is currently down",
    "coming soon",
]


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
            self.last_time = now          # update BEFORE potential sleep
            if self.tokens < 1.0:
                sleep_time = (1.0 - self.tokens) / self.rate
                time.sleep(sleep_time)
                self.tokens = 0.0
                self.last_time = time.monotonic()   # update AFTER sleep too
            else:
                self.tokens -= 1.0
        # Random jitter to avoid bot detection from predictable timing
        time.sleep(random.uniform(1.5, 4.0))


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def is_parked(text: str) -> bool:
    low = text.lower()
    parked_phrases = [
        "domain for sale", "parked", "this domain is for sale",
        "buy this domain", "domain name is for sale",
    ]
    return any(phrase in low for phrase in parked_phrases)


def is_maintenance(text: str) -> bool:
    low = text.lower()
    return any(phrase in low for phrase in MAINTENANCE_PHRASES)


def is_bot_blocked(title: str, body_text: str) -> bool:
    combined = (title + " " + body_text).lower()
    return any(phrase in combined for phrase in config.BOT_DETECTION_PHRASES)


# ---------------------------------------------------------------------------
# Page extraction
# ---------------------------------------------------------------------------

def extract_urls(page_url: str) -> List[Dict[str, Any]]:
    print(f"Loading page with Playwright: {page_url}")
    html = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=config.HEADERS["User-Agent"])
                page = context.new_page()
                page.goto(page_url, timeout=60000, wait_until="domcontentloaded")
                # Scroll to trigger lazy-loaded franchise cards
                for _ in range(3):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(3000)
                try:
                    page.wait_for_selector("a:has-text('Visit Website')", timeout=15000)
                except (TimeoutError, RuntimeError):
                    print("Warning: 'Visit Website' links not found after waiting, but continuing...")
                html = page.content()
            finally:
                browser.close()
    except Exception as e:
        raise RuntimeError(f"Playwright failed to load page: {e}") from e

    soup = BeautifulSoup(html, "html.parser")
    seen: set = set()
    entries = []

    for a in soup.find_all("a", href=True):
        link_text = a.get_text(strip=True).lower()
        if not link_text.endswith("visit website"):
            continue

        href = a["href"].strip()

        # Check for anchor/empty href BEFORE urljoin to avoid resolving "#" into a full URL
        if not href or href == "#":
            country = _extract_country(a)
            # Deduplicate COMING_SOON by country to prevent duplicate rows
            key = f"COMING_SOON::{country}"
            if key not in seen:
                seen.add(key)
                entries.append({
                    "country": country,
                    "url": "",
                    "status": "COMING_SOON",
                    "code": 0,
                    "note": "No live URL yet",
                })
            continue

        full_url = urljoin(page_url, href)

        is_coming_soon = (
            href == page_url or
            not full_url.startswith(("http://", "https://")) or
            "lingua-learn.com/franchise" in full_url
        )

        country = _extract_country(a)

        if is_coming_soon:
            key = f"COMING_SOON::{country}"
            if key not in seen:
                seen.add(key)
                entries.append({
                    "country": country,
                    "url": "",
                    "status": "COMING_SOON",
                    "code": 0,
                    "note": "No live URL yet",
                })
            continue

        parsed = urlparse(full_url)
        clean_url = urlunparse(parsed._replace(fragment=""))
        if clean_url not in seen:
            seen.add(clean_url)
            entries.append({
                "country": country,
                "url": clean_url,
                "status": None,
                "code": None,
                "note": "",
            })

    live_count = sum(1 for e in entries if e.get("status") != "COMING_SOON")
    print(f"Found {len(entries)} franchise entries: {live_count} live, {len(entries) - live_count} coming soon.")
    if live_count == 0:
        print("No live franchise links extracted. Printing first 50 anchors for debugging:")
        for i, a in enumerate(soup.find_all("a", href=True)[:50]):
            text = a.get_text(strip=True)[:50]
            print(f"  {i + 1}. Text: '{text}' -> href: {a.get('href')}")
        raise RuntimeError("No franchise links extracted. Check page structure.")
    return entries


def _extract_country(tag) -> str:
    """Walk up the DOM tree looking for the nearest preceding heading."""
    parent = tag.parent
    while parent:
        h = parent.find_previous_sibling(["h2", "h3", "h4"])
        if h:
            return h.get_text(strip=True)
        parent = parent.parent
    # Fallback: scan whole page for a heading whose siblings include this tag
    soup = tag.find_parent("body") or tag
    for heading in soup.find_all(["h2", "h3", "h4"]):
        if tag in heading.find_next_siblings():
            return heading.get_text(strip=True)
    return "Unknown"


# ---------------------------------------------------------------------------
# Response classification
# ---------------------------------------------------------------------------

def classify_response(
    entry: Dict, url: str, resp: httpx.Response,
    final_url: str, title: str, body_text: str,
) -> Dict:
    """Determine status label and detailed note based on multiple signals."""
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

    # Check maintenance REGARDLESS of content length (not just when content is short)
    if is_maintenance(body_text) or is_maintenance(title):
        status_label = "MAINTENANCE"

    # Content quality check – only override to EMPTY_PAGE if not already flagged
    content_length = len(body_text)
    is_empty = content_length < config.MIN_CONTENT_LENGTH
    if is_empty and status_label == "OK":
        status_label = "EMPTY_PAGE"

    # Build note
    notes = []
    if redirect_note:
        notes.append(redirect_note)
    if title:
        notes.append(f"Title: {title}")
    else:
        notes.append("No title")
    if is_empty:
        if status_label == "MAINTENANCE":
            notes.append("Maintenance page (site temporarily down)")
        else:
            notes.append(f"Low content length ({content_length} chars)")

    # Brand mismatch check
    if title and not any(kw in title.lower() for kw in config.BRAND_KEYWORDS):
        notes.append("Brand mismatch (title doesn't contain 'lingua/learn')")
        if status_label == "OK":
            status_label = "BRAND_MISMATCH"

    final_note = " | ".join(notes) if notes else "No additional info"
    return {**entry, "status": status_label, "code": code, "note": final_note}


# ---------------------------------------------------------------------------
# Playwright fallback checker
# ---------------------------------------------------------------------------

def check_with_playwright(url: str, timeout: int):
    try:
        with sync_playwright() as p:
            ua = random.choice(config.USER_AGENTS)
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=ua,
                    locale="en-US",
                    timezone_id="America/New_York",
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()
                response = page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass  # heavy pages may never reach networkidle; proceed anyway
                page.wait_for_timeout(2000)
                status = response.status if response else 0
                # Retry title/body extraction once – a JS redirect after networkidle
                # can destroy the execution context between wait and title().
                try:
                    title = page.title()
                    body_text = page.locator("body").inner_text()
                except Exception:
                    page.wait_for_timeout(2000)
                    title = page.title()
                    body_text = page.locator("body").inner_text()

                if is_parked(body_text) or is_parked(title):
                    return status, "PARKED", f"Parked domain | Title: {title}"
                if is_bot_blocked(title, body_text):
                    return status, "BOT_BLOCKED", f"Bot/CAPTCHA page detected | Title: {title}"
                if is_maintenance(body_text) or is_maintenance(title):
                    return status, "MAINTENANCE", f"Maintenance page | Title: {title}"
                if status < 400 and len(body_text.strip()) >= config.MIN_CONTENT_LENGTH:
                    return status, "OK", f"Browser-rendered | Title: {title}"
                if status < 400:
                    return status, "EMPTY_PAGE", f"Browser-rendered but empty | Title: {title}"
                return status, f"HTTP_{status}", f"Playwright fallback | Title: {title}"
            finally:
                browser.close()
    except Exception as e:
        return None, "BROWSER_ERROR", str(e)[:80]


# ---------------------------------------------------------------------------
# Per-URL checker
# ---------------------------------------------------------------------------

def check_url_accurate(
    entry: Dict[str, Any],
    client: httpx.Client,
    args: argparse.Namespace,
    rate_limiter: RateLimiter,
) -> Dict[str, Any]:
    url = entry["url"]
    if entry.get("status") == "COMING_SOON":
        return entry

    rate_limiter.wait()

    # Rotate User-Agent per request
    rotated_headers = {**config.HEADERS, "User-Agent": random.choice(config.USER_AGENTS)}

    parsed = urlparse(url)
    netloc = parsed.netloc
    path_qs = parsed.path + ("?" + parsed.query if parsed.query else "")

    has_www = netloc.lower().startswith("www.")
    netloc_no_www = netloc[4:] if has_www else netloc

    # Build URL variants to try: scheme flip + www-strip
    urls_to_try = [url]
    if parsed.scheme == "http":
        urls_to_try.append(f"https://{netloc}{path_qs}")
    elif parsed.scheme == "https":
        urls_to_try.append(f"http://{netloc}{path_qs}")
    if has_www:
        urls_to_try.append(f"{parsed.scheme}://{netloc_no_www}{path_qs}")
        alt_scheme = "http" if parsed.scheme == "https" else "https"
        urls_to_try.append(f"{alt_scheme}://{netloc_no_www}{path_qs}")

    last_code = None
    last_label = "ERROR"
    last_note = ""

    # success_result flag pattern – avoids ambiguous break/continue in nested loops
    success_result: Optional[Dict] = None
    should_retry = False

    for attempt in range(args.retries + 1):
        if success_result is not None:
            break

        for try_url in urls_to_try:
            try:
                resp = client.get(try_url, timeout=args.timeout,
                                  headers=rotated_headers, follow_redirects=True)
                code = resp.status_code
                final_url = str(resp.url)

                if code < 400:
                    content_type = resp.headers.get("content-type", "")
                    # Correct www_stripped detection: compare parsed netlocs
                    try_netloc = urlparse(try_url).netloc
                    www_stripped = has_www and try_netloc == netloc_no_www

                    if "text/html" in content_type:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        title = soup.title.get_text(strip=True) if soup.title else ""
                        body_text = soup.get_text(strip=True)

                        if is_parked(body_text) or is_parked(title):
                            success_result = {**entry, "status": "PARKED", "code": code, "note": "Domain parked / for sale"}
                            break

                        if is_bot_blocked(title, body_text):
                            pw_code, pw_label, pw_note = check_with_playwright(url, args.timeout)
                            success_result = {**entry, "status": pw_label, "code": pw_code, "note": pw_note}
                            break

                        result = classify_response(entry, try_url, resp, final_url, title, body_text)
                        if www_stripped:
                            result["note"] = "[www. removed] " + result.get("note", "")

                        # If httpx sees EMPTY_PAGE, let Playwright have a second look
                        if result.get("status") == "EMPTY_PAGE":
                            pw_code, pw_label, pw_note = check_with_playwright(url, args.timeout)
                            if pw_label == "OK":
                                success_result = {**entry, "status": "OK", "code": pw_code, "note": pw_note}
                                break

                        success_result = result
                        break
                    else:
                        note = f"Content-Type: {content_type}"
                        if www_stripped:
                            note = "[www. removed] " + note
                        success_result = {**entry, "status": "OK", "code": code, "note": note}
                        break

                # --- non-2xx ---
                last_code = code
                if code == 403:
                    last_label = "FORBIDDEN"
                    # Playwright immediately on 403 – likely bot protection
                    pw_code, pw_label, pw_note = check_with_playwright(url, args.timeout)
                    if pw_label not in ("BROWSER_ERROR", "BOT_BLOCKED"):
                        success_result = {**entry, "status": pw_label, "code": pw_code, "note": pw_note}
                        break
                    last_note = f"Status 403 | Playwright: {pw_note}"
                elif code == 404:
                    last_label = "NOT_FOUND"
                elif code >= 500:
                    last_label = f"SERVER_ERROR_{code}"
                else:
                    last_label = f"CLIENT_ERROR_{code}"
                last_note = f"Status {code}"

                if code >= 500 and attempt < args.retries:
                    should_retry = True
                    break

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_label = "TIMEOUT" if isinstance(e, httpx.TimeoutException) else "CONNECTION_ERROR"
                last_note = str(e)[:50]
                if attempt < args.retries:
                    should_retry = True
                    break
                # exhausted retries – try next URL variant

            except httpx.RequestError as e:
                last_label = "REQUEST_ERROR"
                last_note = str(e)[:50]
                # don't retry on generic request errors, move to next variant

        if should_retry:
            should_retry = False
            time.sleep(args.retry_delay * (attempt + 1))
            continue  # retry outer loop

    if success_result is not None:
        return success_result

    # Optional Playwright fallback for persistent failures
    if args.use_browser and last_label in ("TIMEOUT", "FORBIDDEN", "CONNECTION_ERROR"):
        pw_code, pw_label, pw_note = check_with_playwright(url, args.timeout)
        if pw_label not in ("BROWSER_ERROR", "PLAYWRIGHT_NOT_INSTALLED"):
            return {**entry, "status": pw_label, "code": pw_code, "note": pw_note}

    # Optional fallback path probe
    if args.fallback_path and last_label not in ("OK", "PARKED"):
        fallback_url = f"{parsed.scheme}://{parsed.netloc}{args.fallback_path}"
        try:
            fb_resp = client.get(fallback_url, timeout=args.timeout)
            if fb_resp.status_code < 400:
                return {**entry, "status": "OK", "code": fb_resp.status_code,
                        "note": f"resolved via fallback {args.fallback_path}"}
        except httpx.RequestError:
            pass

    return {**entry, "status": last_label, "code": last_code, "note": last_note or entry.get("note", "")}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive integer")
    return ivalue


def non_negative_float(value: str) -> float:
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"{value} is not a non-negative number")
    return fvalue


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check franchise links – advanced classification.")
    parser.add_argument("--url", default=config.DEFAULT_URL)
    parser.add_argument("--output", default=None, help="Output CSV filename (timestamped if not given)")
    parser.add_argument("--timeout", type=positive_int, default=config.DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=positive_int, default=config.DEFAULT_MAX_WORKERS)
    parser.add_argument("--retries", type=non_negative_float, default=config.DEFAULT_RETRIES)
    parser.add_argument("--rate-limit", type=non_negative_float, default=config.DEFAULT_RATE_LIMIT)
    parser.add_argument("--retry-delay", type=non_negative_float, dest="retry_delay", default=config.DEFAULT_RETRY_DELAY)
    parser.add_argument("--use-browser", action="store_true",
                        help="Use Playwright as final fallback for TIMEOUT/FORBIDDEN/CONNECTION_ERROR")
    parser.add_argument("--fallback-path", type=str, default=None,
                        help="Probe this path on the origin if all attempts fail (e.g. /en/)")
    args, _ = parser.parse_known_args()
    return args


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

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
    msg["Subject"] = f"Franchise Links Report - {datetime.now().strftime('%d %b %Y')}"
    msg.attach(MIMEText("Please find attached the daily franchise links check report (CSV).", "plain"))

    try:
        with open(file_path, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(file_path)}")
            msg.attach(part)
    except (OSError, IOError) as e:
        print(f"Failed to attach file: {e}")
        return

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print(f"Email sent to {receiver}")
    except (smtplib.SMTPException, ConnectionError, TimeoutError) as e:
        print(f"Failed to send email: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    args = parse_args()

    entries = extract_urls(args.url)
    if not entries:
        sys.exit("No 'Visit Website' links found.")

    live = [e for e in entries if e.get("status") != "COMING_SOON"]
    soon = [e for e in entries if e.get("status") == "COMING_SOON"]
    results = list(soon)
    rate_limiter = RateLimiter(args.rate_limit)

    # Shared httpx client with HTTP/2 and connection pooling
    with httpx.Client(
        http2=True,
        follow_redirects=True,
        headers=config.HEADERS,
        limits=httpx.Limits(max_keepalive_connections=5),
    ) as client:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(check_url_accurate, e, client, args, rate_limiter): e
                for e in live
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                except (RuntimeError, OSError, ValueError) as exc:
                    entry = futures[future]
                    result = {**entry, "status": "UNHANDLED_ERROR", "code": 0, "note": str(exc)[:100]}
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
