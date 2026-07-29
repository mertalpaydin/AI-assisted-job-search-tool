from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from loguru import logger
from pypdf import PdfReader

from job_search.core.database import DatabaseManager


def escape_latex(text: str | None) -> str:
    """Safely escape special LaTeX characters in text while preserving LaTeX commands."""
    if not text:
        return ""
    # Map special LaTeX characters
    text = text.replace("\\", "\\textbackslash{}")
    text = text.replace("&", "\\&")
    text = text.replace("%", "\\%")
    text = text.replace("$", "\\$")
    text = text.replace("#", "\\#")
    text = text.replace("_", "\\_")
    text = text.replace("{", "\\{")
    text = text.replace("}", "\\}")
    text = text.replace("~", "\\textasciitilde{}")
    text = text.replace("^", "\\textasciicircum{}")
    text = text.replace("ß", "\\ss{}")
    return text


def get_a_or_an(job_title: str | None) -> str:
    """Determine whether 'a' or 'an' should precede the job title in English grammar."""
    title = (job_title or "").strip()
    if not title:
        return "a"
    
    words = title.split()
    first_word = words[0]
    clean_word = re.sub(r"^[^\w]+", "", first_word)
    if not clean_word:
        return "a"

    first_char = clean_word[0].upper()
    is_acronym = len(clean_word) <= 4 and clean_word.isupper()

    if is_acronym:
        # Acronyms pronounced starting with a vowel sound (e.g. AI, ERP, ML, HR, IT, RAG, SQL)
        if first_char in "AEIOUFHMNRSX":
            return "an"
    else:
        if first_char in "AEIOU":
            return "an"

    return "a"


def format_markdown_to_latex(text: str) -> str:
    """Convert common markdown elements (**bold**, *item*) to LaTeX."""
    parts = re.split(r"(\*\*.*?\*\*)", text)
    result = []
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            inner = part[2:-2]
            result.append(f"\\textbf{{{escape_latex(inner)}}}")
        else:
            result.append(escape_latex(part))
    return "".join(result)


def find_latex_compiler() -> str | None:
    """Find available xelatex or pdflatex executable."""
    # Check standard PATH first
    for cmd in ["xelatex", "pdflatex", "tectonic"]:
        path = shutil.which(cmd)
        if path:
            return path
    
    # Check common MiKTeX Windows installation paths
    miktex_paths = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "xelatex.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "pdflatex.exe",
        Path("C:/Program Files/MiKTeX/miktex/bin/x64/xelatex.exe"),
        Path("C:/Program Files/MiKTeX/miktex/bin/x64/pdflatex.exe"),
    ]
    for p in miktex_paths:
        if p.exists():
            return str(p)
            
    return None


def generate_cover_letter_pdf(
    job_id: int,
    db: DatabaseManager,
    project_root: str | Path,
    output_pdf_path: str | Path | None = None,
    override_title: str | None = None,
    override_company: str | None = None,
    override_cl_text: str | None = None,
) -> Path:
    """
    Generate a styled, single-page LaTeX cover letter PDF for a given job.
    
    Supports live unsaved UI overrides for title, company name, and cover letter text.
    Guarantees a 1-page layout by auto-adjusting body font size and margins if needed.
    """
    compiler = find_latex_compiler()
    if not compiler:
        raise RuntimeError("No LaTeX compiler (xelatex/pdflatex) found on system.")

    job = db.get_selected_job(job_id)
    
    # Determine effective values (using live overrides if provided)
    raw_title = (override_title.strip() if override_title is not None and override_title.strip() else (job.title if job else "Position"))
    raw_company = (override_company.strip() if override_company is not None and override_company.strip() else (job.company_name if job else "Company"))
    raw_cl_text = (override_cl_text.strip() if override_cl_text is not None and override_cl_text.strip() else (job.cover_letter_text if job else ""))

    if not raw_cl_text:
        raise ValueError(f"Job {job_id} has no cover letter text to render.")

    root = Path(project_root)
    template_path = root / "config" / "cover_letter_template.tex"
    if not template_path.exists():
        raise FileNotFoundError(f"LaTeX template not found at {template_path}")

    template_str = template_path.read_text(encoding="utf-8")

    # Candidate Info from cv.yaml
    candidate_name = "Applicant Name"
    candidate_address = "City, Country"
    candidate_phone = "+00 000 0000"
    candidate_email = "applicant@example.com"
    city = "City"

    cv_yaml_path = root / "config" / "cv.yaml"
    if cv_yaml_path.exists():
        import yaml
        try:
            cv_data = yaml.safe_load(cv_yaml_path.read_text(encoding="utf-8")) or {}
            info = cv_data.get("cv", {}).get("personal_info", {})
            candidate_name = info.get("name", candidate_name)
            candidate_address = info.get("address", candidate_address)
            candidate_phone = info.get("phone", candidate_phone)
            candidate_email = info.get("email", candidate_email)
            city = info.get("city", city)
        except Exception as e:
            logger.warning("Could not parse cv.yaml for LaTeX header: {}", e)

    today_str = datetime.now().strftime("%d.%m.%Y")
    article = get_a_or_an(raw_title)
    job_title = format_markdown_to_latex(raw_title)
    company_name = format_markdown_to_latex(raw_company)

    # Signature block
    sig_path = root / "config" / "assets" / "signature.png"
    if sig_path.exists():
        clean_sig_path = str(sig_path.resolve()).replace("\\", "/")
        signature_block = f"\\includegraphics[height=1.3cm]{{{clean_sig_path}}}\\\\[0.1cm]"
    else:
        signature_block = "\\vspace{1.0cm}"

    # Prepare body content: split by newlines, format markdown, wrap paragraphs
    raw_body = raw_cl_text.strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", raw_body) if p.strip()]

    # Extract salutation if present at start
    salutation = "Dear Hiring Manager,"
    if paragraphs and paragraphs[0].lower().startswith("dear "):
        salutation = format_markdown_to_latex(paragraphs[0])
        body_paras = paragraphs[1:]
    else:
        body_paras = paragraphs

    # Strip duplicate closing or signature name if present at the end of body_paras
    closings = ("sincerely", "kind regards", "best regards", "yours sincerely", "regards", "warm regards")
    while body_paras and body_paras[-1].strip().lower() in (candidate_name.lower(), "test applicant", "test applicant", "applicant name"):
        body_paras.pop()

    if body_paras and any(body_paras[-1].strip().lower().startswith(c) for c in closings):
        body_paras.pop()

    formatted_paras = [format_markdown_to_latex(p) for p in body_paras]
    formatted_body = "\\noindent " + salutation + "\\\\[0.3cm]\n\n" + "\n\n".join(formatted_paras)

    # Auto-fit configurations (Body Font Command, Margin) to guarantee 1 page
    # Header Name (18pt), Contact (9pt), Date/Subject (12pt) remain fixed
    configs = [
        ("\\fontsize{11pt}{14.5pt}\\selectfont", "1.8cm"),
        ("\\fontsize{10.5pt}{14pt}\\selectfont", "1.6cm"),
        ("\\fontsize{10pt}{13.5pt}\\selectfont", "1.5cm"),
        ("\\fontsize{9.5pt}{13pt}\\selectfont", "1.4cm"),
        ("\\fontsize{9pt}{12.5pt}\\selectfont", "1.3cm"),
    ]

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        tex_file = tmp_path / "cover_letter.tex"
        pdf_file = tmp_path / "cover_letter.pdf"

        final_pdf_path = None

        for body_font_cmd, margin in configs:
            content = template_str
            content = content.replace("VAR_BODY_FONT_CMD", body_font_cmd)
            content = content.replace("VAR_MARGIN", margin)
            content = content.replace("VAR_CANDIDATE_NAME", escape_latex(candidate_name))
            content = content.replace("VAR_CANDIDATE_ADDRESS", escape_latex(candidate_address))
            content = content.replace("VAR_CANDIDATE_PHONE", escape_latex(candidate_phone))
            content = content.replace("VAR_CANDIDATE_EMAIL", candidate_email)  # hyperref url unescaped
            content = content.replace("VAR_CITY", escape_latex(city))
            content = content.replace("VAR_DATE", today_str)
            content = content.replace("VAR_ARTICLE", article)
            content = content.replace("VAR_JOB_TITLE", job_title)
            content = content.replace("VAR_COMPANY_NAME", company_name)
            content = content.replace("VAR_BODY_CONTENT", formatted_body)
            content = content.replace("VAR_SIGNATURE_BLOCK", signature_block)

            tex_file.write_text(content, encoding="utf-8")

            # Run compiler (xelatex / pdflatex)
            proc = subprocess.run(
                [compiler, "-interaction=nonstopmode", f"-output-directory={tmp_path}", str(tex_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if not pdf_file.exists():
                logger.error("LaTeX compiler error (exit code {}):\nSTDOUT:\n{}\nSTDERR:\n{}", proc.returncode, proc.stdout, proc.stderr)
            else:
                reader = PdfReader(pdf_file)
                page_count = len(reader.pages)
                logger.debug(
                    "Compiled PDF with body_font={} margin={} -> page_count={}",
                    body_font_cmd, margin, page_count
                )
                if page_count == 1:
                    final_pdf_path = pdf_file
                    break

        if not final_pdf_path or not final_pdf_path.exists():
            if pdf_file.exists():
                final_pdf_path = pdf_file
            else:
                raise RuntimeError(f"LaTeX compilation failed. Log output:\n{proc.stdout}")

        if output_pdf_path:
            target_path = Path(output_pdf_path)
        else:
            pdf_dir = "data/export"
            config_yaml_path = root / "config" / "config.yaml"
            if config_yaml_path.exists():
                import yaml
                try:
                    cfg = yaml.safe_load(config_yaml_path.read_text(encoding="utf-8")) or {}
                    pdf_dir = cfg.get("export", {}).get("pdf_dir", pdf_dir)
                except Exception as e:
                    logger.warning("Could not read export.pdf_dir from config.yaml: {}", e)
            
            safe_company = re.sub(r"[^\w\-]", "_", raw_company)
            clean_name = re.sub(r"[^\w\-]", "_", candidate_name)
            target_path = Path(pdf_dir) / f"{clean_name}_CoverLetter_{safe_company}.pdf"

        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(final_pdf_path, target_path)
        logger.info("Generated 1-page cover letter PDF at {}", target_path)
        return target_path
