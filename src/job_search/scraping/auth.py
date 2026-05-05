from __future__ import annotations

import requests
from loguru import logger
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


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
        raise ValueError(f"Unsupported browser: {browser!r}. Choose 'chrome', 'edge', or 'firefox'.")

    # Mask the webdriver flag so LinkedIn doesn't detect automation
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def create_session(email: str, password: str, browser: str = "edge") -> requests.Session | None:
    """
    Automate LinkedIn login via Selenium and return an authenticated requests.Session.
    Waits up to 60 seconds for LinkedIn to reach an authenticated page after submitting
    credentials (to allow for any redirect delays). Returns None on failure so the
    caller can shut down gracefully.
    """
    _LOGGED_IN_PATHS = ("/feed", "/home", "/mynetwork", "/jobs", "/messaging", "/notifications")
    _AUTH_TIMEOUT = 60  # seconds to wait for post-login redirect

    def _is_logged_in() -> bool:
        return any(p in driver.current_url for p in _LOGGED_IN_PATHS)

    driver = _make_driver(browser)
    driver.get("https://www.linkedin.com/login")

    try:
        wait = WebDriverWait(driver, 20)

        if _is_logged_in():
            logger.info("Already authenticated on LinkedIn ({})", driver.current_url)
        else:
            try:
                username_field = wait.until(EC.presence_of_element_located((By.ID, "username")))
                username_field.send_keys(email)

                password_field = wait.until(EC.presence_of_element_located((By.ID, "password")))
                password_field.send_keys(password)

                sign_in_button = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
                )
                sign_in_button.click()

                wait.until(EC.url_changes(driver.current_url))
                logger.info("Login submitted, now on: {}", driver.current_url)
            except TimeoutException:
                if _is_logged_in():
                    logger.info("Redirected to authenticated page: {}", driver.current_url)
                else:
                    logger.error("Login form not found and not on authenticated page. Current URL: {}", driver.current_url)
                    driver.quit()
                    return None

        # Wait for a confirmed authenticated URL (handles slow redirects, 2FA, etc.)
        try:
            WebDriverWait(driver, _AUTH_TIMEOUT).until(lambda d: _is_logged_in())
            logger.info("LinkedIn authentication confirmed ({})", driver.current_url)
        except TimeoutException:
            logger.error(
                "LinkedIn login did not reach an authenticated page within {}s. "
                "Current URL: {}. Check credentials or whether CAPTCHA/2FA is blocking login.",
                _AUTH_TIMEOUT,
                driver.current_url,
            )
            driver.quit()
            return None

    except Exception as exc:
        logger.error("Unexpected auth error: {}", exc)
        driver.quit()
        return None

    # Navigate to jobs page so LinkedIn sets all session cookies
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
