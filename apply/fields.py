"""Recognise application-form fields from their labels and answer the ones we can without Claude."""
import re
from dataclasses import dataclass, field


@dataclass
class Field:
    id: str                      # selector-safe handle assigned by the browser extractor
    label: str
    type: str                    # text | email | tel | url | textarea | select | radio | checkbox | file | combobox
    required: bool = False
    options: list[str] = field(default_factory=list)
    kind: str = ""               # filled in by classify()


# (kind, pattern) checked in order against the lower-cased label. First match wins.
PATTERNS = [
    ("resume", r"\b(resume|résumé|cv)\b"),
    ("cover_letter", r"\bcover letter\b"),
    ("first_name", r"\b(first|given|preferred first) name\b"),
    ("last_name", r"\b(last|family|sur) ?name\b"),
    ("email", r"\be-?mail\b"),
    ("phone", r"\b(phone|mobile|cell)\b"),
    ("linkedin", r"\blinkedin\b"),
    ("github", r"\bgithub\b"),
    ("website", r"\b(website|portfolio|personal site|other website)\b"),
    ("sponsorship", r"\b(sponsor|sponsorship|visa)\b"),
    ("work_auth", r"\b(authori[sz]ed|eligible|legally|right) to work\b|\bwork authori[sz]ation\b"),
    ("eeo", r"\b(gender|race|ethnicity|hispanic|latino|veteran|disability|disabled|sexual orientation|transgender)\b"),
    ("salary", r"\b(salary|compensation|pay expectation)\b"),
    ("notice_period", r"\bnotice period\b"),
    ("earliest_start", r"\b(start date|earliest start|when can you start|available to start)\b"),
    ("relocate", r"\breloca"),
    ("how_heard", r"\b(how did you hear|where did you hear|referral source|how did you find)\b"),
    ("pronouns", r"\bpronouns?\b"),
    ("location", r"\b(current location|location|city|where are you based)\b"),
    ("company", r"\b(current company|current employer|organization)\b"),
    ("full_name", r"\b(full name|^name$|^name\b)"),
    ("consent", r"\b(privacy|consent|i agree|i acknowledge|terms|gdpr|data)\b"),
]
LEGAL = {"sponsorship", "work_auth", "eeo"}
CLAUDE_OK = {"essay", "question", "consent", "company"}   # everything else needs a saved answer
COUNTRIES = ["united states", "canada", "united kingdom", "germany", "ireland", "india", "australia"]
COUNTRY_ALIASES = {"u.s.": "united states", "us ": "united states", "usa": "united states", "america": "united states",
                   "uk": "united kingdom", "remote - us": "united states", "remote us": "united states"}


def classify(f: Field) -> Field:
    label = re.sub(r"\s+", " ", f.label.lower()).strip(" *:")
    f.kind = next((kind for kind, pat in PATTERNS if re.search(pat, label)), "")
    if not f.kind:
        f.kind = "essay" if f.type == "textarea" else "question"
    return f


def country_for(text: str, job_location: str) -> str | None:
    """The country a legal question is about: named in the question, else the job's location."""
    for source in (text.lower(), job_location.lower()):
        for c in COUNTRIES:
            if c in source:
                return c
        for alias, c in COUNTRY_ALIASES.items():
            if alias in source + " ":
                return c
    return None


def pick_option(options: list[str], wanted: str) -> str | None:
    """Best option for a wanted answer: exact, then starts-with, then contains."""
    w = wanted.lower().strip()
    opts = [(o, o.lower().strip()) for o in options]
    for test in (lambda o: o == w, lambda o: o.startswith(w), lambda o: w in o):
        for orig, low in opts:
            if test(low):
                return orig
    return None


DECLINE = r"(decline|prefer not|don.t wish|do not wish|not to (say|answer|disclose)|choose not|rather not)"


def answer_known(f: Field, profile_basics: dict, answers: dict, job_location: str,
                 files: dict[str, str]) -> tuple[str | None, str]:
    """(answer, why_unresolved). answer None + reason = needs Claude or the user."""
    k, b = f.kind, profile_basics
    links = {l.get("label", "").lower(): l.get("url", "") for l in b.get("links", [])}
    name = (b.get("name") or "").split()
    simple = {
        "first_name": name[0] if name else None,
        "last_name": " ".join(name[1:]) if len(name) > 1 else None,
        "full_name": b.get("name"),
        "email": b.get("email"),
        "phone": b.get("phone"),
        "location": b.get("location"),
        "linkedin": links.get("linkedin"),
        "github": links.get("github"),
        "website": links.get("portfolio") or links.get("website") or links.get("github"),
        "notice_period": answers.get("notice_period"),
        "earliest_start": answers.get("earliest_start"),
        "salary": answers.get("salary_expectation"),
        "relocate": answers.get("willing_to_relocate"),
        "how_heard": answers.get("how_did_you_hear"),
        "pronouns": answers.get("pronouns"),
    }
    if k == "resume":
        return files.get("resume"), "" if files.get("resume") else "no resume file"
    if k == "cover_letter":
        if f.type == "file":
            return files.get("cover_letter"), ""
        return None, "cover letter text"          # filled from the tailored letter by the runner
    if k in simple:
        value = simple[k]
        if not value:
            return None, f"no saved answer for {k.replace('_', ' ')}"
        if f.options:
            opt = pick_option(f.options, value)
            return (opt, "") if opt else (None, f"saved answer {value!r} isn't one of the options")
        return value, ""
    if k in ("work_auth", "sponsorship"):
        table = answers.get("legal", {}).get("authorized_to_work" if k == "work_auth" else "requires_sponsorship", {})
        country = country_for(f.label, job_location)
        value = table.get(country) if country else (next(iter(set(table.values()))) if len(set(table.values())) == 1 else None)
        if not value:
            return None, "legal question with no saved answer" + (f" for {country}" if country else "")
        if f.options:
            opt = pick_option(f.options, value)
            return (opt, "") if opt else (None, "legal answer doesn't match the options")
        return value, ""
    if k == "eeo":
        pref = answers.get("legal", {}).get("eeo", "decline")
        if f.options:
            if pref == "decline":
                opt = next((o for o in f.options if re.search(DECLINE, o.lower())), None)
            else:
                opt = pick_option(f.options, str(pref))
            if opt:
                return opt, ""
        if not f.required:
            return "", ""                           # optional EEO with no decline option: leave blank
        return None, "EEO question with no decline option"
    if k == "consent":
        return ("yes" if f.type == "checkbox" else None), ("" if f.type == "checkbox" else "consent question")
    return None, ""                                 # essay / question / company: Claude's job
