-- AI-Assisted Job Search Tool - Database Schema
-- Generated: 2026-04-03T12:58:30.654782
-- Total fields: 87 (excluding media files)

-- Jobs Table
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER UNIQUE NOT NULL,  -- LinkedIn job ID
    scraped INTEGER NOT NULL DEFAULT 0,  -- 0=pending, 1=complete, -1=error
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Search metadata
    search_keyword TEXT,
    search_location_id INTEGER,

    -- Job details
    $recipeTypes TEXT,
    *allJobHiringTeamMembersInjectionResult TEXT,
    *applyingInfo TEXT,
    *employmentStatusResolutionResult TEXT,
    *savingInfo TEXT,
    *standardizedTitleResolutionResult TEXT,
    allowedToEdit INTEGER,
    appeal TEXT,
    applicantTrackingSystem TEXT,
    applies INTEGER,
    applyMethod TEXT,
    benefits TEXT,
    benefitsDataSource TEXT,
    claimableByViewer INTEGER,
    closedAt TEXT,
    companyDescription TEXT,
    companyDetails TEXT,
    contentSource TEXT,
    country TEXT,
    dashEntityUrn TEXT,
    dashJobPostingCardUrn TEXT,
    degreeMatches TEXT,
    description TEXT,
    draftApplicationInfo TEXT,
    eligibleForLearningCourseRecsUpsell INTEGER,
    eligibleForReferrals INTEGER,
    eligibleForSharingProfileWithPoster INTEGER,
    employmentStatus TEXT,
    encryptedPricingParams TEXT,
    entityUrn TEXT,
    expireAt INTEGER,
    formattedEmploymentStatus TEXT,
    formattedExperienceLevel TEXT,
    formattedIndustries TEXT,
    formattedJobFunctions TEXT,
    formattedLocation TEXT,
    hiringDashboardViewEnabled INTEGER,
    hiringTeamEntitlements TEXT,
    industries TEXT,
    inferredBenefits TEXT,
    jobApplicationLimitReached INTEGER,
    jobFunctions TEXT,
    jobPosterEntitlements TEXT,
    jobPostingId INTEGER,
    jobPostingUrl TEXT,
    jobRegion TEXT,
    jobState TEXT,
    listedAt INTEGER,
    locationUrn TEXT,
    locationVisibility TEXT,
    matchType TEXT,
    messagingStatus TEXT,
    messagingToken TEXT,
    new INTEGER,
    originalListedAt INTEGER,
    ownerViewEnabled INTEGER,
    postalAddress TEXT,
    poster TEXT,
    repostedJobPosting TEXT,
    salaryInsights TEXT,
    skillMatches TEXT,
    skillsDescription TEXT,
    sourceDomain TEXT,
    standardizedAddresses TEXT,
    standardizedTitle TEXT,
    talentHubJob INTEGER,
    thirdPartySourced INTEGER,
    title TEXT,
    trackingPixelUrl TEXT,
    trackingUrn TEXT,
    trustReviewDecision TEXT,
    trustReviewSla TEXT,
    views INTEGER,
    workRemoteAllowed INTEGER,
    workplaceTypes TEXT,
    workplaceTypesResolutionResults TEXT,
    yearsOfExperienceMatch TEXT,

    -- Indexes
    FOREIGN KEY (company_id) REFERENCES companies(id)
);

-- Companies Table
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_urn TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    *followingInfo TEXT,
    headquarter TEXT,
    lcpTreatment INTEGER,
    name TEXT,
    specialities TEXT,
    staffCount INTEGER,
    staffCountRange TEXT,
    universalName TEXT,
    url TEXT,
    viewerFollowingJobsUpdates INTEGER
);

-- AI Screening Results
CREATE TABLE IF NOT EXISTS screening_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    screening_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cv_match_score REAL,  -- 0.0 to 1.0
    german_requirement_level TEXT,  -- 'none', 'low', 'medium', 'high'
    location_match INTEGER,  -- 0=no, 1=yes
    is_selected INTEGER,  -- 0=no, 1=yes
    screening_reasoning TEXT,
    screening_status INTEGER DEFAULT 0,  -- 0=pending, 1=complete, -1=error
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

-- Cover Letters
CREATE TABLE IF NOT EXISTS cover_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    generation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cover_letter_text TEXT,
    gemini_model_used TEXT,
    api_key_index INTEGER,
    generation_status INTEGER DEFAULT 0,  -- 0=pending, 1=complete, -1=error
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

-- Processing State Tracking
CREATE TABLE IF NOT EXISTS processing_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    stage TEXT NOT NULL,  -- 'search', 'details', 'screening', 'cover_letter'
    status TEXT NOT NULL,  -- 'pending', 'in_progress', 'completed', 'error'
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    UNIQUE(job_id, stage)
);

-- API Usage Tracking
CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_index INTEGER,
    request_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    endpoint TEXT,
    success INTEGER,
    error_type TEXT
);

-- Performance Indexes
CREATE INDEX IF NOT EXISTS idx_jobs_scraped ON jobs(scraped);
CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_screening_status ON screening_results(screening_status);
CREATE INDEX IF NOT EXISTS idx_screening_selected ON screening_results(is_selected);
CREATE INDEX IF NOT EXISTS idx_cover_letter_status ON cover_letters(generation_status);
CREATE INDEX IF NOT EXISTS idx_processing_state_stage ON processing_state(stage, status);
CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp ON api_usage(request_timestamp);