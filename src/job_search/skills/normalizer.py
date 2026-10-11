"""
Deterministic skill spelling normalizer and alias resolver.
Zero AI utilization. Handles punctuation folding, acronym preservation,
alias mapping, and fuzzy typo clustering.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, Optional, Set


# Canonical mappings based on folded strings (lowercase, punctuation-stripped)
CANONICAL_FOLD_MAP: Dict[str, str] = {
    # Experimentation & Methodologies
    "abtesting": "A/B Testing",
    "abtest": "A/B Testing",
    "abtests": "A/B Testing",
    "splittesting": "A/B Testing",
    "splittest": "A/B Testing",
    "multivariatetesting": "A/B Testing",
    "multivariatetest": "A/B Testing",
    "crispdm": "CRISP-DM",
    "tdd": "TDD",
    "bdd": "BDD",
    
    # ML & Data Frameworks
    "scikitlearn": "Scikit-Learn",
    "sklearn": "Scikit-Learn",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "xgboost": "XGBoost",
    "lightgbm": "LightGBM",
    "huggingface": "Hugging Face",
    "langchain": "LangChain",
    "llamaindex": "LlamaIndex",
    "spacy": "spaCy",
    "nltk": "NLTK",
    
    # AI / LLM
    "genai": "Generative AI",
    "generativeai": "Generative AI",
    "llm": "Large Language Models (LLMs)",
    "llms": "Large Language Models (LLMs)",
    "largelanguagemodel": "Large Language Models (LLMs)",
    "largelanguagemodels": "Large Language Models (LLMs)",
    "rag": "RAG",
    "retrievalaugmentedgeneration": "RAG",
    "nlp": "Natural Language Processing (NLP)",
    "naturallanguageprocessing": "Natural Language Processing (NLP)",
    "computervision": "Computer Vision",
    
    # DevOps & Infrastructure
    "cicd": "CI/CD",
    "continuousintegrationcontinuousdeployment": "CI/CD",
    "continuousintegration": "CI/CD",
    "continuousdeployment": "CI/CD",
    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",
    "docker": "Docker",
    "terraform": "Terraform",
    "ansible": "Ansible",
    "mlops": "MLOps",
    
    # Cloud Providers
    "aws": "AWS",
    "amazonwebservices": "AWS",
    "gcp": "GCP",
    "googlecloud": "GCP",
    "googlecloudplatform": "GCP",
    "azure": "Azure",
    "microsoftazure": "Azure",
    
    # Databases & Big Data
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "spark": "Apache Spark",
    "apachespark": "Apache Spark",
    "pyspark": "PySpark",
    "bigquery": "BigQuery",
    "kafka": "Apache Kafka",
    "apachekafka": "Apache Kafka",
    
    # Programming Languages & Runtimes
    "python": "Python",
    "python3": "Python",
    "sql": "SQL",
    "nodejs": "Node.js",
    "node": "Node.js",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "golang": "Go",
    "cplusplus": "C++",
    "csharp": "C#",
    "dotnet": ".NET",
    
    # BI & Analytics
    "powerbi": "Power BI",
    "tableau": "Tableau",
    "excel": "Microsoft Excel",
    "microsoftexcel": "Microsoft Excel",
    "looker": "Looker",
    "streamlit": "Streamlit",
}

# Standard technical acronyms to keep uppercase
KNOWN_ACRONYMS: Set[str] = {
    "SQL", "API", "APIS", "REST", "CI/CD", "AWS", "GCP", "ML", "AI", "NLP",
    "LLM", "LLMS", "RAG", "CRM", "ERP", "ETL", "ELT", "BI", "TDD", "BDD",
    "SEO", "SEM", "SaaS", "PaaS", "IaaS", "MLOps", "DevOps", "JSON", "XML",
}


def fold_token(token: str) -> str:
    """Normalize token by lowercasing and stripping all whitespace, hyphens, slashes, periods, and underscores."""
    return re.sub(r"[\s\-_/.]+", "", token.lower())


class SkillNormalizer:
    """Normalizes skill strings into their canonical names and clusters variants."""

    def __init__(self, custom_mappings: Optional[Dict[str, str]] = None):
        self.fold_map: Dict[str, str] = dict(CANONICAL_FOLD_MAP)
        if custom_mappings:
            for k, v in custom_mappings.items():
                self.fold_map[fold_token(k)] = v

    def normalize(self, token: str) -> str:
        """
        Normalize a raw skill token to its canonical presentation.
        Handles folded matches, acronyms, and clean title-casing.
        """
        if not token:
            return ""

        cleaned = token.strip()
        # Handle special symbol cases explicitly
        upper_c = cleaned.upper()
        if upper_c in ("C++", "C#", ".NET"):
            return cleaned.upper() if cleaned.upper() != ".NET" else ".NET"
        if upper_c == "CI/CD" or upper_c == "CI-CD":
            return "CI/CD"
        if upper_c in ("A/B TESTING", "A/B-TESTING", "A/B TEST"):
            return "A/B Testing"

        # Fold any phrase focused on A/B testing or split testing
        if re.search(r"\ba[/ -]?b[- ]?test(?:ing|s)?\b", cleaned, flags=re.I) or "split testing" in cleaned.lower():
            return "A/B Testing"

        # Check folded map
        folded = fold_token(cleaned)
        if folded in self.fold_map:
            return self.fold_map[folded]

        # Preserve known uppercase acronyms
        if upper_c in KNOWN_ACRONYMS:
            return upper_c

        # Acronym check: all-caps 2 to 4 letters
        if len(cleaned) <= 4 and cleaned.isupper() and cleaned.isalpha():
            return cleaned

        # Standard title-casing for phrases
        # Avoid lowercasing parts of already well-formed camel/pascal cases like 'OpenAI', 'PyTorch'
        if cleaned.islower():
            return cleaned.title()

        return cleaned

    def are_equivalent(self, skill_a: str, skill_b: str) -> bool:
        """Check if two skill strings represent the same canonical skill."""
        return self.normalize(skill_a) == self.normalize(skill_b)
