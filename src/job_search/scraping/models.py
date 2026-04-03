from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobStub:
    """Minimal job data returned from the search API."""
    job_id: int
    title: str | None
    sponsored: bool


@dataclass
class CompanyData:
    """Company data extracted from the API response's 'included' array."""
    company_urn: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedJobDetails:
    """Fully parsed job details ready for database storage."""
    job_id: int
    job_fields: dict[str, Any]
    company: CompanyData | None
