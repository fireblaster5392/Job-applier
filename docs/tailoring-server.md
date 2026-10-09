# Plugging in your own tailoring server

The pipeline gets three things from a **tailoring provider**: your profile, a quick fit score for
each matching job, and a tailored resume + cover letter for the good fits. The built-in provider
does this with Claude and `profile/resume.yaml`. You can replace it with your own MCP server that
holds your information, resume rules, keywords and project list.

Code: `tailor/provider.py` (`TailorProvider`, `ClaudeProvider`, `McpProvider`).
Reference server: `profile_mcp/server.py`, which implements the same three tools on top of the
built-in tailoring. Copy its tool signatures.

## Switch to your server

```toml
# config.toml
[tailor]
provider = "mcp"
mcp_server = "http://127.0.0.1:9000/mcp"     # streamable HTTP
# mcp_server = "python -m my_tailor_server"  # or a command, run over stdio
```

Everything then uses your server: the worker's scoring and tailoring, the Telegram **Tailor**
button, the dashboard's re-tailor and edit pages, the apply queue, and outreach drafting (which
reads the profile). If the server is down, the job gets a plain alert and is retried from the
dashboard, the same as a Claude failure.

## What the pipeline still does with your output

The non-negotiables in ROADMAP.md hold whichever provider runs:

- **Guardrails run on your output**, checked against the profile your own server returns from
  `get_resume_profile`. A bullet whose `id` isn't in that profile is dropped. A reworded bullet
  that adds a number the source bullet doesn't have goes back to the source text. A skill that
  isn't in the profile's `skills` is dropped. The dashboard lists each fix.
- **Legal and EEO answers never come from the server.** They stay in `profile/answers.yaml` on
  the machine running the pipeline.
- **The resume layout comes from `tailor/render.py`** (HTML, then a PDF for uploads). Your server
  decides what goes in (which bullets, their order within each role, wording, summary, skills,
  cover letter). It doesn't decide the template. If your formatting rules need a different layout,
  that template is the place to change, so the fact checks keep working.

## The three tools

Every tool takes plain JSON arguments and returns a JSON object, as structured content or as one
JSON text block.

### `get_resume_profile()`

Returns the whole master profile. The pipeline caches it for 5 minutes. Shape (the same as
`profile/resume.yaml`):

```yaml
basics:
  name: Your Name
  email: you@example.com
  phone: "+1 555 0100"
  location: Calgary, AB
  links: [{label: GitHub, url: "https://github.com/you"}]
  summary: One or two sentences.

experience:                       # newest first
  - company: Example Corp
    title: Software Engineer
    location: Calgary, AB
    start: 2024-05                # YYYY-MM
    end: present
    bullets:
      - id: exco-api              # stable, unique across the whole profile; never reuse
        text: Built a Python/FastAPI service handling 2M requests a day.
        tags: [python, fastapi, backend, api]

projects:                         # as many as you like; the tailoring picks the relevant ones
  - name: Job Watcher
    url: "https://github.com/you/job-watcher"
    bullets:
      - id: proj-watcher
        text: Async Python service polling 40 job boards every 60 seconds.
        tags: [python, asyncio, automation]

education:
  - {school: University of Calgary, degree: BSc Computer Science, end: 2025}

skills:                           # any group names; tailored skills must come from these lists
  languages: [Python, TypeScript, SQL]
  frameworks: [FastAPI, React]
  tools: [Docker, PostgreSQL, AWS]
```

Required by the pipeline: `basics.name`, and an `id`, `text` and `tags` on every bullet. Bullet
ids must be unique, or loading the profile fails.

### `score_fit(job, description)`

```json
{"job": {"title": "Backend Engineer", "company": "acme", "location": "Remote - US",
         "ats": "lever", "url": "https://jobs.lever.co/acme/j1"},
 "description": "full job description text"}
```

Returns `{"score": 0-100, "reasons": ["1-3 short reasons, including any dealbreaker"]}`.
Scores at or above `[pipeline] score_threshold` (70 by default) get tailored and sent to
Telegram. Lower scores show only on the dashboard. Scores outside 0-100 are clamped.

### `tailor_application(job, description)`

Same arguments as `score_fit`. Returns:

```json
{
  "fit_score": 82,
  "fit_reasons": ["2-4 short reasons"],
  "gaps": ["requirements the profile doesn't show"],
  "summary": "2-sentence resume summary aimed at this job, facts from the profile only",
  "bullets": [{"id": "exco-api", "text": "the bullet, lightly reworded; same facts and numbers"}],
  "skills": ["Python", "FastAPI"],
  "cover_letter": "3 short paragraphs, plain text, no greeting or signature",
  "usage": {"model": "whatever-you-used", "input_tokens": 0, "output_tokens": 0}
}
```

`usage` is optional; `usage.model` shows on the dashboard. Bullets go on the resume under their
own role or project, in the order you return them; roles and projects keep the order they have in
the profile. The schema is the
`TailoredApplication` model in `tailor/schema.py`.

## Suggested extras for your profile data

The pipeline ignores keys it doesn't use, so your server can keep its own rules next to the
profile and still return the same file from `get_resume_profile`:

```yaml
keywords:                         # map job-description terms to your tags, for selection
  "distributed systems": [backend, reliability]
  "ml ops": [ml, infra]

formatting:                       # rules your tailoring follows when it picks and words bullets
  max_bullets_total: 10
  max_bullets_per_role: 4
  max_projects: 2
  bullet_style: "action verb, what, measurable result; one line"
  always_include: [exco-api]
  never_include_for: {frontend: [exco-oncall]}

projects:
  - name: ...
    priority: 1                   # your own ranking hint
    tags: [...]                   # project-level tags, in addition to bullet tags
```

Any change that would need the pipeline itself to read one of these keys (for example a page
limit in the PDF renderer) belongs in `tailor/render.py`.

## Checking your server

```bash
python -m unittest tests.test_provider    # the contract, run against a fake server
```

To check a real server, point `config.toml` at it and press **Re-tailor** on any job in the
dashboard. The job page shows the result and any guardrail fixes.
