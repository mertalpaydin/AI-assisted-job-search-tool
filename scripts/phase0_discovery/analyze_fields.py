#!/usr/bin/env python3
"""
Analyze discovered fields and present them organized by importance
"""

import json
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).parent.parent
SAMPLES_DIR = PROJECT_ROOT / 'data' / 'samples'

def analyze_job_structure():
    """Analyze the actual job JSON structure"""

    # Load first sample
    sample_files = list(SAMPLES_DIR.glob('job_*.json'))
    if not sample_files:
        print("No sample files found!")
        return

    with open(sample_files[0], 'r', encoding='utf-8') as f:
        job_data = json.load(f)

    print("=" * 80)
    print("LINKEDIN JOB DATA STRUCTURE ANALYSIS")
    print("=" * 80)

    # Analyze main data fields
    data = job_data.get('data', {})

    print("\n### CRITICAL FIELDS (Always Include) ###\n")

    critical_fields = {
        'Job ID & Tracking': [
            ('dashEntityUrn', data.get('dashEntityUrn')),
            ('entityUrn', data.get('entityUrn')),
            ('trackingUrn', data.get('trackingUrn')),
        ],
        'Basic Information': [
            ('title', data.get('title')),
            ('standardizedTitle', data.get('standardizedTitle')),
            ('description', extract_text(data.get('description'))),
        ],
        'Location & Work Type': [
            ('formattedLocation', data.get('formattedLocation')),
            ('location', data.get('location')),
            ('workRemoteAllowed', data.get('workRemoteAllowed')),
            ('workplaceTypes', data.get('workplaceTypes')),
            ('workplaceTypesResolutionResults', data.get('workplaceTypesResolutionResults')),
        ],
        'Application': [
            ('jobPostingUrl', data.get('jobPostingUrl')),
            ('applyMethod', data.get('applyMethod')),
        ],
    }

    for category, fields in critical_fields.items():
        print(f"\n{category}:")
        for field_name, value in fields:
            value_preview = str(value)[:80] if value else "NULL"
            print(f"  - {field_name}: {value_preview}")

    print("\n\n### USEFUL FIELDS (Recommended for AI Screening & Cover Letters) ###\n")

    useful_fields = {
        'Company Information': [
            ('companyDetails', data.get('companyDetails')),
            ('companyDescription', extract_text(data.get('companyDescription'))),
        ],
        'Job Details': [
            ('formattedEmploymentStatus', data.get('formattedEmploymentStatus')),
            ('employmentStatus', data.get('employmentStatus')),
            ('formattedExperienceLevel', data.get('formattedExperienceLevel')),
        ],
        'Skills & Requirements': [
            ('skillMatches', data.get('skillMatches')),
            ('skills', data.get('skills')),
        ],
        'Compensation': [
            ('salaryInsights', data.get('salaryInsights')),
            ('compensationDescription', extract_text(data.get('compensationDescription'))),
        ],
        'Engagement Metrics': [
            ('applies', data.get('applies')),
            ('views', data.get('views')),
            ('listedAt', data.get('listedAt')),
            ('expireAt', data.get('expireAt')),
        ],
    }

    for category, fields in useful_fields.items():
        print(f"\n{category}:")
        for field_name, value in fields:
            if value is not None:
                value_preview = str(value)[:80] if value else "NULL"
                print(f"  - {field_name}: {value_preview}")

    print("\n\n### OPTIONAL FIELDS (Additional Context) ###\n")

    optional_fields = [
        'posterDashEntityUrn',
        'benefits',
        'employeeCountRange',
        'industries',
        'jobState',
        'premium',
        'sponsored',
    ]

    for field_name in optional_fields:
        value = data.get(field_name)
        if value is not None:
            value_preview = str(value)[:60]
            print(f"  - {field_name}: {value_preview}")

    # Show company data from included section
    print("\n\n### COMPANY DATA (from 'included' section) ###\n")

    included = job_data.get('included', [])
    company_objects = [obj for obj in included if 'company' in obj.get('$type', '').lower()]

    if company_objects:
        company = company_objects[0]
        print("Company fields available:")
        for key in sorted(company.keys()):
            if key != '$type':
                value = company.get(key)
                value_preview = str(value)[:60] if value else "NULL"
                print(f"  - {key}: {value_preview}")

    print("\n\n" + "=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)
    print("""
For your AI-assisted job search tool, I recommend including:

MUST HAVE (for core functionality):
  - Job ID (dashEntityUrn or entityUrn)
  - title
  - description
  - formattedLocation
  - workRemoteAllowed
  - jobPostingUrl
  - applyMethod

HIGHLY RECOMMENDED (for AI screening):
  - company name/details
  - formattedEmploymentStatus (full-time, contract, etc.)
  - formattedExperienceLevel
  - skills/skillMatches
  - description (full job description for German language detection)

RECOMMENDED (for cover letters & tracking):
  - companyDescription
  - salaryInsights
  - applies, views, listedAt (for popularity/freshness)
  - compensationDescription

OPTIONAL (nice to have):
  - benefits
  - industries
  - employeeCountRange
    """)

def extract_text(obj):
    """Extract text from AttributedText objects"""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return obj.get('text', obj.get('attributedText', {}).get('text'))
    return str(obj)

if __name__ == '__main__':
    analyze_job_structure()
