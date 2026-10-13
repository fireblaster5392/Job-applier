# profile/: what you fill in

Everything a resume or cover letter says comes from these files. The job-search MCP server
(`profile_mcp/server.py`) loads them, the pipeline tailors from them, and the checks make sure
nothing in a tailored resume goes beyond them. Every file here is a working placeholder, so
replace the values and keep the shape.

```
profile/
  me.yaml               who you are: contact details, links, headline, targets, education
  experience.yaml       every job and internship, with every bullet you've written
  projects/             one .yaml file per project; as many as you like
  skills.yaml           skill groups, plus the keywords job postings use
  format.yaml           how the resume should look
  answers.yaml          job-application form answers (copy answers.example.yaml; never committed)
```

Starting from scratch? Write rough notes in `profile/notes/` and run `python -m profile_mcp.draft`.
It drafts experience.yaml, projects/ and skills.yaml for you, with recruiter-style bullets from
several angles and questions where a number would help. See [notes/README.md](notes/README.md).

After any edit, run this check:

```bash
python -m profile_mcp.check
```

It lists errors (which stop the profile loading) and warnings, then counts what it found. The
same check is the `check_profile` tool in Claude. The server picks up changes on the next call,
so there's no restart.

## The one rule

**Write bullets that are true as written.** Tailoring picks the bullets that fit each job and may
rewrite them for the role, the way its recruiter wants to read them, using the posting's terms. It can't add a fact, tool, number or claim.
A reworded bullet may only use a number if that number is in the bullet itself or in its role's or
project's `metrics`. Anything else is reverted to your text, and the dashboard shows the fix. So
put your real numbers in. More true bullets give it more to choose from.

## me.yaml

| Field | What it's for |
|---|---|
| `name`, `email`, `phone`, `location` | Resume header and cover letter, as written. |
| `links` | Header links in order, each a `{label, url}`. |
| `headline` | How you describe yourself in one line. The tailoring writes a fresh summary for each job, in this voice. |
| `targets.roles`, `targets.not_interested`, `targets.seniority` | Steer fit scoring: jobs you're not interested in score low. Never printed. |
| `education[]` | `school`, `degree`, `start`, `end`, and optional `details` lines (GPA, awards), printed as written. |

## experience.yaml

A list of roles, newest first.

| Field | Required | What it's for |
|---|---|---|
| `company`, `title`, `start` | yes | Printed. Dates are `YYYY-MM`. `end: present` for a current job. |
| `location`, `end` | | Printed. |
| `tech` | | Tools you used there. Added to every bullet's tags so postings that mention them find these bullets. |
| `keywords` | | Domains and themes (payments, ML, infra). Also added to every bullet's tags. |
| `metrics` | | Facts with numbers ("2M requests a day") that any bullet of this role may quote when reworded. |
| `bullets[]` | yes | `id`, `text`, optional `tags`. |

**Bullet ids** are lowercase letters, digits and dashes (`exco-api`). Each one must be unique
across all files and must never be reused for a different bullet: past applications refer to them.
Write as many bullets per role as you have. The resume shows up to `max_bullets_per_role`.

## projects/*.yaml

One file per project, named however you like. A single `projects.yaml` holding a list also works.
Projects go on the resume newest first.

| Field | Required | What it's for |
|---|---|---|
| `name` | yes | Printed as the heading. |
| `role` | | Printed under the name ("Team of 3, model owner"). |
| `start`, `end` | | Printed, and used to sort. |
| `url` | | For you and for Claude; not printed yet. |
| `summary` | | One line on what it is. Context for the tailoring, not printed. |
| `priority` | | 1 = show it often, 5 = only when it's a strong match. |
| `tech`, `keywords`, `metrics`, `bullets` | bullets: yes | Same as for a role. |

A large library is fine. Once you have more than `shortlist` bullets (80 by default), each job only
shows the tailoring the most relevant ones plus `always_include` and the top bullets of each role.
That keeps it fast and cheap.

## skills.yaml

```yaml
groups:                       # printed under these names, in this order
  Languages: [Python, Go]
  Infrastructure: [Docker, Kubernetes]
keywords:                     # what postings say -> what your files say
  k8s: [kubernetes]
  ml: [machine learning, pytorch]
```

A tailored resume can only list skills from `groups`, and anything else is dropped. Each
`keywords` entry lets a posting's word find your bullets: with `k8s: [kubernetes]`, "k8s" in a
posting matches bullets tagged Kubernetes. Phrases work too ("distributed systems").

## format.yaml

Every rule applies to every resume. Leave a setting out to keep its default.

| Setting | Default | What it does |
|---|---|---|
| `sections` | summary, experience, projects, skills, education | Order of sections; remove one to hide it. |
| `pages` | 1 | Target length, given to the tailoring. |
| `max_bullets_total` | 12 | Experience plus projects. Projects are trimmed first. |
| `max_bullets_per_role` / `min_bullets_per_role` | 4 / 1 | Every role keeps at least the minimum, so there are no gaps in your history. |
| `max_projects` / `max_bullets_per_project` | 3 / 2 | |
| `max_skills` | 14 | |
| `group_skills` | true | Skills under your group names, or as one line. |
| `always_include` / `never_include` | none | Bullet ids that go on every resume, or never. |
| `date_format` | `%b %Y` | May 2024. `%Y-%m` gives 2024-05, `%m/%Y` gives 05/2024. |
| `font`, `font_size` | Helvetica 10.5 | |
| `bullet_style`, `summary_style` | | Your wording rules, given to the tailoring. Facts never change. |
| `cover_letter.paragraphs`, `cover_letter.style` | 3 | |
| `shortlist` | 80 | See projects. 0 always sends everything. |

The counts and include/exclude lists are enforced after tailoring, whatever the model returns.
Style notes are instructions, so check a few drafts on the dashboard.

## answers.yaml

Copy `answers.example.yaml`. These are form answers (work authorization per country, sponsorship,
EEO, notice period, salary). Legal answers are used exactly as written and are never generated.
A form question this file doesn't cover comes back to you. The file is gitignored and stays on
the machine running the pipeline.

## How the server uses all this

| Tool | Uses |
|---|---|
| `check_profile` | Validates every file and counts roles, projects, bullets and skills. |
| `get_profile` | Contact details, skills, education, answers. |
| `search_experience(query)` | Bullets ranked against the query, with tech, keywords and your keyword synonyms. |
| `get_resume_profile` | Everything above merged into one object. The pipeline reads this. |
| `score_fit(job, description)` | 0-100 fit, using your roles, projects, skills and `targets`. |
| `tailor_application(job, description)` | Picks and words bullets, skills, summary and cover letter, following format.yaml; then the checks run. |
| `render_resume(selection, company, slug)` | HTML resume and cover letter laid out per format.yaml. |
| `list_jobs`, `get_job` | Your job pipeline. |

`stories/*.md` is for STAR stories and longer write-ups with real numbers. It feeds interview prep
later and isn't used for resumes yet.
