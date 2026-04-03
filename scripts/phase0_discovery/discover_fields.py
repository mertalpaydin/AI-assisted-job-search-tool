#!/usr/bin/env python3
"""
LinkedIn Data Discovery Script

This script:
1. Authenticates to LinkedIn
2. Fetches sample job postings
3. Saves raw JSON responses
4. Analyzes and catalogs all available fields
"""

import sys
import os
import json
import time
import argparse
import getpass
from datetime import datetime
from pathlib import Path

# Save the original directory
ORIGINAL_DIR = os.getcwd()
PROJECT_ROOT = Path(__file__).parent.parent
CODEBASE_DIR = PROJECT_ROOT / 'codebase'

# Change to codebase directory before imports (helpers.py expects relative paths)
os.chdir(CODEBASE_DIR)

# Add the old codebase to path to reuse authentication
sys.path.insert(0, str(CODEBASE_DIR))

try:
    from scripts.fetch import create_session, JobSearchRetriever, JobDetailRetriever
except ImportError as e:
    print(f"Error importing from old codebase: {e}")
    print(f"Make sure the old codebase is located at: {CODEBASE_DIR}")
    os.chdir(ORIGINAL_DIR)
    sys.exit(1)

# Change back to original directory
os.chdir(ORIGINAL_DIR)

def setup_directories():
    """Create necessary directories for samples"""
    samples_dir = PROJECT_ROOT / 'data' / 'samples'
    os.makedirs(samples_dir, exist_ok=True)
    print("[OK] Created data/samples directory")

def get_linkedin_credentials(args):
    """Get LinkedIn credentials from args, env vars, or prompt"""
    print("\n=== LinkedIn Authentication Required ===", flush=True)

    # Try command-line arguments first
    email = args.email if hasattr(args, 'email') and args.email else None
    password = args.password if hasattr(args, 'password') and args.password else None

    # Try environment variables
    if not email:
        email = os.environ.get('LINKEDIN_EMAIL')
    if not password:
        password = os.environ.get('LINKEDIN_PASSWORD')

    # Prompt if still not provided
    if not email:
        print("This script will use your LinkedIn credentials to fetch sample job data.", flush=True)
        print("Your credentials are NOT saved - they're only used for this session.\n", flush=True)
        try:
            sys.stdout.write("LinkedIn email: ")
            sys.stdout.flush()
            email = sys.stdin.readline().strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNo email provided. Exiting...")
            sys.exit(1)

    if not password:
        try:
            # Try getpass first, fall back to regular input if it fails
            try:
                password = getpass.getpass("LinkedIn password: ")
            except (EOFError, AttributeError):
                # Fallback for non-interactive environments
                sys.stdout.write("LinkedIn password: ")
                sys.stdout.flush()
                password = sys.stdin.readline().strip()
        except KeyboardInterrupt:
            print("\nNo password provided. Exiting...")
            sys.exit(1)

    if not email or not password:
        print("[ERROR] Email and password are required", flush=True)
        sys.exit(1)

    return email, password

def create_temporary_logins_file(email, password):
    """Create a temporary logins.csv file"""
    # Save to old codebase directory where fetch.py expects it
    codebase_path = Path(__file__).parent.parent / 'codebase'
    logins_path = codebase_path / 'logins.csv'

    # Backup existing file if it exists
    if logins_path.exists():
        backup_path = codebase_path / f'logins_backup_{int(time.time())}.csv'
        os.rename(logins_path, backup_path)
        print(f"[OK] Backed up existing logins.csv to {backup_path.name}")

    # Create temporary logins file
    with open(logins_path, 'w') as f:
        f.write("emails,passwords,method\n")
        f.write(f"{email},{password},search\n")
        f.write(f"{email},{password},details\n")

    print("[OK] Created temporary authentication file")
    return logins_path

def fetch_sample_jobs(num_samples=3):
    """
    Fetch sample job postings

    Args:
        num_samples: Number of job samples to fetch

    Returns:
        list: List of (job_id, job_details_json) tuples
    """
    print("\n=== Fetching Sample Jobs ===")

    # Change to codebase directory for imports to work
    original_dir = os.getcwd()
    codebase_dir = Path(__file__).parent.parent / 'codebase'
    os.chdir(codebase_dir)

    try:
        # Step 1: Search for jobs to get job IDs
        print("1. Searching for Python Developer jobs in Germany...", flush=True)
        try:
            searcher = JobSearchRetriever(
                keyword="Python Developer",
                geo_id=103883259  # Germany geo_id from old config
            )
            print("   Created searcher, calling get_jobs()...", flush=True)
        except Exception as e:
            print(f"   [ERROR] Failed to create searcher: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise

        try:
            job_ids_dict = searcher.get_jobs()
            print(f"   API returned {len(job_ids_dict)} jobs", flush=True)
        except Exception as e:
            print(f"   [ERROR] Failed to get jobs: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise

        job_ids = list(job_ids_dict.keys())[:num_samples]

        print(f"   Found {len(job_ids_dict)} jobs, selecting first {num_samples}", flush=True)
        print(f"   Job IDs: {job_ids}", flush=True)

        if not job_ids:
            print("   [WARNING] No job IDs found!", flush=True)
            return []

        # Step 2: Fetch detailed information for these jobs
        print("\n2. Fetching detailed information...", flush=True)
        try:
            retriever = JobDetailRetriever()
            print("   Created detail retriever, fetching details...", flush=True)
        except Exception as e:
            print(f"   [ERROR] Failed to create detail retriever: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise

        try:
            job_details = retriever.get_job_details(job_ids)
            print(f"   Retrieved details for {len(job_details)} jobs", flush=True)
        except Exception as e:
            print(f"   [ERROR] Failed to get job details: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise

        # Return as list of tuples
        samples = []
        for job_id, details in job_details.items():
            if details != -1:  # -1 indicates error
                samples.append((job_id, details))
            else:
                print(f"   [WARNING] Job {job_id} had an error", flush=True)

        print(f"   Successfully retrieved {len(samples)} job details", flush=True)
        return samples

    except Exception as e:
        print(f"\n[ERROR] Exception during fetch: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return []
    finally:
        # Change back to original directory
        os.chdir(original_dir)

def save_samples(samples):
    """Save sample JSON responses to files"""
    print("\n=== Saving Sample Data ===")

    saved_files = []
    samples_dir = PROJECT_ROOT / 'data' / 'samples'

    for i, (job_id, details) in enumerate(samples, 1):
        filename = samples_dir / f"job_{job_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(details, f, indent=2, ensure_ascii=False)

        print(f"{i}. Saved {filename}")
        saved_files.append(str(filename))

    return saved_files

def extract_field_paths(json_obj, prefix='$', path_dict=None, parent_type=None):
    """
    Recursively extract all JSON paths from a nested JSON object

    Args:
        json_obj: JSON object to analyze
        prefix: Current JSON path prefix
        path_dict: Dictionary to store paths
        parent_type: Type identifier from parent object

    Returns:
        dict: Dictionary of {path: (type, example_value)}
    """
    if path_dict is None:
        path_dict = {}

    if isinstance(json_obj, dict):
        # Record the type if present
        obj_type = json_obj.get('$type', parent_type)

        for key, value in json_obj.items():
            current_path = f"{prefix}.{key}"

            # Determine value type
            if value is None:
                value_type = 'NULL'
            elif isinstance(value, bool):
                value_type = 'BOOLEAN'
            elif isinstance(value, int):
                value_type = 'INTEGER'
            elif isinstance(value, float):
                value_type = 'FLOAT'
            elif isinstance(value, str):
                value_type = 'TEXT'
            elif isinstance(value, list):
                value_type = 'ARRAY'
            elif isinstance(value, dict):
                value_type = 'OBJECT'
            else:
                value_type = 'UNKNOWN'

            # Get example value (truncate if too long)
            example = str(value)[:100] if value is not None else None

            # Store the path
            if current_path not in path_dict:
                path_dict[current_path] = {
                    'type': value_type,
                    'example': example,
                    'object_type': obj_type
                }

            # Recursively process nested objects and arrays
            if isinstance(value, dict):
                extract_field_paths(value, current_path, path_dict, obj_type)
            elif isinstance(value, list) and len(value) > 0:
                # Process first item in array as example
                extract_field_paths(value[0], f"{current_path}[0]", path_dict, obj_type)

    elif isinstance(json_obj, list):
        for i, item in enumerate(json_obj):
            current_path = f"{prefix}[{i}]"
            extract_field_paths(item, current_path, path_dict, parent_type)

    return path_dict

def analyze_samples(sample_files):
    """Analyze sample JSON files and create field catalog"""
    print("\n=== Analyzing Field Structure ===")

    all_paths = {}

    for filename in sample_files:
        print(f"Processing {filename}...")
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)

        paths = extract_field_paths(data)

        # Merge with all_paths
        for path, info in paths.items():
            if path not in all_paths:
                all_paths[path] = info

    print(f"[OK] Found {len(all_paths)} unique field paths")
    return all_paths

def categorize_fields(paths):
    """Categorize fields by their purpose"""
    categories = {
        'identification': [],
        'basic_info': [],
        'location_work': [],
        'compensation': [],
        'application': [],
        'engagement': [],
        'requirements': [],
        'company_info': [],
        'metadata': [],
        'other': []
    }

    for path, info in paths.items():
        path_lower = path.lower()

        # Categorization logic
        if any(x in path_lower for x in ['job_id', 'entityurn', 'reference', 'trackingurn']):
            categories['identification'].append((path, info))
        elif any(x in path_lower for x in ['title', 'description', 'skill']):
            categories['basic_info'].append((path, info))
        elif any(x in path_lower for x in ['location', 'remote', 'work', 'onsite', 'hybrid']):
            categories['location_work'].append((path, info))
        elif any(x in path_lower for x in ['salary', 'compensation', 'benefit', 'pay']):
            categories['compensation'].append((path, info))
        elif any(x in path_lower for x in ['apply', 'application', 'url']):
            categories['application'].append((path, info))
        elif any(x in path_lower for x in ['view', 'applies', 'applicant', 'listed']):
            categories['engagement'].append((path, info))
        elif any(x in path_lower for x in ['experience', 'education', 'requirement', 'qualification']):
            categories['requirements'].append((path, info))
        elif any(x in path_lower for x in ['company', 'employer', 'organization']):
            categories['company_info'].append((path, info))
        elif any(x in path_lower for x in ['$type', 'metadata', 'decorationid']):
            categories['metadata'].append((path, info))
        else:
            categories['other'].append((path, info))

    return categories

def save_field_catalog(paths, categories):
    """Save field catalog to YAML file"""
    print("\n=== Creating Field Catalog ===")

    catalog_file = PROJECT_ROOT / 'data' / 'samples' / 'field_catalog.yaml'

    with open(catalog_file, 'w', encoding='utf-8') as f:
        f.write("# LinkedIn Job Data Field Catalog\n")
        f.write(f"# Generated: {datetime.now().isoformat()}\n")
        f.write(f"# Total fields: {len(paths)}\n\n")

        for category, fields in categories.items():
            if not fields:
                continue

            f.write(f"\n{category}:\n")
            f.write(f"  # {len(fields)} fields in this category\n\n")

            for path, info in sorted(fields, key=lambda x: x[0]):
                # Determine usefulness rating (heuristic)
                usefulness = 'optional'
                path_lower = path.lower()

                if any(x in path_lower for x in ['job_id', 'title', 'description', 'location', 'application']):
                    usefulness = 'critical'
                elif any(x in path_lower for x in ['remote', 'work', 'skill', 'salary', 'company', 'experience']):
                    usefulness = 'useful'

                f.write(f"  - name: \"{path.split('.')[-1]}\"\n")
                f.write(f"    path: \"{path}\"\n")
                f.write(f"    type: \"{info['type']}\"\n")
                if info['example']:
                    # Escape quotes and newlines in example
                    example = info['example'].replace('\\n', ' ').replace('"', '\\"')
                    f.write(f"    example: \"{example}\"\n")
                f.write(f"    category: \"{category}\"\n")
                f.write(f"    usefulness: \"{usefulness}\"\n")
                if info.get('object_type'):
                    f.write(f"    object_type: \"{info['object_type']}\"\n")
                f.write("\n")

    print(f"[OK] Saved field catalog to {catalog_file}")
    return catalog_file

def create_summary_report(sample_files, catalog_file, categories):
    """Create a summary report"""
    print("\n=== Creating Summary Report ===")

    report_file = PROJECT_ROOT / 'data' / 'samples' / 'discovery_report.md'

    with open(report_file, 'w', encoding='utf-8') as f:
        f.write("# LinkedIn Data Discovery Report\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write("## Summary\n\n")
        f.write(f"- **Sample jobs fetched:** {len(sample_files)}\n")
        total_fields = sum(len(fields) for fields in categories.values())
        f.write(f"- **Total unique fields:** {total_fields}\n\n")

        f.write("## Field Distribution by Category\n\n")
        f.write("| Category | Field Count |\n")
        f.write("|----------|-------------|\n")
        for category, fields in categories.items():
            f.write(f"| {category.replace('_', ' ').title()} | {len(fields)} |\n")

        f.write("\n## Sample Files\n\n")
        for i, filename in enumerate(sample_files, 1):
            size_kb = os.path.getsize(filename) / 1024
            f.write(f"{i}. `{filename}` ({size_kb:.1f} KB)\n")

        f.write(f"\n## Field Catalog\n\n")
        f.write(f"Comprehensive field catalog saved to: `{catalog_file}`\n\n")

        f.write("## Next Steps\n\n")
        f.write("1. Review the field catalog in `field_catalog.yaml`\n")
        f.write("2. Select which fields you want to include in the database\n")
        f.write("3. Run the schema generation script to create the final database design\n")

    print(f"[OK] Saved summary report to {report_file}")
    return report_file

def cleanup_temp_files(logins_path):
    """Remove temporary files"""
    try:
        if logins_path and os.path.exists(logins_path):
            os.remove(logins_path)
            print("[OK] Removed temporary authentication file")
    except Exception as e:
        print(f"Warning: Could not remove temporary file {logins_path}: {e}")

def main():
    """Main discovery process"""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description='LinkedIn Data Discovery Script')
    parser.add_argument('--email', help='LinkedIn email address')
    parser.add_argument('--password', help='LinkedIn password')
    parser.add_argument('--samples', type=int, default=3, help='Number of job samples to fetch (default: 3)')
    args = parser.parse_args()

    print("=" * 60)
    print("LinkedIn Data Discovery Script")
    print("=" * 60)

    logins_path = None

    try:
        # Step 1: Setup
        setup_directories()

        # Step 2: Get credentials
        email, password = get_linkedin_credentials(args)

        # Step 3: Create temporary logins file
        logins_path = create_temporary_logins_file(email, password)

        # Step 4: Fetch sample jobs
        samples = fetch_sample_jobs(num_samples=3)

        if not samples:
            print("\n[ERROR] Failed to fetch any job samples")
            return 1

        # Step 5: Save samples
        sample_files = save_samples(samples)

        # Step 6: Analyze field structure
        all_paths = analyze_samples(sample_files)

        # Step 7: Categorize fields
        categories = categorize_fields(all_paths)

        # Step 8: Save field catalog
        catalog_file = save_field_catalog(all_paths, categories)

        # Step 9: Create summary report
        report_file = create_summary_report(sample_files, catalog_file, categories)

        # Success!
        print("\n" + "=" * 60)
        print("[OK] Discovery Complete!")
        print("=" * 60)
        print(f"\nResults:")
        print(f"  - Sample data: data/samples/job_*.json")
        print(f"  - Field catalog: {catalog_file}")
        print(f"  - Summary report: {report_file}")
        print(f"\nNext: Review the field catalog and select which fields to include in your database.")

        return 0

    except KeyboardInterrupt:
        print("\n\n[ERROR] Discovery cancelled by user")
        return 1
    except Exception as e:
        print(f"\n\n[ERROR] Error during discovery: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # Cleanup
        if logins_path:
            cleanup_temp_files(logins_path)

if __name__ == '__main__':
    sys.exit(main())
