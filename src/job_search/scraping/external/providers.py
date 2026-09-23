"""
External job search providers: Indeed (via JobSpy), Arbeitsagentur, SerpApi, and RapidAPI JSearch.
Each provider handles rate limits, quotas, and errors gracefully.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Any

import requests
from loguru import logger


class BaseProvider:
    """Base class for external job providers."""

    name: str = "base"

    def search(self, keyword: str, location: str, limit: int = 15) -> list[dict[str, Any]]:
        raise NotImplementedError


class IndeedProvider(BaseProvider):
    """Indeed scraping via python-jobspy with TLS fingerprinting."""

    name: str = "indeed"

    def __init__(self, delay_between_calls: float = 2.0) -> None:
        self._delay = delay_between_calls

    def search(self, keyword: str, location: str, limit: int = 15) -> list[dict[str, Any]]:
        from jobspy import scrape_jobs

        results = []
        try:
            logger.info("[Indeed] Searching for '{}' in '{}'...", keyword, location)
            df = scrape_jobs(
                site_name=["indeed"],
                search_term=keyword,
                location=location,
                results_wanted=limit,
                country_indeed="germany",
            )
            time.sleep(self._delay)

            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    jk = str(row.get("id") or "").strip()
                    job_url = str(row.get("job_url") or "").strip()
                    if not jk and "jk=" in job_url:
                        jk = job_url.split("jk=")[-1].split("&")[0]
                    if not jk:
                        jk = str(hash(f"{row.get('company')}_{row.get('title')}"))

                    results.append({
                        "source": "indeed",
                        "external_id": jk,
                        "title": str(row.get("title") or "").strip(),
                        "company_name": str(row.get("company") or "").strip(),
                        "location": str(row.get("location") or "").strip(),
                        "url": job_url,
                        "description": str(row.get("description") or "").strip(),
                        "salary_info": str(row.get("salary_source") or "").strip() or None,
                        "posted_at": str(row.get("date_posted") or "").strip() or None,
                        "search_keyword": keyword,
                    })
            logger.info("[Indeed] Found {} results for '{}'", len(results), keyword)
        except Exception as e:
            logger.error("[Indeed] Scraping error for '{}': {}", keyword, e)

        return results


class ArbeitsagenturProvider(BaseProvider):
    """German Federal Employment Agency official REST API provider."""

    name: str = "arbeitsagentur"
    BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
    HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "Mozilla/5.0"}

    def search(self, keyword: str, location: str, limit: int = 15) -> list[dict[str, Any]]:
        results = []
        try:
            logger.info("[Arbeitsagentur] Searching for '{}' in '{}'...", keyword, location)
            # Bundesagentur für Arbeit API expects a clean German city or postal code without country suffix (e.g. 'Frankfurt' instead of 'Frankfurt, Germany')
            clean_location = location.split(",")[0].strip() if "," in location else location.strip()
            search_url = f"{self.BASE_URL}/pc/v6/jobs"
            params = {
                "was": keyword,
                "wo": clean_location,
                "size": limit,
            }
            resp = requests.get(search_url, headers=self.HEADERS, params=params, timeout=20)
            if resp.status_code != 200:
                logger.warning("[Arbeitsagentur] Search failed ({}) for '{}'", resp.status_code, keyword)
                return results

            data = resp.json()
            items = data.get("ergebnisliste", [])

            for item in items[:limit]:
                refnr = str(item.get("referenznummer") or "").strip()
                title = str(item.get("stellenangebotsTitel") or item.get("titel") or "").strip()
                company = str(item.get("firma") or item.get("arbeitgeber") or "").strip()
                if not refnr or not title or not company:
                    continue

                # Parse location
                loc_list = item.get("stellenlokationen", [])
                loc_str = location
                if loc_list and isinstance(loc_list, list):
                    first_loc = loc_list[0]
                    if isinstance(first_loc, dict):
                        addr = first_loc.get("adresse", {})
                        city = addr.get("ort") or addr.get("region")
                        if city:
                            loc_str = str(city)

                # Parse salary info if present
                salary_str = None
                s_von = item.get("gehaltsspanneVon")
                s_bis = item.get("gehaltsspanneBis")
                if s_von or s_bis:
                    if s_von and s_bis:
                        salary_str = f"€{int(s_von):,} - €{int(s_bis):,}"
                    elif s_von:
                        salary_str = f"From €{int(s_von):,}"

                external_url = item.get("externeURL") or item.get("allianzpartnerUrl")

                # Fetch full description using base64 encoded refnr
                description = ""
                try:
                    b64_refnr = base64.b64encode(refnr.encode("utf-8")).decode("utf-8")
                    detail_url = f"{self.BASE_URL}/pc/v4/jobdetails/{b64_refnr}"
                    d_resp = requests.get(detail_url, headers=self.HEADERS, timeout=15)
                    if d_resp.status_code == 200:
                        d_data = d_resp.json()
                        description = str(d_data.get("stellenangebotsBeschreibung") or "").strip()
                        if not external_url:
                            external_url = d_data.get("externeURL")
                except Exception as de:
                    logger.debug("[Arbeitsagentur] Could not fetch details for {}: {}", refnr, de)

                results.append({
                    "source": "arbeitsagentur",
                    "external_id": refnr,
                    "title": title,
                    "company_name": company,
                    "location": loc_str,
                    "url": external_url or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
                    "description": description,
                    "salary_info": salary_str,
                    "posted_at": str(item.get("datumErsteVeroeffentlichung") or "").strip() or None,
                    "search_keyword": keyword,
                })

            logger.info("[Arbeitsagentur] Found {} results for '{}'", len(results), keyword)
        except Exception as e:
            logger.error("[Arbeitsagentur] Error searching for '{}': {}", keyword, e)

        return results


class SerpApiProvider(BaseProvider):
    """Google for Jobs via SerpApi."""

    name: str = "serpapi"

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")
        if not key:
            try:
                from job_search.core.config import load_secrets
                key = load_secrets().serpapi_api_key
            except Exception:
                pass
        self._api_key = key
        self._quota_exhausted = False

    def search(self, keyword: str, location: str, limit: int = 15) -> list[dict[str, Any]]:
        results = []
        if not self._api_key:
            logger.warning("[SerpApi] No SERPAPI_API_KEY found; skipping provider.")
            return results
        if self._quota_exhausted:
            logger.warning("[SerpApi] Quota previously exhausted; skipping search for '{}'.", keyword)
            return results

        try:
            logger.info("[SerpApi] Querying Google Jobs for '{}' in '{}'...", keyword, location)
            url = "https://serpapi.com/search.json"
            params = {
                "engine": "google_jobs",
                "q": f"{keyword} {location}",
                "location": location,
                "hl": "en",
                "gl": "de",
                "api_key": self._api_key,
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                logger.warning("[SerpApi] Quota exceeded or rate limited (HTTP 429). Pausing SerpApi.")
                self._quota_exhausted = True
                return results

            if resp.status_code != 200:
                logger.warning("[SerpApi] Request failed with status {}: {}", resp.status_code, resp.text[:200])
                return results

            data = resp.json()
            if "error" in data:
                err_msg = str(data["error"])
                if "quota" in err_msg.lower() or "searches" in err_msg.lower():
                    logger.warning("[SerpApi] Monthly searches quota exhausted: {}", err_msg)
                    self._quota_exhausted = True
                else:
                    logger.info("[SerpApi] Google response note: {} (Google for Jobs is suppressed in Germany under EU DMA)", err_msg)
                return results

            jobs_raw = data.get("jobs_results", [])
            for j in jobs_raw[:limit]:
                job_id = str(j.get("job_id") or "").strip()
                title = str(j.get("title") or "").strip()
                company = str(j.get("company_name") or "").strip()
                if not title or not company:
                    continue
                if not job_id:
                    job_id = str(hash(f"{company}_{title}"))

                apply_links = [opt.get("link") for opt in j.get("apply_options", []) if opt.get("link")]
                url_apply = apply_links[0] if apply_links else f"https://www.google.com/search?q={keyword}&ibp=htl;jobs"

                results.append({
                    "source": "serpapi",
                    "external_id": job_id,
                    "title": title,
                    "company_name": company,
                    "location": str(j.get("location") or location).strip(),
                    "url": url_apply,
                    "description": str(j.get("description") or "").strip(),
                    "salary_info": None,
                    "posted_at": None,
                    "search_keyword": keyword,
                })
            logger.info("[SerpApi] Found {} results for '{}'", len(results), keyword)
        except Exception as e:
            logger.error("[SerpApi] Error searching for '{}': {}", keyword, e)

        return results


class RapidApiProvider(BaseProvider):
    """Multi-board job search via RapidAPI JSearch."""

    name: str = "rapidapi"

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.getenv("RAPIDAPI_KEY") or os.getenv("RAPID_API_KEY")
        if not key:
            try:
                from job_search.core.config import load_secrets
                key = load_secrets().rapidapi_key
            except Exception:
                pass
        self._api_key = key
        self._quota_exhausted = False

    def search(self, keyword: str, location: str, limit: int = 15) -> list[dict[str, Any]]:
        results = []
        if not self._api_key:
            logger.warning("[RapidAPI] No RAPIDAPI_KEY found; skipping provider.")
            return results
        if self._quota_exhausted:
            logger.warning("[RapidAPI] Quota previously exhausted; skipping search for '{}'.", keyword)
            return results

        try:
            logger.info("[RapidAPI] Querying JSearch for '{}' in '{}'...", keyword, location)
            url = "https://jsearch.p.rapidapi.com/search-v2"
            headers = {
                "X-RapidAPI-Key": self._api_key,
                "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
            }
            params = {
                "query": f"{keyword} in {location}",
                "country": "de" if ("germany" in location.lower() or "deutschland" in location.lower()) else "us",
                "num_pages": "1",
            }
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code in (401, 403, 404, 429):
                logger.warning(
                    "[RapidAPI] JSearch request returned status {}: {}. Pausing RapidAPI provider for this run.",
                    resp.status_code,
                    resp.text[:200],
                )
                self._quota_exhausted = True
                return results

            if resp.status_code != 200:
                logger.warning("[RapidAPI] Request failed with status {}: {}", resp.status_code, resp.text[:200])
                return results

            data = resp.json()
            raw_data = data.get("data")
            if isinstance(raw_data, dict):
                jobs_raw = raw_data.get("jobs", [])
            elif isinstance(raw_data, list):
                jobs_raw = raw_data
            else:
                jobs_raw = data.get("jobs", [])
            for j in jobs_raw[:limit]:
                job_id = str(j.get("job_id") or "").strip()
                title = str(j.get("job_title") or "").strip()
                company = str(j.get("employer_name") or "").strip()
                if not title or not company:
                    continue
                if not job_id:
                    job_id = str(hash(f"{company}_{title}"))

                city = j.get("job_city", "")
                country = j.get("job_country", "")
                loc_str = f"{city}, {country}".strip(", ") or location

                results.append({
                    "source": "rapidapi",
                    "external_id": job_id,
                    "title": title,
                    "company_name": company,
                    "location": loc_str,
                    "url": j.get("job_apply_link") or j.get("job_google_link"),
                    "description": str(j.get("job_description") or "").strip(),
                    "salary_info": None,
                    "posted_at": str(j.get("job_posted_at_datetime_utc") or "").strip() or None,
                    "search_keyword": keyword,
                })
            logger.info("[RapidAPI] Found {} results for '{}'", len(results), keyword)
        except Exception as e:
            logger.error("[RapidAPI] Error searching for '{}': {}", keyword, e)

        return results
