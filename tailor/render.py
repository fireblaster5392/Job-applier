"""Render a tailored resume and cover letter as printable HTML (single column, ATS-friendly).
Section order, dates, skills layout and font come from profile/format.yaml.

Print to PDF from the browser, or swap in RenderCV later for typeset PDFs.
"""
from datetime import date, datetime

from jinja2 import Environment

from profile_mcp.profile import Profile

from .schema import TailoredApplication

env = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)

RESUME = env.from_string("""<!doctype html>
<html><head><meta charset="utf-8"><title>{{ b.name }} resume</title>
<style>
  @page { size: letter; margin: 0.5in; }
  body { font: {{ f.font_size }}pt/1.35 {{ f.font }}; color: #111; max-width: 7.5in; margin: 0 auto; }
  h1 { font-size: 18pt; margin: 0; }
  .contact { margin: 2px 0 8px; }
  h2 { font-size: 11pt; text-transform: uppercase; border-bottom: 1px solid #333; margin: 12px 0 4px; }
  .role { display: flex; justify-content: space-between; font-weight: bold; margin-top: 6px; }
  .sub { display: flex; justify-content: space-between; font-style: italic; }
  ul { margin: 2px 0 0 18px; padding: 0; }
  li { margin: 1px 0; }
  p { margin: 2px 0; }
</style></head><body>
<h1>{{ b.name }}</h1>
<div class="contact">{{ [b.email, b.phone, b.location] | select | join(" · ") }}
{% for l in b.links or [] %} · <a href="{{ l.url }}">{{ l.label }}</a>{% endfor %}</div>
{% for name in f.sections %}
{% if name == "summary" and app.summary %}<h2>Summary</h2><p>{{ app.summary }}</p>
{% elif name in entries and entries[name] %}<h2>{{ name | title }}</h2>
{% for e in entries[name] %}
<div class="role"><span>{{ e.heading }}</span><span>{{ e.dates }}</span></div>
{% if e.sub %}<div class="sub"><span>{{ e.sub }}</span><span>{{ e.location }}</span></div>{% endif %}
<ul>{% for text in e.bullets %}<li>{{ text }}</li>{% endfor %}</ul>
{% endfor %}
{% elif name == "skills" and skills %}<h2>Skills</h2>
{% for group, items in skills %}<p>{% if group %}<b>{{ group }}:</b> {% endif %}{{ items | join(", ") }}</p>{% endfor %}
{% elif name == "education" and education %}<h2>Education</h2>
{% for ed in education %}<div class="role"><span>{{ ed.degree }}, {{ ed.school }}</span><span>{{ ed.dates }}</span></div>
{% if ed.details %}<ul>{% for d in ed.details %}<li>{{ d }}</li>{% endfor %}</ul>{% endif %}
{% endfor %}
{% endif %}
{% endfor %}
</body></html>""")

LETTER = env.from_string("""<!doctype html>
<html><head><meta charset="utf-8"><title>{{ b.name }} cover letter</title>
<style>
  @page { size: letter; margin: 1in; }
  body { font: 11pt/1.5 Georgia, serif; color: #111; max-width: 6.5in; margin: 0 auto; }
</style></head><body>
<p>{{ b.name }}<br>{{ [b.email, b.phone] | select | join(" · ") }}</p>
<p>{{ today }}</p>
<p>Dear {{ company }} hiring team,</p>
{% for para in paragraphs %}<p>{{ para }}</p>{% endfor %}
<p>Sincerely,<br>{{ b.name }}</p>
</body></html>""")


def _date(value, fmt: str) -> str:
    text = str(value or "")
    if text.lower() == "present":
        return "Present"
    for pattern in ("%Y-%m", "%Y-%m-%d", "%Y"):
        try:
            return datetime.strptime(text, pattern).strftime(fmt if pattern != "%Y" else "%Y")
        except ValueError:
            continue
    return text


def _dates(entry: dict, fmt: str) -> str:
    return " – ".join(_date(entry[k], fmt) for k in ("start", "end") if entry.get(k))


def _entries(profile: Profile, app: TailoredApplication) -> dict:
    chosen = {b.id: (i, b.text) for i, b in enumerate(app.bullets)}
    fmt = profile.format["date_format"]
    out = {}
    for section in ("experience", "projects"):
        entries = []
        for e in profile.data.get(section, []):   # keep the profile's order (newest first)
            picks = sorted((chosen[b["id"]] for b in e.get("bullets", []) if b["id"] in chosen))
            if not picks:
                continue
            if section == "experience":
                entries.append({"heading": e["title"], "sub": e["company"], "location": e.get("location", ""),
                                "dates": _dates(e, fmt), "bullets": [t for _, t in picks]})
            else:
                entries.append({"heading": e["name"], "sub": e.get("role", ""), "location": "",
                                "dates": _dates(e, fmt), "bullets": [t for _, t in picks]})
        out[section] = entries
    return out


def _skills(profile: Profile, app: TailoredApplication) -> list[tuple[str, list[str]]]:
    if not app.skills:
        return []
    if not profile.format["group_skills"]:
        return [("", app.skills)]
    picked = {s.lower() for s in app.skills}
    groups = [(g, [s for s in items if str(s).lower() in picked])
              for g, items in profile.data.get("skills", {}).items()]
    return [(g, items) for g, items in groups if items]


def resume_html(profile: Profile, app: TailoredApplication) -> str:
    fmt = profile.format["date_format"]
    education = [{**ed, "dates": _dates(ed, fmt) if ed.get("start") else _date(ed.get("end"), fmt)}
                 for ed in profile.data.get("education", [])]
    return RESUME.render(b=profile.basics, app=app, f=profile.format, entries=_entries(profile, app),
                         skills=_skills(profile, app), education=education)


def cover_letter_html(profile: Profile, app: TailoredApplication, company: str) -> str:
    paragraphs = [p.strip() for p in app.cover_letter.split("\n\n") if p.strip()]
    return LETTER.render(b=profile.basics, company=company, paragraphs=paragraphs,
                         today=date.today().strftime("%B %d, %Y").replace(" 0", " "))
