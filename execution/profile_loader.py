"""
Profile loader — single source of truth for all candidate data.

Reads ABOUTME.md and provides structured access for all pipeline scripts.
Singleton-cached: file is parsed once per process, then reused.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PersonalInfo:
    name: str = ""
    dob: str = ""
    age: int = 0
    nationality: str = ""
    nationality_localized: dict[str, str] = field(default_factory=dict)
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    linkedin_handle: str = ""
    github: str = ""
    github_handle: str = ""
    location: str = ""
    location_localized: dict[str, str] = field(default_factory=dict)
    availability: str = ""
    availability_localized: dict[str, str] = field(default_factory=dict)
    drivers_license: str = ""
    drivers_license_localized: dict[str, str] = field(default_factory=dict)
    work_permit: dict[str, str] = field(default_factory=dict)


@dataclass
class Experience:
    company: str = ""
    company_anonymous: str = ""
    company_location: str = ""
    role: str = ""
    role_official: str = ""
    role_official_label_localized: dict[str, str] = field(default_factory=dict)
    role_start: str = ""
    role_period: str = ""
    metrics: dict[str, str] = field(default_factory=dict)
    allowed_numbers: list[str] = field(default_factory=list)
    experience_bullets: dict[str, list[str]] = field(default_factory=dict)
    bullet_keywords: dict[int, list[str]] = field(default_factory=dict)

    # Default localized labels used when role_official_label_localized is empty
    # or missing the requested language. Prevents EN label leaking into DE/IT CVs.
    _DEFAULT_OFFICIAL_LABELS = {
        "de": "Vertraglicher Titel",
        "en": "Official Title",
        "it": "Titolo contrattuale",
    }

    def official_label(self, lang_code: str) -> str:
        """Localized prefix for the parenthetical, e.g. 'Vertraglicher Titel'."""
        return (
            self.role_official_label_localized.get(lang_code)
            or self._DEFAULT_OFFICIAL_LABELS.get(lang_code, "Official Title")
        )

    def official_line(self, lang_code: str) -> str:
        """Render the contract-title disclosure, e.g.
        '(Vertraglicher Titel: Digital Sales Specialist)'.
        Returns '' when role_official is unset, so callers can short-circuit."""
        if not self.role_official:
            return ""
        return f"({self.official_label(lang_code)}: {self.role_official})"


@dataclass
class Certification:
    name: str = ""
    score: str = ""
    issuer: str = ""


@dataclass
class Education:
    degree_type: str = ""
    has_bachelor: bool = False
    has_master: bool = False
    start_year: str = ""
    end_year: str = ""
    education_localized: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class Language:
    code: str = ""
    level: str = ""
    localized: dict[str, dict[str, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Main profile class
# ---------------------------------------------------------------------------

@dataclass
class CandidateProfile:
    personal: PersonalInfo = field(default_factory=PersonalInfo)
    experience: Experience = field(default_factory=Experience)
    certifications: list[Certification] = field(default_factory=list)
    education: Education = field(default_factory=Education)
    languages: list[Language] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    technical_skills: str = ""
    certs_anonymous: str = ""
    certs_full: str = ""
    interests: dict[str, list[str]] = field(default_factory=dict)
    achievements: dict[str, list[dict]] = field(default_factory=dict)
    achievement_keywords: dict[int, list[str]] = field(default_factory=dict)
    target: str = ""
    projects: list[dict] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    languages_text: str = ""
    privacy_trusted: list[str] = field(default_factory=list)
    privacy_untrusted: list[str] = field(default_factory=list)
    anonymize_fields: list[str] = field(default_factory=list)
    profile_summary: dict[str, str] = field(default_factory=dict)
    relocation_motivation: dict[str, str] = field(default_factory=dict)
    strengths: list[str] = field(default_factory=list)
    soft_skills: dict[str, list[str]] = field(default_factory=dict)

    # ---- Profile string generators ----

    def profile_anonymous(self) -> str:
        """Generate anonymized profile string (no PII — for untrusted providers)."""
        p = self.personal
        e = self.experience
        lines = [
            f"- Age: {p.age}, European national relocating to Switzerland",
            f"- Experience: {e.role_period} as {e.role} at {e.company_anonymous} (Italy), since {e.role_start}",
        ]
        # Add bullet points for experience
        en_bullets = e.experience_bullets.get("en", [])
        for b in en_bullets:
            lines.append(f"  - {b}")
        lines.extend([
            f"- Certifications: {self.certs_anonymous}",
            f"- Languages: {self.languages_text}",
            f"- Education: {self.education.education_localized.get('en', {}).get('description', '')}",
            f"- Technical skills: {self.technical_skills}",
            f"- Availability: {p.availability}",
            f"- Driver's license: {p.drivers_license}",
            f"- Target: {self.target}",
        ])
        if self.relocation_motivation:
            lines.append(f"- Relocation: {self.relocation_motivation.get('en', '')}")
        if self.strengths:
            lines.append(f"- Key strengths: {'; '.join(self.strengths)}")
        return "\n".join(lines)

    def profile_full(self) -> str:
        """Generate full profile string (with PII — for trusted providers)."""
        p = self.personal
        e = self.experience
        lines = [
            f"- Name: {p.name}, {p.age} years old (soon {p.age + 1}), {p.nationality} national relocating to Switzerland",
            f"- Experience: {e.role_period} as {e.role} at {e.company} ({e.company_location}), since {e.role_start}",
        ]
        en_bullets = e.experience_bullets.get("en", [])
        for b in en_bullets:
            lines.append(f"  - {b}")
        lines.extend([
            f"- Certifications: {self.certs_full}",
            f"- Languages: {self.languages_text}",
            f"- Education: {self.education.education_localized.get('en', {}).get('description', '')}",
            f"- Technical skills: {self.technical_skills}",
            f"- Availability: {p.availability}",
            f"- Driver's license: {p.drivers_license}",
            f"- LinkedIn: {p.linkedin}",
            f"- Target: {self.target}",
        ])
        if self.relocation_motivation:
            lines.append(f"- Relocation: {self.relocation_motivation.get('en', '')}")
        if self.strengths:
            lines.append(f"- Key strengths: {'; '.join(self.strengths)}")
        return "\n".join(lines)

    def profile_for_provider(self, provider: str) -> str:
        """Select anonymous or full profile based on privacy rules."""
        provider_lower = provider.lower()
        if any(t in provider_lower for t in self.privacy_trusted):
            return self.profile_full()
        return self.profile_anonymous()

    def allowed_numbers_set(self) -> set[str]:
        """All metric values the LLM is allowed to use (for hallucination check)."""
        return set(self.experience.allowed_numbers)

    def cv_labels(self, lang_code: str) -> dict[str, str]:
        """Get localized CV section labels + dynamic personal data values."""
        base = dict(self.labels.get(lang_code, self.labels.get("de", {})))
        # Add dynamic values from personal info
        loc = self.personal.nationality_localized
        base["nationality_value"] = loc.get(lang_code, loc.get("en", ""))
        perm = self.personal.work_permit
        base["permit_value"] = perm.get(lang_code, perm.get("en", ""))
        avail = self.personal.availability_localized
        base["availability_value"] = avail.get(lang_code, avail.get("en", ""))
        lic = self.personal.drivers_license_localized
        base["license_value"] = lic.get(lang_code, lic.get("en", ""))
        loca = self.personal.location_localized
        base["location_value"] = loca.get(lang_code, loca.get("en", ""))
        # Education
        edu = self.education.education_localized.get(lang_code, {})
        base["matura_title"] = edu.get("title", "")
        base["matura_school"] = edu.get("school", "")
        return base

    def cv_experience_bullets(self, lang_code: str) -> list[str]:
        """Get experience bullets for a language."""
        return self.experience.experience_bullets.get(
            lang_code, self.experience.experience_bullets.get("de", [])
        )

    def cv_achievements(self, lang_code: str) -> list[dict]:
        """Get achievements for a language."""
        return self.achievements.get(lang_code, self.achievements.get("de", []))

    def cv_language_list(self, lang_code: str) -> list[dict[str, str]]:
        """Get language list formatted for CV in a specific display language."""
        result = []
        for lang in self.languages:
            loc = lang.localized.get(lang_code, lang.localized.get("en", {}))
            result.append({"name": loc.get("name", ""), "level": loc.get("level", "")})
        return result

    def cv_interests(self, lang_code: str) -> list[str]:
        """Get interests for a language."""
        return self.interests.get(lang_code, self.interests.get("de", []))

    def cv_soft_skills(self, lang_code: str) -> list[str]:
        """Get soft skills for a language."""
        return self.soft_skills.get(lang_code, self.soft_skills.get("de", []))

    def cv_projects(self, lang_code: str) -> list[dict]:
        """Get projects with localized names, descriptions, and tags for CV display."""
        result = []
        for proj in self.projects:
            name = proj.get("name_localized", {}).get(lang_code, proj.get("name", ""))
            desc = proj.get("description_localized", {}).get(lang_code, "")
            result.append({
                "name": name,
                "tech": proj.get("tech", ""),
                "url": proj.get("url", ""),
                "tags": proj.get("tags", []),
                "description": desc,
            })
        return result

    def relevant_projects(self, key_matches: list[str], lang_code: str, top_n: int = 1) -> list[dict]:
        """Return projects sorted by tag overlap with job key_matches (for cover letter injection).

        Only returns projects with at least 1 matching tag.
        """
        all_projects = self.cv_projects(lang_code)
        km_lower = {w.lower() for w in key_matches}

        def score(proj: dict) -> int:
            return sum(1 for tag in proj["tags"] if tag.lower() in km_lower)

        scored = sorted(all_projects, key=score, reverse=True)
        return [p for p in scored if score(p) > 0][:top_n]


# ---------------------------------------------------------------------------
# ABOUTME.md parser
# ---------------------------------------------------------------------------

# Regex: match "## Section Name" followed by ```yaml ... ```
_SECTION_RE = re.compile(
    r'##\s+(.+?)\s*\n'           # Section header
    r'(?:.*?\n)*?'                # Any lines between header and code block
    r'```yaml\s*\n'              # Opening yaml fence
    r'(.*?)'                     # YAML content (non-greedy)
    r'\n```',                    # Closing fence
    re.DOTALL,
)

def _find_aboutme() -> Path:
    """Find ABOUTME.md — checks project root, then parent directories."""
    # Try relative to this script: execution/ -> project root
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    candidate = project_root / "ABOUTME.md"
    if candidate.exists():
        return candidate
    # Fallback: environment variable
    env_path = os.environ.get("ABOUTME_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    raise FileNotFoundError(
        f"ABOUTME.md not found at {candidate}. "
        f"Set ABOUTME_PATH env var or place it in the project root."
    )


def _parse_aboutme(path: Path) -> CandidateProfile:
    """Parse ABOUTME.md into a CandidateProfile."""
    text = path.read_text(encoding="utf-8")

    # Extract all YAML blocks with their section names
    sections: dict[str, dict] = {}
    for match in _SECTION_RE.finditer(text):
        section_name = match.group(1).strip()
        yaml_content = match.group(2)
        try:
            parsed = yaml.safe_load(yaml_content)
            if isinstance(parsed, dict):
                sections[section_name] = parsed
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in section '{section_name}': {e}") from e

    profile = CandidateProfile()

    # --- Personal Information ---
    pi = sections.get("Personal Information", {})
    profile.personal = PersonalInfo(
        name=pi.get("name", ""),
        dob=pi.get("dob", ""),
        age=int(pi.get("age", 0)),
        nationality=pi.get("nationality", ""),
        nationality_localized=pi.get("nationality_localized", {}),
        email=pi.get("email", ""),
        phone=pi.get("phone", ""),
        linkedin=pi.get("linkedin", ""),
        linkedin_handle=pi.get("linkedin_handle", ""),
        github=pi.get("github", ""),
        github_handle=pi.get("github_handle", ""),
        location=pi.get("location", ""),
        location_localized=pi.get("location_localized", {}),
        availability=pi.get("availability", ""),
        availability_localized=pi.get("availability_localized", {}),
        drivers_license=pi.get("drivers_license", ""),
        drivers_license_localized=pi.get("drivers_license_localized", {}),
        work_permit=pi.get("work_permit", {}),
    )

    # --- Work Experience ---
    we = sections.get("Work Experience", {})
    # Convert bullet_keywords keys to int (YAML may parse them as int already)
    raw_bk = we.get("bullet_keywords", {})
    bullet_kw = {int(k): v for k, v in raw_bk.items()}
    profile.experience = Experience(
        company=we.get("company", ""),
        company_anonymous=we.get("company_anonymous", ""),
        company_location=we.get("company_location", ""),
        role=we.get("role", ""),
        role_official=we.get("role_official", ""),
        role_official_label_localized=we.get("role_official_label_localized", {}),
        role_start=we.get("role_start", ""),
        role_period=we.get("role_period", ""),
        metrics=we.get("metrics", {}),
        allowed_numbers=we.get("allowed_numbers", []),
        experience_bullets=we.get("experience_bullets", {}),
        bullet_keywords=bullet_kw,
    )

    # --- Skills ---
    sk = sections.get("Skills", {})
    profile.skills = sk.get("skills", [])
    profile.technical_skills = sk.get("technical_skills", "")
    profile.soft_skills = sk.get("soft_skills", {})

    # --- Profile Summary ---
    ps = sections.get("Profile Summary", {})
    profile.profile_summary = ps.get("profile_summary", {})
    profile.relocation_motivation = ps.get("relocation_motivation", {})
    profile.strengths = ps.get("strengths", [])

    # --- Certifications ---
    ce = sections.get("Certifications", {})
    profile.certifications = [
        Certification(name=c.get("name", ""), score=c.get("score", ""), issuer=c.get("issuer", ""))
        for c in ce.get("certifications", [])
    ]
    profile.certs_anonymous = ce.get("certs_anonymous", "")
    profile.certs_full = ce.get("certs_full", "")

    # --- Education ---
    ed = sections.get("Education", {})
    profile.education = Education(
        degree_type=ed.get("degree_type", ""),
        has_bachelor=ed.get("has_bachelor", False),
        has_master=ed.get("has_master", False),
        start_year=str(ed.get("start_year", "")),
        end_year=str(ed.get("end_year", "")),
        education_localized=ed.get("education_localized", {}),
    )

    # --- Languages ---
    la = sections.get("Languages", {})
    profile.languages = [
        Language(code=l.get("code", ""), level=l.get("level", ""), localized=l.get("localized", {}))
        for l in la.get("languages", [])
    ]
    profile.languages_text = la.get("languages_text", "")

    # --- Interests ---
    profile.interests = sections.get("Interests", {}).get("interests", {})

    # --- Achievements ---
    ac = sections.get("Achievements", {})
    profile.achievements = ac.get("achievements", {})
    raw_ak = ac.get("achievement_keywords", {})
    profile.achievement_keywords = {int(k): v for k, v in raw_ak.items()}

    # --- Technical Projects ---
    tp = sections.get("Technical Projects", {})
    profile.projects = tp.get("projects", [])

    # --- Target Roles ---
    tr = sections.get("Target Roles", {})
    profile.target = tr.get("target", "")
    profile.search_terms = tr.get("search_terms", [])

    # --- CV Section Labels ---
    lb = sections.get("CV Section Labels", {})
    profile.labels = lb.get("labels", {})

    # --- Privacy Rules ---
    pr = sections.get("Privacy Rules", {})
    profile.privacy_trusted = pr.get("trusted_providers", [])
    profile.privacy_untrusted = pr.get("untrusted_providers", [])
    profile.anonymize_fields = pr.get("anonymize_fields", [])

    # Validate required fields — fail early with clear message
    _required = [
        (profile.personal.name, "Personal Information -> name"),
        (profile.personal.email, "Personal Information -> email"),
        (profile.experience.role, "Work Experience -> role"),
        (profile.experience.allowed_numbers, "Work Experience -> allowed_numbers"),
    ]
    _missing = [desc for val, desc in _required if not val]
    if _missing:
        raise ValueError(
            f"ABOUTME.md missing required fields: {', '.join(_missing)}. "
            f"Check YAML structure."
        )

    return profile


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_cached_profile: CandidateProfile | None = None


def load_profile() -> CandidateProfile:
    """Load and cache the candidate profile from ABOUTME.md.

    Thread-safe for read-only access. Profile is parsed once per process.
    """
    global _cached_profile
    if _cached_profile is None:
        path = _find_aboutme()
        _cached_profile = _parse_aboutme(path)
    return _cached_profile


# ---------------------------------------------------------------------------
# Quick accessors (convenience)
# ---------------------------------------------------------------------------

def get_name() -> str:
    return load_profile().personal.name

def get_email() -> str:
    return load_profile().personal.email

def get_phone() -> str:
    return load_profile().personal.phone


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = load_profile()
    print("=== Profile Loader Self-Test ===\n")
    print(f"Name: {p.personal.name}")
    print(f"Email: {p.personal.email}")
    print(f"DOB: {p.personal.dob}")
    print(f"Role: {p.experience.role} at {p.experience.company}")
    print(f"Skills: {p.skills[:5]}...")
    print(f"Certifications: {len(p.certifications)}")
    print(f"Languages: {p.languages_text}")
    print(f"Search terms: {len(p.search_terms)}")
    print(f"\n--- Anonymous Profile ---")
    print(p.profile_anonymous()[:300])
    print(f"\n--- Full Profile ---")
    print(p.profile_full()[:300])
    print(f"\n--- CV Labels (de) ---")
    labels = p.cv_labels("de")
    print(f"  summary_heading: {labels.get('summary_heading')}")
    print(f"  nationality_value: {labels.get('nationality_value')}")
    print(f"  matura_title: {labels.get('matura_title')}")
    print(f"\n--- Allowed Numbers ---")
    print(f"  {p.allowed_numbers_set()}")
    print(f"\n--- Achievements (en) ---")
    print(f"  {p.cv_achievements('en')}")
    print("\nAll checks passed!")
