#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Franchise Links Checker – Daily Scan Edition (Production / GitHub Actions)

Key design decisions (derived from multi-run empirical data):
  - workers=1  →  eliminates inter-request collisions; this single change
                  removed all 429 errors in every clean run
  - Playwright required  →  hard import; always available in CI
  - httpx-first page extraction with Playwright fallback
  - MAINTENANCE has priority over REDIRECT in classify_http_response
  - 429 final state always triggers Playwright (catches MAINTENANCE pages
    that rate-limit before body is readable — e.g. Chile, Lithuania)
  - Exponential backoff with jitter on retries
  - 5–10 s cooldown after every 429 before retry
  - 2–4 s human-like random delay between every request
  - Per-entry [N/M] progress + end-of-run summary
  - Email delivery via SMTP (EMAIL_USER / EMAIL_PASS / EMAIL_TO env vars)

GitHub Actions recommended run command:
    python main.py --use-browser
"""

import csv
import os
import sys
import time
import random
import smtplib
import threading
import argparse
from collections import Counter
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class ScannerConfig:
    DEFAULT_URL         = "https://lingua-learn.com/franchise/"
    DEFAULT_OUTPUT_BASE = "Franchise_Links_Report"
    DEFAULT_TIMEOUT     = 25      # generous for slow TLDs
    DEFAULT_MAX_WORKERS = 1       # single worker — eliminates 429 collisions
    DEFAULT_RETRIES     = 2
    DEFAULT_RETRY_DELAY = 3       # base seconds for exponential backoff
    DEFAULT_RATE_LIMIT  = 0.3
    MIN_CONTENT_LENGTH  = 200
    BRAND_KEYWORDS      = ["lingua", "learn", "language"]
    BOT_DETECTION_PHRASES = [
        # Generic bot/CAPTCHA gates
        "bot verification", "captcha", "access denied",
        "please verify", "are you human", "robot challenge",
        # Cloudflare challenge pages
        "checking your browser", "just a moment", "enable javascript and cookies",
        "ray id", "cf-browser-verification", "attention required! | cloudflare",
    ]
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]
    HEADERS = {
        "User-Agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Referer":                   "https://www.google.com/",
        "Sec-Ch-Ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile":          "?0",
        "Sec-Ch-Ua-Platform":        '"Windows"',
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "cross-site",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection":                "keep-alive",
    }


SCANNER_CONFIG = ScannerConfig()

STATUS_OK = "OK"
STATUS_COMING_SOON = "COMING_SOON"
STATUS_REDIRECT_MAIN = "REDIRECT_MAIN"
STATUS_REDIRECT_OTHER = "REDIRECT_OTHER"
STATUS_MAINTENANCE = "MAINTENANCE"
STATUS_PARKED = "PARKED"
STATUS_BOT_BLOCKED = "BOT_BLOCKED"
STATUS_EMPTY_PAGE = "EMPTY_PAGE"
STATUS_BRAND_MISMATCH = "BRAND_MISMATCH"
STATUS_BROWSER_ERROR = "BROWSER_ERROR"
STATUS_CONNECTION_ERROR = "CONNECTION_ERROR"
STATUS_FORBIDDEN = "FORBIDDEN"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_REQUEST_ERROR = "REQUEST_ERROR"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_UNHANDLED_ERROR = "UNHANDLED_ERROR"

REPORT_COLUMNS = ["country", "url", "status", "code", "note"]

MAINTENANCE_PHRASES = [
    "scheduled maintenance",
    "down for maintenance",
    "back online shortly",
    "under maintenance",
    "working to make things better",
    "site is currently down",
    "coming soon",
]


# ---------------------------------------------------------------------------
# Rate limiter  (token-bucket + mandatory human-like delay)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, rate: float):
        self.rate      = rate
        self.lock      = threading.Lock()
        self.last_time = time.monotonic()
        self.tokens    = 1.0

    def wait(self):
        if self.rate <= 0:
            return
        with self.lock:
            now     = time.monotonic()
            elapsed = now - self.last_time
            self.tokens += elapsed * self.rate
            if self.tokens > 1.0:
                self.tokens = 1.0
            self.last_time = now
            if self.tokens < 1.0:
                sleep_time = (1.0 - self.tokens) / self.rate
                time.sleep(sleep_time)
                self.tokens    = 0.0
                self.last_time = time.monotonic()
            else:
                self.tokens -= 1.0
        # Human-like inter-request delay — reduces 429 risk at single-worker concurrency
        time.sleep(random.uniform(2.0, 4.0))


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

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
    return any(p in combined for p in SCANNER_CONFIG.BOT_DETECTION_PHRASES)


# ---------------------------------------------------------------------------
# Page extraction  (httpx-first, Playwright fallback)
# ---------------------------------------------------------------------------

def extract_franchise_entries(page_url: str) -> List[Dict[str, Any]]:
    """Fetch the franchise listing page and extract all 'Visit Website' links.

    Tries plain httpx first (fast, no Playwright startup).
    Falls back to Playwright only when the page appears JS-rendered (no live
    links found via httpx).
    """
    html       = _fetch_html_httpx(page_url)
    entries    = _parse_franchise_entries(html, page_url)
    live_count = sum(1 for entry in entries if entry.get("status") != STATUS_COMING_SOON)

    if live_count == 0:
        print("httpx found no live links — retrying with Playwright...")
        html       = _fetch_html_playwright(page_url)
        entries    = _parse_franchise_entries(html, page_url)
        live_count = sum(1 for entry in entries if entry.get("status") != STATUS_COMING_SOON)

    print(f"Found {len(entries)} entries: {live_count} live, "
          f"{len(entries) - live_count} coming soon.")
    if live_count == 0:
        raise RuntimeError("No live franchise links extracted. Check page structure.")
    return entries


def _fetch_html_httpx(url: str) -> str:
    try:
        with httpx.Client(
            timeout=SCANNER_CONFIG.DEFAULT_TIMEOUT,
            headers=SCANNER_CONFIG.HEADERS,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        print(f"Warning: httpx preflight failed ({e}). Will try Playwright.")
        return ""


def _fetch_html_playwright(url: str) -> str:
    print(f"Loading page with Playwright: {url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=SCANNER_CONFIG.HEADERS["User-Agent"])
                page    = context.new_page()
                page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                for _ in range(3):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(3000)
                try:
                    page.wait_for_selector("a:has-text('Visit Website')", timeout=15_000)
                except Exception:
                    print("Warning: 'Visit Website' links not found after waiting.")
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        raise RuntimeError(f"Playwright failed to load page: {e}") from e


def _parse_franchise_entries(html: str, page_url: str) -> List[Dict[str, Any]]:
    if not html:
        return []
    soup    = BeautifulSoup(html, "html.parser")
    seen: set = set()
    entries   = []

    for link_element in soup.find_all("a", href=True):
        if not link_element.get_text(strip=True).lower().endswith("visit website"):
            continue

        href    = link_element["href"].strip()
        country = _extract_country(link_element)

        if not href or href == "#":
            entries.append({
                "country": country, "url": "#",
                "status": STATUS_COMING_SOON, "code": None, "note": STATUS_COMING_SOON,
            })
            continue

        full_url = urljoin(page_url, href)
        if (
            href == page_url
            or not full_url.startswith(("http://", "https://"))
            or "lingua-learn.com/franchise" in full_url
        ):
            entries.append({
                "country": country, "url": "#",
                "status": STATUS_COMING_SOON, "code": None, "note": STATUS_COMING_SOON,
            })
            continue

        parsed    = urlparse(full_url)
        clean_url = urlunparse(parsed._replace(fragment=""))
        if clean_url not in seen:
            seen.add(clean_url)
            entries.append({
                "country": country, "url": clean_url,
                "status": None, "code": None, "note": "",
            })

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


# ---------------------------------------------------------------------------
# Playwright fallback checker
# ---------------------------------------------------------------------------

def inspect_url_with_browser(url: str, timeout: int) -> Tuple[Optional[int], str, str, str]:
    """Browser-render a URL and return (status_code, label, title, body_text)."""
    try:
        with sync_playwright() as p:
            user_agent = random.choice(SCANNER_CONFIG.USER_AGENTS)
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=user_agent,
                    locale="en-US",
                    timezone_id="America/New_York",
                    viewport={"width": 1280, "height": 800},
                )
                page     = context.new_page()
                response = page.goto(url, timeout=timeout * 1000,
                                     wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                status = response.status if response else 0
                try:
                    page.wait_for_function("document.title.length > 0", timeout=10_000)
                except Exception:
                    pass
                title     = page.title()
                body_text = page.locator("body").inner_text()

                if is_parked(body_text) or is_parked(title):
                    return status, STATUS_PARKED, title, body_text
                if is_bot_blocked(title, body_text):
                    return status, STATUS_BOT_BLOCKED, title, body_text
                if is_maintenance(body_text) or is_maintenance(title):
                    return status, STATUS_MAINTENANCE, title, body_text
                if status < 400 and len(body_text.strip()) >= SCANNER_CONFIG.MIN_CONTENT_LENGTH:
                    return status, STATUS_OK, title, body_text
                if status < 400:
                    return status, STATUS_EMPTY_PAGE, title, body_text
                return status, f"HTTP_{status}", title, body_text
            finally:
                browser.close()
    except Exception as e:
        return None, STATUS_BROWSER_ERROR, "", str(e)[:80]


# ---------------------------------------------------------------------------
# Response classification  (MAINTENANCE has priority over REDIRECT)
# ---------------------------------------------------------------------------

def classify_http_response(
    entry: Dict,
    original_url: str,
    resp: httpx.Response,
    final_url: str,
    title: str,
    body_text: str,
) -> Dict:
    original_domain = urlparse(original_url).netloc
    final_domain    = urlparse(final_url).netloc
    code            = resp.status_code

    is_redirect   = final_domain != original_domain
    redirect_note = ""
    status_label  = STATUS_OK

    # Maintenance takes priority — a redirected maintenance page is MAINTENANCE
    if is_maintenance(body_text) or is_maintenance(title):
        status_label = STATUS_MAINTENANCE
    elif is_redirect:
        if final_domain.endswith(".com"):
            status_label  = STATUS_REDIRECT_MAIN
            redirect_note = f"Redirected to .com ({final_domain})"
        else:
            status_label  = STATUS_REDIRECT_OTHER
            redirect_note = f"Redirected to {final_domain}"

    content_length = len(body_text)
    is_empty       = content_length < SCANNER_CONFIG.MIN_CONTENT_LENGTH
    if is_empty and status_label == STATUS_OK:
        status_label = STATUS_EMPTY_PAGE

    notes = []
    if redirect_note:
        notes.append(redirect_note)
    notes.append(f"Title: {title}" if title else "No title")
    if is_empty:
        notes.append(
            "Maintenance page (site temporarily down)"
            if status_label == STATUS_MAINTENANCE
            else f"Low content length ({content_length} chars)"
        )
    if title and not any(kw in title.lower() for kw in SCANNER_CONFIG.BRAND_KEYWORDS):
        notes.append("Brand mismatch (title doesn't contain 'lingua/learn')")
        if status_label == STATUS_OK:
            status_label = STATUS_BRAND_MISMATCH

    return {
        **entry,
        "status": status_label,
        "code":   code,
        "note":   " | ".join(notes) or "No additional info",
    }


# ---------------------------------------------------------------------------
# Per-URL checker
# ---------------------------------------------------------------------------

def scan_franchise_url(
    entry: Dict[str, Any],
    client: httpx.Client,
    args: argparse.Namespace,
    rate_limiter: RateLimiter,
) -> Dict[str, Any]:
    url = entry["url"]
    if entry.get("status") == STATUS_COMING_SOON:
        return entry

    rate_limiter.wait()

    request_headers = {
        **SCANNER_CONFIG.HEADERS,
        "User-Agent": random.choice(SCANNER_CONFIG.USER_AGENTS),
    }

    parsed        = urlparse(url)
    original_host = parsed.netloc
    path_and_query = parsed.path + ("?" + parsed.query if parsed.query else "")
    has_www_prefix = original_host.lower().startswith("www.")
    host_without_www = original_host[4:] if has_www_prefix else original_host

    urls_to_try = [url]
    if parsed.scheme == "http":
        urls_to_try.append(f"https://{original_host}{path_and_query}")
    elif parsed.scheme == "https":
        urls_to_try.append(f"http://{original_host}{path_and_query}")
    if has_www_prefix:
        urls_to_try.append(f"{parsed.scheme}://{host_without_www}{path_and_query}")
        alt_scheme = "http" if parsed.scheme == "https" else "https"
        urls_to_try.append(f"{alt_scheme}://{host_without_www}{path_and_query}")

    last_code: Optional[int] = None
    last_label  = "ERROR"
    last_note   = ""
    last_429    = False
    success_result: Optional[Dict] = None
    should_retry = False

    for attempt in range(args.retries + 1):
        if success_result is not None:
            break

        for candidate_url in urls_to_try:
            try:
                resp      = client.get(candidate_url, timeout=args.timeout,
                                       headers=request_headers, follow_redirects=True)
                code      = resp.status_code
                final_url = str(resp.url)

                if code < 400:
                    content_type = resp.headers.get("content-type", "")
                    candidate_host = urlparse(candidate_url).netloc
                    used_non_www_host = has_www_prefix and candidate_host == host_without_www

                    if "text/html" in content_type:
                        soup      = BeautifulSoup(resp.text, "html.parser")
                        title     = soup.title.get_text(strip=True) if soup.title else ""
                        body_text = soup.get_text(strip=True)

                        if is_parked(body_text) or is_parked(title):
                            success_result = {
                                **entry, "status": STATUS_PARKED,
                                "code": code, "note": "Domain parked / for sale",
                            }
                            break

                        if is_bot_blocked(title, body_text):
                            if args.use_browser:
                                browser_code, browser_status, browser_title, browser_body = (
                                    inspect_url_with_browser(url, args.timeout)
                                )
                            else:
                                browser_code = None
                                browser_status = STATUS_BOT_BLOCKED
                                browser_title = title
                            success_result = {
                                **entry, "status": browser_status, "code": browser_code,
                                "note": f"Bot/CAPTCHA detected | Title: {browser_title}",
                            }
                            break

                        result = classify_http_response(
                            entry,
                            url,
                            resp,
                            final_url,
                            title,
                            body_text,
                        )
                        if used_non_www_host:
                            result["note"] = "[www. removed] " + result.get("note", "")

                        # Empty page — try Playwright for a proper render.
                        # Covers HTTP 202 too: if the body was empty, classify_http_response
                        # already set status=EMPTY_PAGE, so this condition catches it.
                        if args.use_browser and result.get("status") == STATUS_EMPTY_PAGE:
                            browser_code, browser_status, browser_title, browser_body = \
                                inspect_url_with_browser(url, args.timeout)
                            if browser_status not in (
                                STATUS_BROWSER_ERROR,
                                STATUS_EMPTY_PAGE,
                                STATUS_BOT_BLOCKED,
                            ):
                                # Playwright got real content — use it
                                success_result = {
                                    **entry, "status": browser_status, "code": browser_code,
                                    "note": f"Browser-rendered | Title: {browser_title}",
                                }
                                break
                            # Playwright was blocked (BOT_BLOCKED/Robot Challenge) or also
                            # empty. The httpx 202 result is more reliable — site is
                            # reachable, just throttling the scanner. Fall through to store
                            # the original classify_http_response result.

                        # No title from httpx — get rendered title via Playwright
                        if (
                            args.use_browser
                            and not title
                            and code < 400
                            and result.get("status") != STATUS_EMPTY_PAGE
                        ):
                            browser_code, browser_status, browser_title, browser_body = \
                                inspect_url_with_browser(url, args.timeout)
                            if browser_code and browser_code < 400:
                                result = classify_http_response(entry, url, resp, final_url,
                                                                browser_title, browser_body)
                                if used_non_www_host:
                                    result["note"] = "[www. removed] " + result.get("note", "")

                        success_result = result
                        break
                    else:
                        note = f"Content-Type: {content_type}"
                        if used_non_www_host:
                            note = "[www. removed] " + note
                        success_result = {**entry, "status": STATUS_OK, "code": code, "note": note}
                        break

                # --- non-2xx ---
                last_code = code

                if code == 429:
                    last_429      = True
                    final_url_429 = str(resp.url)
                    original_domain = urlparse(url).netloc
                    location      = resp.headers.get("location", "")
                    location_domain = urlparse(location).netloc if location else ""
                    final_domain_429 = urlparse(final_url_429).netloc
                    redirect_domain  = location_domain or final_domain_429

                    if redirect_domain and redirect_domain != original_domain:
                        # We have enough info from headers — classify and return now.
                        # Don't retry or use Playwright; the CDN will keep rate-limiting.
                        if redirect_domain.endswith(".com"):
                            label = STATUS_REDIRECT_MAIN
                            note  = f"Redirected to .com ({redirect_domain}) [rate-limited]"
                        else:
                            label = STATUS_REDIRECT_OTHER
                            note  = f"Redirected to {redirect_domain} [rate-limited]"
                        return {**entry, "status": label, "code": code, "note": note}
                    else:
                        last_label = "HTTP_429"
                        last_note  = f"Rate limited, final URL: {final_url_429}"

                    cooldown = random.uniform(5, 10)
                    print(f"  [429] {url} — cooling down {cooldown:.0f}s before retry...")
                    time.sleep(cooldown)
                    if attempt < args.retries:
                        should_retry = True
                    break

                elif code == 403:
                    last_label = STATUS_FORBIDDEN
                    last_note  = f"Status {code}"
                    # Immediate Playwright fallback for 403 — often just a bot gate
                    browser_code, browser_status, browser_title, browser_body = (
                        inspect_url_with_browser(url, args.timeout)
                        if args.use_browser
                        else (None, STATUS_FORBIDDEN, "", "")
                    )
                    if browser_status not in (STATUS_BROWSER_ERROR, STATUS_BOT_BLOCKED):
                        if browser_code and browser_code < 400:
                            class MockResponse:
                                def __init__(self, status_code):
                                    self.status_code = status_code
                                    self.url         = url
                                    self.headers     = {}
                            result = classify_http_response(entry, url, MockResponse(browser_code),
                                                            url, browser_title, browser_body)
                            success_result = result
                        else:
                            success_result = {
                                **entry, "status": browser_status, "code": browser_code,
                                "note": f"Status {browser_code} | Title: {browser_title}",
                            }
                        break
                    last_note = "Status 403 | Playwright: browser error or still blocked"

                elif code == 404:
                    last_label = STATUS_NOT_FOUND
                    last_note  = f"Status {code}"
                elif code >= 500:
                    last_label = f"SERVER_ERROR_{code}"
                    last_note  = f"Status {code}"
                    if attempt < args.retries:
                        should_retry = True
                    break
                else:
                    last_label = f"CLIENT_ERROR_{code}"
                    last_note  = f"Status {code}"

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                err_str    = str(e)
                last_label = STATUS_TIMEOUT if isinstance(e, httpx.TimeoutException) \
                             else STATUS_CONNECTION_ERROR
                last_note  = err_str[:50]

                # SSL certificate failure — retry once with verify=False so we can
                # still read the page content (e.g. Chile/Lithuania maintenance pages).
                # We flag the result with SSL_ERROR so the report stays honest.
                is_ssl_error = (
                    isinstance(e, httpx.ConnectError) and (
                        "CERTIFICATE_VERIFY_FAILED" in err_str
                        or "SSL" in err_str
                        or "certificate" in err_str.lower()
                    )
                )
                if is_ssl_error:
                    try:
                        with httpx.Client(
                            verify=False,
                            timeout=args.timeout,
                            headers=request_headers,
                            follow_redirects=True,
                        ) as insecure_client:
                            insecure_response = insecure_client.get(candidate_url)
                            insecure_final_url = str(insecure_response.url)
                            insecure_status_code = insecure_response.status_code

                            # If body is empty, try two fallback URLs in order:
                            # 1. The final redirect destination (may differ from candidate_url)
                            # 2. The bare domain (strips www. — some servers block www but
                            #    allow the bare domain directly, e.g. Chile, Lithuania)
                            if not insecure_response.text.strip():
                                parsed_candidate = urlparse(candidate_url)
                                bare_domain_url = urlunparse(parsed_candidate._replace(
                                    netloc=parsed_candidate.netloc[4:]
                                    if parsed_candidate.netloc.startswith("www.")
                                    else parsed_candidate.netloc
                                ))
                                for fallback_url in [insecure_final_url, bare_domain_url]:
                                    if fallback_url == candidate_url:
                                        continue
                                    try:
                                        insecure_response = insecure_client.get(fallback_url)
                                        insecure_final_url = str(insecure_response.url)
                                        insecure_status_code = insecure_response.status_code
                                        if insecure_response.text.strip():
                                            break
                                    except Exception:
                                        pass

                            if "text/html" in insecure_response.headers.get("content-type", ""):
                                insecure_soup = BeautifulSoup(insecure_response.text, "html.parser")
                                insecure_title = (
                                    insecure_soup.title.get_text(strip=True)
                                    if insecure_soup.title
                                    else ""
                                )
                                insecure_body_text = insecure_soup.get_text(strip=True)
                                if (
                                    is_maintenance(insecure_body_text)
                                    or is_maintenance(insecure_title)
                                ):
                                    success_result = {
                                        **entry, "url": insecure_final_url,
                                        "status": STATUS_MAINTENANCE,
                                        "code": insecure_status_code,
                                        "note": f"SSL_ERROR (cert invalid) — page readable | "
                                                f"Title: {insecure_title}",
                                    }
                                    break
                                else:
                                    # Page readable but not maintenance — full classification
                                    insecure_result = classify_http_response(
                                        entry,
                                        candidate_url,
                                        insecure_response,
                                        insecure_final_url,
                                        insecure_title,
                                        insecure_body_text,
                                    )
                                    insecure_result["note"] = (
                                        "SSL_ERROR (cert invalid) — page readable | "
                                        + insecure_result.get("note", "")
                                    )
                                    success_result = insecure_result
                                    break
                    except Exception:
                        pass  # SSL bypass also failed — fall through

                    # httpx (even with verify=False) was blocked by the server.
                    # Keep last_label = CONNECTION_ERROR so the final Playwright
                    # fallback fires — Playwright renders as a real browser and
                    # bypasses the server's bot/host check that blocks plain httpx.
                    if success_result is None:
                        last_note = "SSL_ERROR — httpx blocked, escalating to Playwright"

                if success_result is None and attempt < args.retries:
                    should_retry = True
                break

            except httpx.RequestError as e:
                last_label = STATUS_REQUEST_ERROR
                last_note  = str(e)[:50]

        if should_retry:
            should_retry = False
            backoff = args.retry_delay * (2 ** attempt) + random.uniform(0.5, 2.0)
            print(f"  [retry {attempt + 1}] {url} — waiting {backoff:.1f}s")
            time.sleep(backoff)
            continue

    if success_result is not None:
        return success_result

    # Final fallback: Playwright for TIMEOUT / FORBIDDEN / CONNECTION_ERROR.
    # Note: 429 with a known redirect domain now returns early (above).
    # Only pure HTTP_429 (no redirect info) reaches here.
    should_use_browser_fallback = (
        args.use_browser
        and (
            last_label in (STATUS_TIMEOUT, STATUS_FORBIDDEN, STATUS_CONNECTION_ERROR)
            or (last_429 and last_label == "HTTP_429")
        )
    )
    if should_use_browser_fallback:
        browser_code, browser_status, browser_title, browser_body = inspect_url_with_browser(
            url,
            args.timeout,
        )
        if browser_status != STATUS_BROWSER_ERROR:
            ssl_prefix = "SSL_ERROR (cert invalid) — " if "SSL_ERROR" in last_note else ""
            return {**entry, "status": browser_status, "code": browser_code,
                    "note": f"{ssl_prefix}Browser-rendered | Title: {browser_title}"}

    return {**entry, "status": last_label, "code": last_code,
            "note": last_note or entry.get("note", "")}


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
    parser = argparse.ArgumentParser(
        description="Check franchise links – daily scan edition (production)."
    )
    parser.add_argument("--url",           default=SCANNER_CONFIG.DEFAULT_URL)
    parser.add_argument("--output",        default=None)
    parser.add_argument("--timeout",       type=positive_int,
                        default=SCANNER_CONFIG.DEFAULT_TIMEOUT)
    parser.add_argument("--workers",       type=positive_int,
                        default=SCANNER_CONFIG.DEFAULT_MAX_WORKERS)
    parser.add_argument("--retries",       type=int,
                        default=SCANNER_CONFIG.DEFAULT_RETRIES)
    parser.add_argument("--rate-limit",    type=non_negative_float,
                        default=SCANNER_CONFIG.DEFAULT_RATE_LIMIT)
    parser.add_argument("--retry-delay",   type=non_negative_float, dest="retry_delay",
                        default=SCANNER_CONFIG.DEFAULT_RETRY_DELAY)
    parser.add_argument("--use-browser",   action="store_true",
                        help="Trigger Playwright on TIMEOUT/FORBIDDEN/429 results")
    args, _ = parser.parse_known_args()
    return args


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_email(file_path: str, summary: str) -> None:
    sender   = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    receiver = os.environ.get("EMAIL_TO")
    if not sender or not password or not receiver:
        print("Email not sent: set EMAIL_USER / EMAIL_PASS / EMAIL_TO env vars.")
        return

    date_str = datetime.now().strftime("%d %b %Y")
    msg            = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = receiver
    msg["Subject"] = f"Franchise Links Report – {date_str}"
    body = (
        f"Daily franchise links check completed on {date_str}.\n\n"
        f"Summary:\n{summary}\n\n"
        "Full report attached as CSV."
    )
    msg.attach(MIMEText(body, "plain"))

    try:
        with open(file_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f"attachment; filename={os.path.basename(file_path)}")
            msg.attach(part)
    except OSError as e:
        print(f"Failed to attach file: {e}")
        return

    smtp_host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    smtp_port_raw = os.environ.get("SMTP_PORT") or "587"
    try:
        smtp_port = int(smtp_port_raw)
    except ValueError:
        print(f"Invalid SMTP_PORT={smtp_port_raw!r}; using default 587.")
        smtp_port = 587
    try:
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print(f"Email sent to {receiver}")
    except Exception as e:
        print(f"Failed to send email: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    args = parse_args()

    print(f"Starting daily scan  →  {args.url}")
    print(f"Workers: {args.workers} | Timeout: {args.timeout}s | "
          f"Retries: {args.retries} | Retry delay: {args.retry_delay}s | "
          f"Browser fallback: {args.use_browser}")

    entries = extract_franchise_entries(args.url)
    if not entries:
        sys.exit("No 'Visit Website' links found.")

    live_entries = [
        (position, entry)
        for position, entry in enumerate(entries)
        if entry.get("status") != STATUS_COMING_SOON
    ]
    results_by_position: List[Optional[Dict[str, Any]]] = [
        entry if entry.get("status") == STATUS_COMING_SOON else None
        for entry in entries
    ]
    rate_limiter = RateLimiter(args.rate_limit)

    with httpx.Client(
        http2=True,
        follow_redirects=True,
        headers=SCANNER_CONFIG.HEADERS,
        limits=httpx.Limits(max_keepalive_connections=5),
    ) as client:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    scan_franchise_url,
                    entry,
                    client,
                    args,
                    rate_limiter,
                ): (position, entry)
                for position, entry in live_entries
            }
            done = 0
            for future in as_completed(futures):
                done += 1
                position, entry = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {**entry, "status": STATUS_UNHANDLED_ERROR,
                              "code": 0, "note": str(exc)[:100]}
                results_by_position[position] = result
                print(f"  [{done}/{len(live_entries)}] {result['country']} — {result['status']}")

    results = [result for result in results_by_position if result is not None]

    if args.output:
        output_file = args.output
    else:
        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Save to data/ folder for dashboard integration
        os.makedirs("data", exist_ok=True)
        output_file = f"data/{SCANNER_CONFIG.DEFAULT_OUTPUT_BASE}_{timestamp}.csv"

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(results)

    print(f"\nDone. Report saved: {output_file}")

    counts  = Counter(r["status"] for r in results)
    summary = "\n".join(f"  {status}: {count}" for status, count in sorted(counts.items()))
    print(summary)

    send_email(output_file, summary)


if __name__ == "__main__":
    run()
