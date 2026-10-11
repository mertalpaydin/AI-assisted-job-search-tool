"""
Unit tests for open-vocabulary skill extraction, garbage filtering,
and spelling normalization.
"""
import pytest

from job_search.skills.filter_pipeline import SkillFilterPipeline, clean_candidate_token
from job_search.skills.harvester import SkillHarvester
from job_search.skills.normalizer import SkillNormalizer
from job_search.skills.open_extractor import OpenSkillExtractor


# ---------------------------------------------------------------------------
# 1. Spelling Normalization Tests
# ---------------------------------------------------------------------------

def test_spelling_normalization_ab_testing():
    normalizer = SkillNormalizer()
    variants = [
        "A/B testing", "A/B-testing", "a/b test", "ab testing",
        "split testing", "multivariate testing", "A/B-TESTING"
    ]
    for v in variants:
        assert normalizer.normalize(v) == "A/B Testing", f"Failed for variant: {v}"


def test_spelling_normalization_technical_stack():
    normalizer = SkillNormalizer()
    cases = {
        "scikit-learn": "Scikit-Learn",
        "sklearn": "Scikit-Learn",
        "power bi": "Power BI",
        "powerbi": "Power BI",
        "Power-BI": "Power BI",
        "ci/cd": "CI/CD",
        "ci-cd": "CI/CD",
        "continuous integration": "CI/CD",
        "k8s": "Kubernetes",
        "kubernetes": "Kubernetes",
        "postgres": "PostgreSQL",
        "postgresql": "PostgreSQL",
        "node.js": "Node.js",
        "nodejs": "Node.js",
        "genai": "Generative AI",
        "generative ai": "Generative AI",
        "llms": "Large Language Models (LLMs)",
        "aws": "AWS",
        "gcp": "GCP",
        "pytorch": "PyTorch",
        "tensorflow": "TensorFlow",
        "c++": "C++",
        "c#": "C#",
        ".net": ".NET",
    }
    for variant, expected in cases.items():
        assert normalizer.normalize(variant) == expected, f"Failed for variant: {variant}"


def test_spelling_equivalence():
    normalizer = SkillNormalizer()
    assert normalizer.are_equivalent("A/B testing", "split testing")
    assert normalizer.are_equivalent("scikit-learn", "sklearn")
    assert normalizer.are_equivalent("k8s", "kubernetes")
    assert not normalizer.are_equivalent("Python", "SQL")


# ---------------------------------------------------------------------------
# 2. Garbage Filtering Tests
# ---------------------------------------------------------------------------

def test_clean_candidate_token():
    assert clean_candidate_token("* Strong experience with Python (preferred)") == "Python"
    assert clean_candidate_token("- Hands-on knowledge of Docker is a plus") == "Docker"
    assert clean_candidate_token("1. Solid background in SQL or similar") == "SQL"
    assert clean_candidate_token("A/B testing,") == "A/B testing"


def test_filter_pipeline_discards_garbage():
    filt = SkillFilterPipeline()

    garbage_samples = [
        ("VMware-Technologien macht dich zum entscheidenden Faktor in digitalen Transformationsprojekten", "too_long"),
        ("presenting technical materials in written and verbal form", "too_long"),
        ("stakeholders", "single_stopword"),
        ("3+ years of experience in data analysis", "too_long"),
        ("5 years", "years_experience"),
        ("master's degree", "degree_boilerplate"),
        ("team player", "corporate_filler"),
        ("working in tech", "verbal_clause"),
        ("leading teams", "verbal_clause"),
        ("R", "too_short"),
    ]

    for sample, expected_reason in garbage_samples:
        is_bad, reason = filt.is_garbage(sample)
        assert is_bad, f"Sample '{sample}' should have been flagged as garbage"
        assert reason == expected_reason, f"Expected reason '{expected_reason}', got '{reason}' for '{sample}'"


def test_filter_pipeline_accepts_valid_skills():
    filt = SkillFilterPipeline()
    valid_samples = [
        "Python",
        "SQL",
        "A/B Testing",
        "Kubernetes",
        "Docker",
        "PyTorch",
        "Power BI",
        "CI/CD",
        "Apache Spark",
        "Tableau",
    ]
    for sample in valid_samples:
        is_bad, reason = filt.is_garbage(sample)
        assert not is_bad, f"Valid skill '{sample}' was incorrectly rejected with reason '{reason}'"


# ---------------------------------------------------------------------------
# 3. Open Extractor Tests
# ---------------------------------------------------------------------------

def test_open_extractor_triggers():
    extractor = OpenSkillExtractor()
    job_text = """
    About Us:
    We are a fast-growing tech company building AI products.

    Requirements:
    * Proven experience with Python, SQL, and Docker
    * Hands-on knowledge of PyTorch and Scikit-learn
    * Strong background in A/B testing and split testing
    * Working knowledge of cloud infrastructure (AWS, Azure or GCP)
    * Experience building CI/CD pipelines

    Benefits:
    * 30 days vacation
    * Gym membership
    """
    skills = extractor.extract_from_description(job_text)
    assert "Python" in skills
    assert "SQL" in skills
    assert "Docker" in skills
    assert "PyTorch" in skills
    assert "Scikit-Learn" in skills
    assert "A/B Testing" in skills
    assert "AWS" in skills
    assert "Azure" in skills
    assert "GCP" in skills
    assert "CI/CD" in skills

    # Ensure perks are not extracted as skills
    assert "Gym membership" not in skills
    assert "30 days vacation" not in skills


def test_open_extractor_german_sections():
    extractor = OpenSkillExtractor()
    german_ad = """
    Über uns:
    Wir sind ein führendes Beratungshaus.

    Anforderungen:
    * Fundierte Kenntnisse in Python und SQL
    * Erfahrung mit Kubernetes und Docker
    * Mehrjährige Erfahrung mit A/B-Testing

    Wir bieten:
    * Flexible Arbeitszeiten
    """
    skills = extractor.extract_from_description(german_ad)
    assert "Python" in skills
    assert "SQL" in skills
    assert "Kubernetes" in skills
    assert "Docker" in skills
    assert "A/B Testing" in skills


def test_open_extractor_linkedin_native_tags():
    extractor = OpenSkillExtractor()
    json_str = '{"text": "Python\\nSQL\\nPower BI\\nRAG\\nAgentic AI\\nA/B Testing"}'
    skills = extractor.extract_from_linkedin_json(json_str)
    assert "Python" in skills
    assert "SQL" in skills
    assert "Power BI" in skills
    assert "RAG" in skills
    assert "A/B Testing" in skills


# ---------------------------------------------------------------------------
# 4. Corpus Harvester & Cross-Company Dispersion Tests
# ---------------------------------------------------------------------------

def test_skill_harvester_dispersion():
    harvester = SkillHarvester(min_companies=2)

    # Job 1 from Company Alpha
    harvester.process_job(
        job_id=1,
        company_name="Alpha Tech",
        description="Requirements:\n* Experience with Python, SQL, and Luminex workflows",
    )

    # Job 2 from Company Beta
    harvester.process_job(
        job_id=2,
        company_name="Beta Analytics",
        description="Qualifications:\n* Knowledge of Python and A/B testing",
    )

    # Job 3 from Company Gamma
    harvester.process_job(
        job_id=3,
        company_name="Gamma AI",
        description="Requirements:\n* Hands-on with A/B-testing and SQL",
    )

    verified = harvester.get_verified_skill_names()

    # Multi-company skills verified
    assert "Python" in verified  # 2 companies (Alpha, Beta)
    assert "SQL" in verified  # 2 companies (Alpha, Gamma)
    assert "A/B Testing" in verified  # 2 companies (Beta, Gamma)

    # Single-company term Luminex workflows should NOT be verified
    assert "Luminex Workflows" not in verified
