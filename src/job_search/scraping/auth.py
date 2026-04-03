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
        return webdriver.Chrome()
    elif browser == "edge":
        return webdriver.Edge()
    elif browser == "firefox":
        return webdriver.Firefox()
    else:
        raise ValueError(f"Unsupported browser: {browser!r}. Choose 'chrome', 'edge', or 'firefox'.")


def create_session(email: str, password: str, browser: str = "edge") -> requests.Session | None:
    """
    Automate LinkedIn login via Selenium and return an authenticated requests.Session.
    Prompts the user to press ENTER after handling any CAPTCHA or 2FA.
    Returns None if login elements are not found within the timeout.
    """
    driver = _make_driver(browser)
    driver.get("https://www.linkedin.com/checkpoint/rm/sign-in-another-account")

    try:
        wait = WebDriverWait(driver, 10)

        username_field = wait.until(EC.presence_of_element_located((By.ID, "username")))
        username_field.send_keys(email)

        password_field = wait.until(EC.presence_of_element_located((By.ID, "password")))
        password_field.send_keys(password)

        sign_in_button = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, '//*[@id="organic-div"]/form/div[4]/button')
            )
        )
        sign_in_button.click()

        wait.until(EC.url_changes(driver.current_url))

    except TimeoutException:
        logger.error("Login element not found within timeout for {}", email)
        driver.quit()
        return None

    input(f'Press ENTER after successful login for "{email}": ')

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
