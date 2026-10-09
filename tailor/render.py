"""Render a tailored resume and cover letter as printable HTML (one page, single column, ATS-friendly).

Print to PDF from the browser, or swap in RenderCV later for typeset PDFs.
"""
from datetime import date

from jinja2 import Environment

from profile_mcp.profile import Profile

from .schema import TailoredApplication

env = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)

RESUME = env.from_string("""<!doctype html>
<html><head><meta charset="utf-8"><title>{{ b.name }} resume</title>
<style>
  @page { size: letter; margin: 0.5in; }
  body { font: 10.5pt/1.35 Helvetica, Arial, sans-serif; color: #111; max-width: 7.5in; margin: 0 auto; }
  h1 { font-size: 18pt; margin: 0; }
  .contact { margin: 2px 0 8px; }
  h2 { font-size: 11pt; text-transform: uppercase; border-bottom: 1px solid #333; margin: 12px 0 4px; }
  .role { display: flex; justify-content: space-between; font-weight: bold; margin-top: 6px; }
  .sub { display: flex; justify-content: space-between; font-style: italic; }
  ul { margin: 2px 0 0 18px; padding: 0; }
  li { margin: 1px 0; }
</style></head><body>
<h1>{{ b.name }}</h1>
<div class="contact">{{ [b.email, b.phone, b.location] | select | join(" · ") }}
{% for l in b.links or [] %} · <a href="{{ l.url }}">{{ l.label }}</a>{% endfor %}</div>
{% if app.summary %}<h2>Summary</h2><p>{{ app.summary }}</p>{% endif %}
{% for section, entries in sections %}
{% if entries %}<h2>{{ section }}</h2>{% endif %}
{% for e in entries %}
<div class="role"><span>{{ e.heading }}</span><span>{{ e.dates }}</span></div>
{% if e.sub %}<div class="sub"><span>{{ e.sub }}</span><span>{{ e.location }}</span></div>{% endif %}
<ul>{% for text in e.bullets %}<li>{{ text }}</li>{% endfor %}</ul>
{% endfor %}
{% endfor %}
{% if app.skills %}<h2>Skills</h2><p>{{ app.skills | join(", ") }}</p>{% endif %}
{% if education %}<h2>Education</h2>
{% for ed in education %}<div class="role"><span>{{ ed.degree }}, {{ ed.school }}</span><span>{{ ed.end }}</span></div>{% endfor %}
{% endif %}
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


def _sections(profile: Profile, app: TailoredApplication):
    chosen = {b.id: (i, b.text) for i, b in enumerate(app.bullets)}
    out = []
    for section, title in (("experience", "Experience"), ("projects", "Projects")):
        entries = []
        for e in profile.data.get(section, []):   # keep the resume's chronological order
            picks = sorted((chosen[b["id"]] for b in e.get("bullets", []) if b["id"] in chosen))
            if not picks:
                continue
            if section == "experience":
                entries.append({"heading": e["title"], "sub": e["company"], "location": e.get("location", ""),
                                "dates": f"{e.get('start', '')} – {e.get('end', '')}", "bullets": [t for _, t in picks]})
            else:
                entries.append({"heading": e["name"], "sub": "", "location": "", "dates": "",
                                "bullets": [t for _, t in picks]})
        out.append((title, entries))
    return out


def resume_html(profile: Profile, app: TailoredApplication) -> str:
    return RESUME.render(b=profile.basics, app=app, sections=_sections(profile, app),
                         education=profile.data.get("education", []))


def cover_letter_html(profile: Profile, app: TailoredApplication, company: str) -> str:
    paragraphs = [p.strip() for p in app.cover_letter.split("\n\n") if p.strip()]
    return LETTER.render(b=profile.basics, company=company, paragraphs=paragraphs,
                         today=date.today().strftime("%B %d, %Y").replace(" 0", " "))
