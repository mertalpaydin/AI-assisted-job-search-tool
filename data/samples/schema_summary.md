# Database Schema Summary

**Generated:** 2026-04-03 12:58:30

## Overview

- **Total fields:** 87
- **Job fields:** 77
- **Company fields:** 10
- **Media files excluded:** Yes

## Tables

1. **jobs** - Main job postings table
2. **companies** - Company information
3. **screening_results** - AI screening results
4. **cover_letters** - Generated cover letters
5. **processing_state** - Job processing status tracking
6. **api_usage** - API usage tracking for rate limiting

## Key Job Fields

- **dashEntityUrn** (TEXT)
- **entityUrn** (TEXT)
- **title** (TEXT)
- **description** (TEXT)
- **formattedLocation** (TEXT)
- **workRemoteAllowed** (INTEGER)
- **workplaceTypes** (TEXT)
- **formattedEmploymentStatus** (TEXT)
- **formattedExperienceLevel** (TEXT)
- **jobPostingUrl** (TEXT)
- **applies** (INTEGER)
- **views** (INTEGER)
- **listedAt** (INTEGER)
- **expireAt** (INTEGER)

## Key Company Fields

- **name** (TEXT)
- **headquarter** (TEXT)
- **staffCount** (INTEGER)
- **staffCountRange** (TEXT)
- **url** (TEXT)

## Files Generated

- SQL Schema: `final_schema.sql`
- Field Mappings: `field_mappings.json`
- This Summary: `schema_summary.md`
