#!/usr/bin/env python3
"""
Generate final database schema based on discovered fields
Excludes media files (logos, images, etc.)
"""

import json
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent.parent
SAMPLES_DIR = PROJECT_ROOT / 'data' / 'samples'
OUTPUT_DIR = PROJECT_ROOT / 'data' / 'samples'

# Fields to exclude (media files and unnecessary tracking)
EXCLUDED_KEYWORDS = [
    'logo', 'image', 'photo', 'picture', 'icon', 'avatar',
    'backgroundcover', 'digitalmedia', 'media', 'cropinfo',
    'asset', 'coverphoto'
]

def should_exclude_field(field_name, field_path):
    """Check if field should be excluded (media files)"""
    field_lower = field_name.lower()
    path_lower = field_path.lower()

    for keyword in EXCLUDED_KEYWORDS:
        if keyword in field_lower or keyword in path_lower:
            return True
    return False

def get_sql_type(json_type, field_name=''):
    """Map JSON type to SQL type"""
    type_mapping = {
        'TEXT': 'TEXT',
        'INTEGER': 'INTEGER',
        'FLOAT': 'REAL',
        'BOOLEAN': 'INTEGER',  # SQLite uses INTEGER for boolean
        'NULL': 'TEXT',  # Allow NULL fields as TEXT
        'ARRAY': 'TEXT',  # Store as JSON string
        'OBJECT': 'TEXT',  # Store as JSON string
    }

    # Special cases
    if 'date' in field_name.lower() or 'time' in field_name.lower() or field_name in ['listedAt', 'expireAt']:
        return 'INTEGER'  # Unix timestamp

    return type_mapping.get(json_type, 'TEXT')

def analyze_all_samples():
    """Analyze all sample files to get complete field list"""
    all_fields = {}

    sample_files = list(SAMPLES_DIR.glob('job_*.json'))

    for sample_file in sample_files:
        with open(sample_file, 'r', encoding='utf-8') as f:
            job_data = json.load(f)

        # Extract fields from data section
        data = job_data.get('data', {})
        extract_fields(data, 'data', all_fields)

        # Extract fields from included section (company data, etc.)
        included = job_data.get('included', [])
        for idx, obj in enumerate(included):
            obj_type = obj.get('$type', '')
            if 'company' in obj_type.lower():
                extract_fields(obj, f'included_company', all_fields, is_company=True)

    return all_fields

def extract_fields(obj, prefix, fields_dict, is_company=False):
    """Recursively extract field information"""
    if not isinstance(obj, dict):
        return

    for key, value in obj.items():
        if key == '$type':
            continue

        field_path = f"{prefix}.{key}"
        field_name = key

        # Skip excluded fields
        if should_exclude_field(field_name, field_path):
            continue

        # Determine type
        if value is None:
            json_type = 'NULL'
        elif isinstance(value, bool):
            json_type = 'BOOLEAN'
        elif isinstance(value, int):
            json_type = 'INTEGER'
        elif isinstance(value, float):
            json_type = 'REAL'
        elif isinstance(value, str):
            json_type = 'TEXT'
        elif isinstance(value, list):
            json_type = 'ARRAY'
        elif isinstance(value, dict):
            # For nested objects, check if it's text content
            if 'text' in value:
                json_type = 'TEXT'
            else:
                json_type = 'OBJECT'
        else:
            json_type = 'TEXT'

        # Store field info
        if field_name not in fields_dict:
            fields_dict[field_name] = {
                'path': field_path,
                'type': json_type,
                'sql_type': get_sql_type(json_type, field_name),
                'is_company': is_company,
                'example': str(value)[:100] if value is not None else None
            }

def generate_sql_schema(fields_dict):
    """Generate SQL CREATE TABLE statements"""
    sql = []

    # Separate job fields and company fields
    job_fields = {k: v for k, v in fields_dict.items() if not v['is_company']}
    company_fields = {k: v for k, v in fields_dict.items() if v['is_company']}

    # Jobs table
    sql.append("-- Jobs Table")
    sql.append("CREATE TABLE IF NOT EXISTS jobs (")
    sql.append("    id INTEGER PRIMARY KEY AUTOINCREMENT,")
    sql.append("    job_id INTEGER UNIQUE NOT NULL,  -- LinkedIn job ID")
    sql.append("    scraped INTEGER NOT NULL DEFAULT 0,  -- 0=pending, 1=complete, -1=error")
    sql.append("    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,")
    sql.append("    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,")
    sql.append("")
    sql.append("    -- Search metadata")
    sql.append("    search_keyword TEXT,")
    sql.append("    search_location_id INTEGER,")
    sql.append("")

    # Add job fields
    sql.append("    -- Job details")
    for field_name, field_info in sorted(job_fields.items()):
        if field_name not in ['id', 'job_id', 'scraped']:
            sql.append(f"    {field_name} {field_info['sql_type']},")

    sql.append("")
    sql.append("    -- Indexes")
    sql.append("    FOREIGN KEY (company_id) REFERENCES companies(id)")
    sql.append(");")
    sql.append("")

    # Companies table
    sql.append("-- Companies Table")
    sql.append("CREATE TABLE IF NOT EXISTS companies (")
    sql.append("    id INTEGER PRIMARY KEY AUTOINCREMENT,")
    sql.append("    company_urn TEXT UNIQUE NOT NULL,")
    sql.append("    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,")
    sql.append("    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,")
    sql.append("")

    for field_name, field_info in sorted(company_fields.items()):
        if field_name not in ['id', 'entityUrn']:
            sql.append(f"    {field_name} {field_info['sql_type']},")

    sql[-1] = sql[-1].rstrip(',')  # Remove trailing comma
    sql.append(");")
    sql.append("")

    # Screening results table (from plan)
    sql.append("-- AI Screening Results")
    sql.append("CREATE TABLE IF NOT EXISTS screening_results (")
    sql.append("    id INTEGER PRIMARY KEY AUTOINCREMENT,")
    sql.append("    job_id INTEGER NOT NULL,")
    sql.append("    screening_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,")
    sql.append("    cv_match_score REAL,  -- 0.0 to 1.0")
    sql.append("    german_requirement_level TEXT,  -- 'none', 'low', 'medium', 'high'")
    sql.append("    location_match INTEGER,  -- 0=no, 1=yes")
    sql.append("    is_selected INTEGER,  -- 0=no, 1=yes")
    sql.append("    screening_reasoning TEXT,")
    sql.append("    screening_status INTEGER DEFAULT 0,  -- 0=pending, 1=complete, -1=error")
    sql.append("    FOREIGN KEY (job_id) REFERENCES jobs(job_id)")
    sql.append(");")
    sql.append("")

    # Cover letters table
    sql.append("-- Cover Letters")
    sql.append("CREATE TABLE IF NOT EXISTS cover_letters (")
    sql.append("    id INTEGER PRIMARY KEY AUTOINCREMENT,")
    sql.append("    job_id INTEGER NOT NULL,")
    sql.append("    generation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,")
    sql.append("    cover_letter_text TEXT,")
    sql.append("    gemini_model_used TEXT,")
    sql.append("    api_key_index INTEGER,")
    sql.append("    generation_status INTEGER DEFAULT 0,  -- 0=pending, 1=complete, -1=error")
    sql.append("    error_message TEXT,")
    sql.append("    retry_count INTEGER DEFAULT 0,")
    sql.append("    FOREIGN KEY (job_id) REFERENCES jobs(job_id)")
    sql.append(");")
    sql.append("")

    # Processing state table
    sql.append("-- Processing State Tracking")
    sql.append("CREATE TABLE IF NOT EXISTS processing_state (")
    sql.append("    id INTEGER PRIMARY KEY AUTOINCREMENT,")
    sql.append("    job_id INTEGER NOT NULL,")
    sql.append("    stage TEXT NOT NULL,  -- 'search', 'details', 'screening', 'cover_letter'")
    sql.append("    status TEXT NOT NULL,  -- 'pending', 'in_progress', 'completed', 'error'")
    sql.append("    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,")
    sql.append("    error_message TEXT,")
    sql.append("    retry_count INTEGER DEFAULT 0,")
    sql.append("    FOREIGN KEY (job_id) REFERENCES jobs(job_id),")
    sql.append("    UNIQUE(job_id, stage)")
    sql.append(");")
    sql.append("")

    # API usage tracking
    sql.append("-- API Usage Tracking")
    sql.append("CREATE TABLE IF NOT EXISTS api_usage (")
    sql.append("    id INTEGER PRIMARY KEY AUTOINCREMENT,")
    sql.append("    api_key_index INTEGER,")
    sql.append("    request_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,")
    sql.append("    endpoint TEXT,")
    sql.append("    success INTEGER,")
    sql.append("    error_type TEXT")
    sql.append(");")
    sql.append("")

    # Indexes
    sql.append("-- Performance Indexes")
    sql.append("CREATE INDEX IF NOT EXISTS idx_jobs_scraped ON jobs(scraped);")
    sql.append("CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id);")
    sql.append("CREATE INDEX IF NOT EXISTS idx_screening_status ON screening_results(screening_status);")
    sql.append("CREATE INDEX IF NOT EXISTS idx_screening_selected ON screening_results(is_selected);")
    sql.append("CREATE INDEX IF NOT EXISTS idx_cover_letter_status ON cover_letters(generation_status);")
    sql.append("CREATE INDEX IF NOT EXISTS idx_processing_state_stage ON processing_state(stage, status);")
    sql.append("CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp ON api_usage(request_timestamp);")

    return '\n'.join(sql)

def generate_field_mappings(fields_dict):
    """Generate JSON path mappings for field extraction"""
    mappings = {
        'job_fields': [],
        'company_fields': []
    }

    for field_name, field_info in sorted(fields_dict.items()):
        mapping = {
            'field_name': field_name,
            'json_path': field_info['path'],
            'type': field_info['type'],
            'sql_type': field_info['sql_type']
        }

        if field_info['is_company']:
            mappings['company_fields'].append(mapping)
        else:
            mappings['job_fields'].append(mapping)

    return mappings

def main():
    """Generate final schema and mappings"""
    print("=" * 80)
    print("GENERATING FINAL DATABASE SCHEMA")
    print("=" * 80)

    # Analyze all samples
    print("\n1. Analyzing sample job data...")
    all_fields = analyze_all_samples()
    print(f"   Found {len(all_fields)} fields (excluding media files)")

    job_fields = {k: v for k, v in all_fields.items() if not v['is_company']}
    company_fields = {k: v for k, v in all_fields.items() if v['is_company']}
    print(f"   - Job fields: {len(job_fields)}")
    print(f"   - Company fields: {len(company_fields)}")

    # Generate SQL schema
    print("\n2. Generating SQL schema...")
    sql_schema = generate_sql_schema(all_fields)

    # Save SQL schema
    schema_file = OUTPUT_DIR / 'final_schema.sql'
    with open(schema_file, 'w', encoding='utf-8') as f:
        f.write(f"-- AI-Assisted Job Search Tool - Database Schema\n")
        f.write(f"-- Generated: {datetime.now().isoformat()}\n")
        f.write(f"-- Total fields: {len(all_fields)} (excluding media files)\n\n")
        f.write(sql_schema)

    print(f"   Saved to: {schema_file}")

    # Generate field mappings
    print("\n3. Generating field extraction mappings...")
    mappings = generate_field_mappings(all_fields)

    # Save mappings as JSON
    mappings_file = OUTPUT_DIR / 'field_mappings.json'
    with open(mappings_file, 'w', encoding='utf-8') as f:
        json.dump(mappings, f, indent=2)

    print(f"   Saved to: {mappings_file}")

    # Generate summary report
    print("\n4. Creating summary report...")
    summary_file = OUTPUT_DIR / 'schema_summary.md'

    with open(summary_file, 'w', encoding='utf-8') as f:
        f.write("# Database Schema Summary\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write("## Overview\n\n")
        f.write(f"- **Total fields:** {len(all_fields)}\n")
        f.write(f"- **Job fields:** {len(job_fields)}\n")
        f.write(f"- **Company fields:** {len(company_fields)}\n")
        f.write(f"- **Media files excluded:** Yes\n\n")

        f.write("## Tables\n\n")
        f.write("1. **jobs** - Main job postings table\n")
        f.write("2. **companies** - Company information\n")
        f.write("3. **screening_results** - AI screening results\n")
        f.write("4. **cover_letters** - Generated cover letters\n")
        f.write("5. **processing_state** - Job processing status tracking\n")
        f.write("6. **api_usage** - API usage tracking for rate limiting\n\n")

        f.write("## Key Job Fields\n\n")
        key_fields = [
            'dashEntityUrn', 'entityUrn', 'title', 'description',
            'formattedLocation', 'workRemoteAllowed', 'workplaceTypes',
            'formattedEmploymentStatus', 'formattedExperienceLevel',
            'jobPostingUrl', 'applies', 'views', 'listedAt', 'expireAt'
        ]

        for field in key_fields:
            if field in job_fields:
                f.write(f"- **{field}** ({job_fields[field]['sql_type']})\n")

        f.write("\n## Key Company Fields\n\n")
        key_company_fields = [
            'name', 'description', 'headquarter', 'industries',
            'staffCount', 'staffCountRange', 'url'
        ]

        for field in key_company_fields:
            if field in company_fields:
                f.write(f"- **{field}** ({company_fields[field]['sql_type']})\n")

        f.write("\n## Files Generated\n\n")
        f.write(f"- SQL Schema: `{schema_file.name}`\n")
        f.write(f"- Field Mappings: `{mappings_file.name}`\n")
        f.write(f"- This Summary: `{summary_file.name}`\n")

    print(f"   Saved to: {summary_file}")

    print("\n" + "=" * 80)
    print("[OK] Schema Generation Complete!")
    print("=" * 80)
    print("\nNext steps:")
    print("  1. Review the generated schema: data/samples/final_schema.sql")
    print("  2. Review field mappings: data/samples/field_mappings.json")
    print("  3. Proceed to Phase 1: Project Setup")

if __name__ == '__main__':
    main()
