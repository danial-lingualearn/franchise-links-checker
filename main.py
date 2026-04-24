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
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# Config

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

MAINTENANCE_PHRASES = [
    "scheduled maintenance",
    "down for maintenance",
    "back online shortly",
    "under maintenance",
    "working to make things better",
    "site is currently down",
    "coming soon",
]


# Rate limiter

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
                self.last_time = time.monotonic()
            else:
                self.tokens -= 1.0
        time.sleep(random.uniform(1.5, 4.0))


# Detection helpers

def is_parked(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in [
        "domain for sale", "parked", "this domain is for sale",
        "buy this domain", "domain name is for sale",
    ])


def is_maintenance(text: str) -> bool:
    return any(p in text.lower() for p in MAINTENANCE_PHRASES)


def is_bot_blocked(title: str, body_text: str) -> bool:
    combined = (title + " " + body_text).lower()
    return any(p in combined for p in config.BOT_DETECTION_PHRASES)


# Page extraction  (Playwright, no COMING_SOON dedup — notebook behaviour)

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
                for _ in range(3):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(3000)
                try:
                    page.wait_for_selector("a:has-text('Visit Website')", timeout=15000)
                except Exception:
                    print("Warning: 'Visit Website' links not found after waiting, continuing...")
                html = page.content()
            finally:
                browser.close()
    except Exception as e:
        raise RuntimeError(f"Playwright failed to load page: {e}") from e

    soup = BeautifulSoup(html, "html.parser")
    seen: set = set()
    entries = []

    for a in soup.find_all("a", href=True):
        if not a.get_text(strip=True).lower().endswith("visit website"):
            continue

        href = a["href"].strip()
        country = _extract_country(a)

        # Bare anchor → COMING_SOON, intentionally no dedup (notebook behaviour)
        if not href or href == "#":
            entries.append({
                "country": country,
                "url": "#",
                "status": "COMING_SOON",
                "code": None,
                "note": "COMING_SOON",
            })
            continue

        full_url = urljoin(page_url, href)

        # Internal / self link → also COMING_SOON, no dedup
        if (
            href == page_url
            or not full_url.startswith(("http://", "https://"))
            or "lingua-learn.com/franchise" in full_url
        ):
            entries.append({
                "country": country,
                "url": "#",
                "status": "COMING_SOON",
                "code": None,
                "note": "COMING_SOON",
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
    print(f"Found {len(entries)} entries: {live_count} live, {len(entries) - live_count} coming soon.")
    if live_count == 0:
        raise RuntimeError("No live franchise links extracted. Check page structure.")
    return entries


def _extract_country(tag) -> str:
    parent = tag.parent
    while parent:
        h = parent.find_previous_sibling(["h2", "h3", "h4"])
        if h:
            return h.get_text(strip=True)
        parent = parent.parent
    soup = tag.find_parent("body") or tag
    for heading in soup.find_all(["h2", "h3", "h4"]):
        if tag in heading.find_next_siblings():
            return heading.get_text(strip=True)
    return "Unknown"


# Response classification  (notebook-style: always use original url,
#                           so www→canonical counts as REDIRECT_OTHER)

def classify_response(
    entry: Dict,
    original_url: str,
    resp: httpx.Response,
    final_url: str,
    title: str,
    body_text: str,
) -> Dict:
    original_domain = urlparse(original_url).netloc
    final_domain = urlparse(final_url).netloc
    code = resp.status_code

    is_redirect = final_domain != original_domain
    redirect_note = ""
    status_label = "OK"

    # Check maintenance FIRST (highest priority)
    if is_maintenance(body_text) or is_maintenance(title):
        status_label = "MAINTENANCE"
    # Then check for redirect
    elif is_redirect:
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
    notes.append(f"Title: {title}" if title else "No title")
    if is_empty:
        notes.append(
            "Maintenance page (site temporarily down)"
            if status_label == "MAINTENANCE"
            else f"Low content length ({content_length} chars)"
        )

    if title and not any(kw in title.lower() for kw in config.BRAND_KEYWORDS):
        notes.append("Brand mismatch (title doesn't contain 'lingua/learn')")
        if status_label == "OK":
            status_label = "BRAND_MISMATCH"

    return {
        **entry,
        "status": status_label,
        "code": code,
        "note": " | ".join(notes) or "No additional info",
    }


# Playwright fallback

def check_with_playwright(url: str, timeout: int) -> Tuple[Optional[int], str, str, str]:
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
                    pass
                page.wait_for_timeout(2000)
                status = response.status if response else 0
                try:
                    title = page.title()
                    body_text = page.locator("body").inner_text()
                except Exception:
                    page.wait_for_timeout(2000)
                    title = page.title()
                    body_text = page.locator("body").inner_text()

                if is_parked(body_text) or is_parked(title):
                    return status, "PARKED", title, body_text
                if is_bot_blocked(title, body_text):
                    return status, "BOT_BLOCKED", title, body_text
                if is_maintenance(body_text) or is_maintenance(title):
                    return status, "MAINTENANCE", title, body_text
                if status < 400 and len(body_text.strip()) >= config.MIN_CONTENT_LENGTH:
                    return status, "OK", title, body_text
                if status < 400:
                    return status, "EMPTY_PAGE", title, body_text
                return status, f"HTTP_{status}", title, body_text
            finally:
                browser.close()
    except Exception as e:
        return None, "BROWSER_ERROR", "", str(e)[:80]


# Per-URL checker  (notebook-style: 429 → redirect label, not CLIENT_ERROR)

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

    rotated_headers = {**config.HEADERS, "User-Agent": random.choice(config.USER_AGENTS)}

    parsed = urlparse(url)
    netloc = parsed.netloc
    path_qs = parsed.path + ("?" + parsed.query if parsed.query else "")
    has_www = netloc.lower().startswith("www.")
    netloc_no_www = netloc[4:] if has_www else netloc

    urls_to_try = [url]
    if parsed.scheme == "http":
        urls_to_try.append(f"https://{netloc}{path_qs}")
    elif parsed.scheme == "https":
        urls_to_try.append(f"http://{netloc}{path_qs}")
    if has_www:
        urls_to_try.append(f"{parsed.scheme}://{netloc_no_www}{path_qs}")
        alt_scheme = "http" if parsed.scheme == "https" else "https"
        urls_to_try.append(f"{alt_scheme}://{netloc_no_www}{path_qs}")

    last_code: Optional[int] = None
    last_label = "ERROR"
    last_note = ""
    success_result: Optional[Dict] = None
    should_retry = False

    for attempt in range(args.retries + 1):
        if success_result is not None:
            break

        for try_url in urls_to_try:
            try:
                resp = client.get(
                    try_url, timeout=args.timeout,
                    headers=rotated_headers, follow_redirects=True,
                )
                code = resp.status_code
                final_url = str(resp.url)

                if code < 400:
                    content_type = resp.headers.get("content-type", "")
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
                            pw_code, pw_label, pw_title, pw_body = check_with_playwright(url, args.timeout)
                            pw_note = f"Bot/CAPTCHA detected | Title: {pw_title}"
                            success_result = {**entry, "status": pw_label, "code": pw_code, "note": pw_note}
                            break

                        # Pass original url so www→canonical = REDIRECT_OTHER
                        result = classify_response(entry, url, resp, final_url, title, body_text)
                        if www_stripped:
                            result["note"] = "[www. removed] " + result.get("note", "")

                        if result.get("status") == "EMPTY_PAGE":
                            pw_code, pw_label, pw_title, pw_body = check_with_playwright(url, args.timeout)
                            if pw_label == "OK":
                                success_result = {**entry, "status": "OK", "code": pw_code, "note": f"Browser-rendered | Title: {pw_title}"}
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

                if code == 429:
                    # Notebook behaviour: treat 429 as a soft block, resolve to redirect label
                    final_url_429 = str(resp.url)
                    orig_domain = urlparse(url).netloc
                    location = resp.headers.get("location", "")
                    loc_domain = urlparse(location).netloc if location else ""
                    final_domain_429 = urlparse(final_url_429).netloc
                    redirect_domain = loc_domain or final_domain_429

                    if redirect_domain and redirect_domain != orig_domain:
                        if redirect_domain.endswith(".com"):
                            last_label = "REDIRECT_MAIN"
                            last_note = f"Redirected to .com ({redirect_domain})"
                        else:
                            last_label = "REDIRECT_OTHER"
                            last_note = f"Redirected to {redirect_domain}"
                    else:
                        last_label = "REDIRECT_OTHER"
                        last_note = f"Rate limited, final URL: {final_url_429}"
                    break  # no retry on 429

                elif code == 403:
                    last_label = "FORBIDDEN"
                    pw_code, pw_label, pw_title, pw_body = check_with_playwright(url, args.timeout)
                    if pw_label not in ("BROWSER_ERROR", "BOT_BLOCKED"):
                        if pw_code and pw_code < 400:
                            # Run Playwright result through classify_response for consistent title/brand/maintenance checks
                            class MockResponse:
                                def __init__(self, status_code):
                                    self.status_code = status_code
                                    self.url = url
                                    self.headers = {}
                            mock_resp = MockResponse(pw_code)
                            result = classify_response(entry, url, mock_resp, url, pw_title, pw_body)
                            success_result = result
                        else:
                            pw_note = f"Status {pw_code} | Title: {pw_title}"
                            success_result = {**entry, "status": pw_label, "code": pw_code, "note": pw_note}
                        break
                    pw_note = f"Status 403 | Playwright: Browser error or blocked"
                    last_note = pw_note
                elif code == 404:
                    last_label = "NOT_FOUND"
                    last_note = f"Status {code}"
                elif code >= 500:
                    last_label = f"SERVER_ERROR_{code}"
                    last_note = f"Status {code}"
                    if attempt < args.retries:
                        should_retry = True
                        break
                else:
                    last_label = f"CLIENT_ERROR_{code}"
                    last_note = f"Status {code}"

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_label = "TIMEOUT" if isinstance(e, httpx.TimeoutException) else "CONNECTION_ERROR"
                last_note = str(e)[:50]
                if attempt < args.retries:
                    should_retry = True
                    break

            except httpx.RequestError as e:
                last_label = "REQUEST_ERROR"
                last_note = str(e)[:50]

        if should_retry:
            should_retry = False
            time.sleep(args.retry_delay * (attempt + 1))
            continue

    if success_result is not None:
        return success_result

    if args.use_browser and last_label in ("TIMEOUT", "FORBIDDEN", "CONNECTION_ERROR"):
        pw_code, pw_label, pw_title, pw_body = check_with_playwright(url, args.timeout)
        if pw_label not in ("BROWSER_ERROR", "PLAYWRIGHT_NOT_INSTALLED"):
            pw_note = f"Browser-rendered | Title: {pw_title}"
            return {**entry, "status": pw_label, "code": pw_code, "note": pw_note}

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


# Argument parsing

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
    parser.add_argument("--output", default=None)
    parser.add_argument("--timeout", type=positive_int, default=config.DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=positive_int, default=config.DEFAULT_MAX_WORKERS)
    parser.add_argument("--retries", type=non_negative_float, default=config.DEFAULT_RETRIES)
    parser.add_argument("--rate-limit", type=non_negative_float, default=config.DEFAULT_RATE_LIMIT)
    parser.add_argument("--retry-delay", type=non_negative_float, dest="retry_delay", default=config.DEFAULT_RETRY_DELAY)
    parser.add_argument("--use-browser", action="store_true")
    parser.add_argument("--fallback-path", type=str, default=None)
    args, _ = parser.parse_known_args()
    return args


# Email delivery

def send_email(file_path: str) -> None:
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    receiver = os.environ.get("EMAIL_TO")
    if not sender or not password or not receiver:
        print("Email not sent: EMAIL_USER / EMAIL_PASS / EMAIL_TO not set.")
        return

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = f"Franchise Links Report - {datetime.now().strftime('%d %b %Y')}"
    msg.attach(MIMEText("Please find attached the daily franchise links check report (CSV).", "plain"))

    try:
        with open(file_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(file_path)}")
            msg.attach(part)
    except OSError as e:
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
    except Exception as e:
        print(f"Failed to send email: {e}")


# Entry point

def run() -> None:
    args = parse_args()

    entries = extract_urls(args.url)
    if not entries:
        sys.exit("No 'Visit Website' links found.")

    live = [e for e in entries if e.get("status") != "COMING_SOON"]
    soon = [e for e in entries if e.get("status") == "COMING_SOON"]
    results = list(soon)
    rate_limiter = RateLimiter(args.rate_limit)

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
                except Exception as exc:
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
