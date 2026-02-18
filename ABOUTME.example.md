# About Me — [Your Name]

> Single source of truth for all pipeline scripts. Parsed by `execution/profile_loader.py`.
> Update this file to change your profile everywhere — CV, cover letter, evaluation, follow-ups.
>
> **Setup:** Copy this file to `ABOUTME.md` and fill in your real data. The pipeline reads `ABOUTME.md` (gitignored).

## Personal Information

```yaml
name: "Your Name"
dob: "01.01.1995"
age: 31
nationality: "Swiss"
nationality_localized:
  de: "Schweizerisch"
  en: "Swiss"
  it: "Svizzero/a"
email: "your.email@example.com"
phone: "+41 79 123 4567"
linkedin: "linkedin.com/in/yourhandle"
linkedin_handle: "yourhandle"
location: "Switzerland"
location_localized:
  de: "Schweiz"
  en: "Switzerland"
  it: "Svizzera"
availability: "3 months notice period"
availability_localized:
  de: "3 Monate Kündigungsfrist"
  en: "3 months notice period"
  it: "3 mesi di preavviso"
drivers_license: "Category B (car)"
drivers_license_localized:
  de: "Kategorie B (Auto)"
  en: "Category B (car)"
  it: "Categoria B (auto)"
work_permit:
  de: "B-Bewilligung"
  en: "B permit"
  it: "Permesso B"
```

## Work Experience

```yaml
company: "Your Company AG"
company_anonymous: "a mid-size technology company"
company_location: "Zurich, Switzerland"
role: "Your Job Title"
role_start: "January 2022"
role_period: "~3 years"

metrics:
  # Define your quantifiable achievements here
  key_metric_1: "10,000+"
  key_metric_2: "500+"
  improvement_pct: "25%"
  team_size: "15"
  notice_days: "90"
  years_experience: "~3"

# These are the ONLY numbers the LLM may use (anti-hallucination whitelist)
allowed_numbers:
  - "10000"
  - "10,000"
  - "500"
  - "25"
  - "15"
  - "90"
  - "3"
  - "0"
  - "1"
  - "2"

experience_bullets:
  de:
    - "Bullet point 1 in German with your metrics"
    - "Bullet point 2 in German"
  en:
    - "Bullet point 1 in English with your metrics"
    - "Bullet point 2 in English"
  it:
    - "Bullet point 1 in Italian with your metrics"
    - "Bullet point 2 in Italian"

# Keyword mappings for deterministic bullet reordering (index -> keywords)
bullet_keywords:
  0: ["keyword1", "keyword2"]
  1: ["keyword3", "keyword4"]
```

## Profile Summary

```yaml
profile_summary:
  de: "Your professional summary in German..."
  en: "Your professional summary in English..."
  it: "Your professional summary in Italian..."

strengths:
  - "Strength 1"
  - "Strength 2"
  - "Strength 3"

relocation_motivation:
  de: "Why you want to work in Switzerland (German)..."
  en: "Why you want to work in Switzerland (English)..."
  it: "Why you want to work in Switzerland (Italian)..."
```

## Skills

```yaml
# Ordered list for CV (LLM reorders per job relevance)
skills:
  - "Your Skill 1"
  - "Your Skill 2"
  - "Your Skill 3"

# Full skill list for profile text
technical_skills: "skill1, skill2, skill3, etc."

soft_skills:
  de: ["Analytisches Denken", "Teamarbeit"]
  en: ["Analytical Thinking", "Teamwork"]
  it: ["Pensiero Analitico", "Lavoro di Squadra"]
```

## Certifications

```yaml
certifications:
  - name: "Your Certification"
    score: "95%"
    issuer: "Issuing Organization"

certs_anonymous: "Certification 1, Certification 2"
certs_full: "Certification 1 (95%), Certification 2 (90%)"
```

## Education

```yaml
degree_type: "Bachelor"
has_bachelor: true
has_master: false
start_year: "2015"
end_year: "2019"

education_localized:
  de:
    title: "Bachelor of Science in Informatik"
    school: "ETH Zurich"
    description: "BSc in Computer Science"
  en:
    title: "Bachelor of Science in Computer Science"
    school: "ETH Zurich"
    description: "BSc in Computer Science"
  it:
    title: "Laurea in Informatica"
    school: "ETH Zurich"
    description: "Laurea triennale in Informatica"
```

## Languages

```yaml
languages:
  - code: "de"
    level: "native"
    localized:
      de: { name: "Deutsch", level: "Muttersprache" }
      en: { name: "German", level: "Native" }
      it: { name: "Tedesco", level: "Madrelingua" }
  - code: "en"
    level: "C1"
    localized:
      de: { name: "Englisch", level: "C1" }
      en: { name: "English", level: "C1" }
      it: { name: "Inglese", level: "C1" }

languages_text: "German (native), English (C1)"
```

## Interests

```yaml
interests:
  de:
    - "Interest 1 (German)"
    - "Interest 2 (German)"
  en:
    - "Interest 1 (English)"
    - "Interest 2 (English)"
  it:
    - "Interest 1 (Italian)"
    - "Interest 2 (Italian)"
```

## Achievements

```yaml
achievements:
  de:
    - { number: "10K+", label: "Your achievement in German" }
  en:
    - { number: "10K+", label: "Your achievement in English" }
  it:
    - { number: "10K+", label: "Your achievement in Italian" }

achievement_keywords:
  0: ["keyword1", "keyword2"]
```

## Target Roles

```yaml
target: "Your target role description"
search_terms:
  - "Job Title 1"
  - "Job Title 2"
  - "Job Title 3"
```

## CV Section Labels

```yaml
# Localized labels for CV sections (rarely need changing)
labels:
  de:
    personal_info: "Persönliche Daten"
    date_of_birth: "Geburtsdatum"
    nationality: "Nationalität"
    permit: "Aufenthaltsstatus"
    email_label: "E-Mail"
    phone_label: "Telefon"
    location_label: "Standort"
    availability_label: "Verfügbarkeit"
    license_label: "Führerschein"
    skills_heading: "Fachkenntnisse"
    languages_heading: "Sprachen"
    summary_heading: "Profil"
    experience_heading: "Berufserfahrung"
    education_heading: "Ausbildung"
    certifications_heading: "Zertifikate"
    present: "heute"
    courses: "Kurse"
    achievements_heading: "Schlüsselerfolge"
    interests_heading: "Interessen"
  en:
    personal_info: "Personal Info"
    date_of_birth: "Date of Birth"
    nationality: "Nationality"
    permit: "Work Permit"
    email_label: "Email"
    phone_label: "Phone"
    location_label: "Location"
    availability_label: "Availability"
    license_label: "Driver's License"
    skills_heading: "Skills"
    languages_heading: "Languages"
    summary_heading: "Profile"
    experience_heading: "Work Experience"
    education_heading: "Education"
    certifications_heading: "Certifications"
    present: "present"
    courses: "Courses"
    achievements_heading: "Key Achievements"
    interests_heading: "Interests"
  it:
    personal_info: "Dati Personali"
    date_of_birth: "Data di nascita"
    nationality: "Nazionalità"
    permit: "Permesso di lavoro"
    email_label: "Email"
    phone_label: "Telefono"
    location_label: "Sede"
    availability_label: "Disponibilità"
    license_label: "Patente"
    skills_heading: "Competenze"
    languages_heading: "Lingue"
    summary_heading: "Profilo"
    experience_heading: "Esperienza Lavorativa"
    education_heading: "Formazione"
    certifications_heading: "Certificazioni"
    present: "presente"
    courses: "Corsi"
    achievements_heading: "Risultati Chiave"
    interests_heading: "Interessi"
```

## Privacy Rules

```yaml
# Which providers are trusted with full PII
trusted_providers:
  - "gemini"
  - "google"

# Which providers get anonymized profile (no PII)
untrusted_providers:
  - "openrouter"
  - "deepseek"

# Fields to strip for anonymous profile
anonymize_fields:
  - "name"
  - "email"
  - "phone"
  - "linkedin"
  - "company"
  - "company_location"
```
