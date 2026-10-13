# Tailoring server contract

The job-search MCP server in `profile_mcp/server.py` is the tailoring server, seeded from your
files in `profile/` (what to fill in: [profile/README.md](../profile/README.md)). This page is the
contract between it and the pipeline. It's useful if you ever want to swap in a different server,
or call the server from another tool.

The pipeline gets three things from a **tailoring provider**: your profile, a quick fit score for
each matching job, and a tailored resume + cover letter for the good fits. By default it runs the
same code as the server in-process. With `provider = "mcp"` it calls a server over MCP instead.

Code: `tailor/provider.py` (`TailorProvider`, `ClaudeProvider`, `McpProvider`).
Reference server: `profile_mcp/server.py`.

## Calling a server over MCP instead of in-process

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
- **format.yaml limits are enforced** after tailoring (bullet counts, projects, skills,
  always/never include), using the `format` in the returned profile.
- **The resume layout comes from `tailor/render.py`**, driven by `format` (section order, dates,
  skills grouping, font). The server decides what goes in. The template decides how it's laid out.

## The three tools

Every tool takes plain JSON arguments and returns a JSON object, as structured content or as one
JSON text block.

### `get_resume_profile()`

Returns the whole merged profile, which is what `profile_mcp.profile.load_seed` builds from the seed
files. The pipeline caches it for 5 minutes. Keys:

| Key | From | Shape |
|---|---|---|
| `basics` | me.yaml | `name, email, phone, location, links[], summary` (summary = your headline) |
| `targets` | me.yaml | `roles[], not_interested[], seniority` |
| `education` | me.yaml | list of `school, degree, start, end, details[]` |
| `experience` | experience.yaml | list of `company, title, location, start, end, tech[], keywords[], metrics[], bullets[{id, text, tags[]}]` |
| `projects` | projects/*.yaml | list of `name, role, url, start, end, priority, summary, tech[], keywords[], metrics[], bullets[...]`, newest first |
| `skills` | skills.yaml `groups` | `{group name: [skills]}` |
| `keywords` | skills.yaml `keywords` | `{posting term: [what it means in your files]}` |
| `format` | format.yaml | the settings in profile/README.md |

Required: `basics.name`, and an `id` and `text` on every bullet, with ids unique.

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
  "bullets": [{"id": "exco-api", "text": "the bullet, rewritten for this job; same facts and numbers"}],
  "skills": ["Python", "FastAPI"],
  "cover_letter": "3 short paragraphs, plain text, no greeting or signature",
  "usage": {"model": "whatever-you-used", "input_tokens": 0, "output_tokens": 0}
}
```

`usage` is optional; `usage.model` shows on the dashboard. Bullets go on the resume under their
own role or project, in the order you return them; roles and projects keep the order they have in
the profile. The schema is the
`TailoredApplication` model in `tailor/schema.py`.

## Checking your server

```bash
python -m unittest tests.test_provider    # the contract, run against a fake server
```

To check a real server, point `config.toml` at it and press **Re-tailor** on any job in the
dashboard. The job page shows the result and any guardrail fixes.
