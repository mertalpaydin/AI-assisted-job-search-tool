from __future__ import annotations

import time
from pathlib import Path

import requests
from loguru import logger
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Selector sets — LinkedIn serves at least two different login-page layouts:
#
#  "Classic"  /login   — id=username / id=password / button[type=submit]
#  "React"    /login/  — React-generated ids, autocomplete attrs, ALL buttons
#                        have type="button" — the real <input> element exists
#                        in the DOM but has CSS making it invisible to Selenium
#                        so interactability-checks fail.
#
# Strategy:
#   1. Detect fields via presence_of_element_located (works on both layouts)
#   2. Fill fields via JavaScript (bypasses Selenium's interactability checks
#      and is compatible with React's controlled-input state management)
#   3. Submit via JS click on the Einloggen/Sign-in button
# ---------------------------------------------------------------------------

_USERNAME_CSS = (
    "#username, "
    "input[name='session_key'], "
    "input[autocomplete='username'], "
    "input[autocomplete='email'], "
    "input[type='email']"
)

_PASSWORD_CSS = (
    "#password, "
    "input[name='session_password'], "
    "input[autocomplete='current-password'], "
    "input[type='password']"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_driver(browser: str) -> webdriver.Remote:
    browser = browser.lower()
    if browser == "chrome":
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        opts = ChromeOptions()
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        driver = webdriver.Chrome(options=opts)
    elif browser == "edge":
        from selenium.webdriver.edge.options import Options as EdgeOptions
        opts = EdgeOptions()
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        driver = webdriver.Edge(options=opts)
    elif browser == "firefox":
        driver = webdriver.Firefox()
    else:
        raise ValueError(
            f"Unsupported browser: {browser!r}. Choose 'chrome', 'edge', or 'firefox'."
        )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def _save_debug_snapshot(driver: webdriver.Remote, label: str) -> None:
    """Save a screenshot and partial page-source to ./debug/ for diagnosis."""
    try:
        debug_dir = Path("debug")
        debug_dir.mkdir(exist_ok=True)
        ts = int(time.time())
        shot_path = debug_dir / f"login_fail_{label}_{ts}.png"
        src_path  = debug_dir / f"login_fail_{label}_{ts}.html"
        driver.save_screenshot(str(shot_path))
        src_path.write_text(driver.page_source[:50_000], encoding="utf-8", errors="replace")
        logger.info("Debug snapshot saved → {} / {}", shot_path, src_path)
    except Exception as exc:
        logger.debug("Could not save debug snapshot: {}", exc)


def _js_set_value(driver: webdriver.Remote, element, value: str) -> None:
    """
    Set a form field value via JavaScript, compatible with React controlled inputs.

    React's synthetic event system tracks value through the native property.
    Setting element.value directly is ignored by React because it doesn't
    trigger React's internal synthetic events.  The correct approach is to
    use the native HTMLInputElement.prototype value setter (bypasses React's
    override) then fire both 'input' and 'change' events with bubbles:true.
    """
    driver.execute_script(
        """
        var setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        ).set;
        setter.call(arguments[0], arguments[1]);
        arguments[0].dispatchEvent(new Event('input',  {bubbles: true}));
        arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
        """,
        element,
        value,
    )


def _js_click_submit(driver: webdriver.Remote) -> bool:
    """
    Find and click the sign-in submit button via JavaScript.

    Handles both the classic page (button[type='submit']) and the React page
    (all buttons have type="button"; we match by text content in several
    languages LinkedIn commonly uses).
    """
    clicked = driver.execute_script(
        """
        // 1. Prefer an explicit type=submit button that is NOT an OAuth button
        var submitBtns = document.querySelectorAll("button[type='submit']");
        for (var b of submitBtns) {
            var lbl = (b.getAttribute('aria-label') || '').toLowerCase();
            if (lbl.indexOf('apple') === -1 && lbl.indexOf('google') === -1
                    && lbl.indexOf('microsoft') === -1) {
                b.click();
                return 'submit:' + (b.textContent.trim() || b.getAttribute('aria-label'));
            }
        }

        // 2. React pages use type="button" — find by visible text
        var keywords = [
            'einloggen', 'anmelden', 'sign in', 'log in',
            'continuar', 'connexion', 'acceder', 'ingresar'
        ];
        var allBtns = document.querySelectorAll('button');
        for (var b of allBtns) {
            var t = b.textContent.trim().toLowerCase();
            for (var kw of keywords) {
                if (t === kw) {
                    b.click();
                    return 'text:' + b.textContent.trim();
                }
            }
        }

        // 3. Last resort: first visible button-looking element in a form
        var forms = document.querySelectorAll('form');
        for (var f of forms) {
            var btn = f.querySelector("button");
            if (btn) { btn.click(); return 'form-first:' + btn.textContent.trim(); }
        }
        return null;
        """
    )
    if clicked:
        logger.debug("Submit clicked via JS: {}", clicked)
        return True
    logger.warning("Could not find a submit button via JS")
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_session(
    email: str,
    password: str,
    browser: str = "edge",
    attempt: int = 1,
) -> requests.Session | None:
    """
    Automate LinkedIn login via Selenium and return an authenticated requests.Session.

    Handles both the classic (/login) and React (/login/) versions of the
    LinkedIn login page.  Uses JavaScript for all form interactions so that
    Selenium's visibility/interactability checks do not block React pages.
    On failure a screenshot and partial page-source are written to ./debug/.

    Returns None on failure so the caller can retry or shut down gracefully.
    """
    _LOGGED_IN_PATHS = (
        "/feed", "/home", "/mynetwork", "/jobs", "/messaging", "/notifications"
    )
    _FORM_WAIT    = 20   # seconds to wait for the username field to appear
    _AUTH_TIMEOUT = 60   # seconds to wait for post-login redirect

    def _is_logged_in() -> bool:
        return any(p in driver.current_url for p in _LOGGED_IN_PATHS)

    logger.info("Login attempt {} — opening LinkedIn login page…", attempt)
    driver = _make_driver(browser)
    driver.get("https://www.linkedin.com/login")

    try:
        wait = WebDriverWait(driver, _FORM_WAIT)

        if _is_logged_in():
            logger.info("Already authenticated on LinkedIn ({})", driver.current_url)
        else:
            try:
                logger.debug("Waiting for username field (URL: {})…", driver.current_url)

                # presence_of_element_located works for both page layouts.
                # element_to_be_clickable fails on the React layout because the
                # actual <input> is CSS-hidden — it's a common React accessible
                # input pattern where the real input is behind a styled overlay.
                username_field = wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, _USERNAME_CSS)
                    )
                )
                logger.debug(
                    "Username field — id={!r} name={!r} type={!r} autocomplete={!r}",
                    username_field.get_attribute("id"),
                    username_field.get_attribute("name"),
                    username_field.get_attribute("type"),
                    username_field.get_attribute("autocomplete"),
                )

                password_field = wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, _PASSWORD_CSS)
                    )
                )
                logger.debug(
                    "Password field — id={!r} name={!r} type={!r}",
                    password_field.get_attribute("id"),
                    password_field.get_attribute("name"),
                    password_field.get_attribute("type"),
                )

                # Fill both fields via JS (works for React and classic pages)
                _js_set_value(driver, username_field, email)
                _js_set_value(driver, password_field, password)
                logger.debug("Credentials injected via JS")

                # Short pause so React can process the state updates
                time.sleep(0.5)

                # Click the submit button via JS
                _js_click_submit(driver)
                logger.info("Login submitted")

                # Wait for the URL to change away from /login
                wait.until(EC.url_changes(driver.current_url))
                logger.info("Post-submit URL: {}", driver.current_url)

            except TimeoutException:
                if _is_logged_in():
                    logger.info(
                        "Redirected to authenticated page: {}", driver.current_url
                    )
                else:
                    logger.error(
                        "Login form not found within {}s. URL: {}  Title: {!r}",
                        _FORM_WAIT,
                        driver.current_url,
                        driver.title,
                    )
                    _save_debug_snapshot(
                        driver, f"form_not_found_attempt{attempt}"
                    )
                    driver.quit()
                    return None

        # Wait for a confirmed authenticated URL
        # (handles slow redirects, 2FA prompts, CAPTCHA pages, etc.)
        try:
            WebDriverWait(driver, _AUTH_TIMEOUT).until(lambda d: _is_logged_in())
            logger.info("LinkedIn authentication confirmed ({})", driver.current_url)
        except TimeoutException:
            logger.error(
                "LinkedIn login did not reach an authenticated page within {}s. "
                "Current URL: {}. Check credentials or whether CAPTCHA/2FA is blocking.",
                _AUTH_TIMEOUT,
                driver.current_url,
            )
            _save_debug_snapshot(driver, f"auth_timeout_attempt{attempt}")
            driver.quit()
            return None

    except Exception as exc:
        logger.error("Unexpected auth error (attempt {}): {}", attempt, exc)
        _save_debug_snapshot(driver, f"unexpected_attempt{attempt}")
        driver.quit()
        return None

    # Navigate to jobs page so LinkedIn sets all required session cookies
    driver.get(
        "https://www.linkedin.com/jobs/search/"
        "?keywords=Python+Developer&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON"
    )

    cookies = driver.get_cookies()
    driver.quit()

    session = requests.Session()
    for cookie in cookies:
        session.cookies.set(cookie["name"], cookie["value"])

    logger.info("Session created for {}", email)
    return session


def make_headers(session: requests.Session) -> dict[str, str]:
    """Build the LinkedIn Voyager API headers for a given session."""
    csrf_token = session.cookies.get("JSESSIONID", "").strip('"')
    cookie_str = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
    return {
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_str,
        "Csrf-Token": csrf_token,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "X-Li-Track": (
            '{"clientVersion":"1.13.5589","mpVersion":"1.13.5589","osName":"web",'
            '"timezoneOffset":1,"timezone":"Europe/Berlin","deviceFormFactor":"DESKTOP",'
            '"mpName":"voyager-web","displayDensity":1,"displayWidth":1920,"displayHeight":1080}'
        ),
    }
