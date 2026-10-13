# Roadmap

## Final goal

A personal job-search assistant that runs 24/7 on a small server and turns new postings into
strong, honest applications and real conversations, with the user making every decision that
leaves their hands.

**End state, in one pass:**

1. New jobs at target companies are found within about 2 minutes of being posted.
2. Each match gets a quick fit score. Good fits are tailored right away (resume + cover letter
   from the user's real experience) and arrive on the user's phone as one message, ready to approve.
3. One tap on **Approve** submits the application at a human pace. Anything the system can't do
   confidently (a CAPTCHA, an essay question, an unknown legal question) comes back to the user
   instead of being guessed.
4. After applying, it finds the hiring manager, engineers and recruiters, and drafts short notes.
   The user sends LinkedIn notes; emails go out on the user's tap.
5. The inbox is watched: replies, confirmations, assessments, interviews, rejections and offers
   update each job automatically, and the important ones ping the user's phone.
6. Daily networking keeps a steady flow of conversations with startups, even with no open role,
   so referrals exist before jobs do.
7. Follow-ups are drafted when someone goes quiet; interview prep sheets are drafted when an
   interview lands; a weekly review shows what's working so targets and thresholds improve.

**Non-negotiables.** If code, a prompt or a feature request conflicts with these, these win:

- **Nothing leaves under the user's name without their tap.** Applications submit only after
  Approve; every email needs its own Send tap. No auto-send "fast lane" unless the user turns it on.
- **Never log into or automate the user's LinkedIn.** Find people through search APIs; the user
  sends LinkedIn messages themselves.
- **No invented resume content.** Tailoring only selects and rewords bullets from
  the seed files in `profile/`, keeping every fact and number; guardrails drop unknown bullets, new numbers and unlisted skills.
  Legal answers (work authorization, sponsorship, EEO) come verbatim from `profile/answers.yaml`.
- **Human pacing, no evasion.** Applications are spaced out with daily caps. No CAPTCHA solvers,
  no fingerprint spoofing, no bypassing a site's protections: CAPTCHAs go to the user.
- **Secrets stay out of git.** Keys and passwords live in `.env`.
- **Everything is tracked.** Every job, message, reply and status change is in the database and
  visible on the dashboard.

## Build steps

| # | Step | Status |
|---|---|---|
| 1 | **Job watcher**: Greenhouse, Lever, Ashby APIs every 60 s, dedupe in SQLite | ✅ Built |
| 2 | **Profile**: seed files (me, experience, project library, skills + keywords, format rules, answers) with a checker; a drafter that turns rough notes into recruiter-style bullets from several angles (`python -m profile_mcp.draft`); job-search MCP server (stdio locally, HTTP `mcp` service on the VPS) that tailors from them | ✅ Built (placeholder data; user fills the seed) |
| 3 | **Fit scoring**: Haiku scores every match 0-100 | ✅ Built |
| 3b | **Tailoring interface**: profile, scoring and tailoring behind one provider; built-in by default, or any MCP server that meets `docs/tailoring-server.md` | ✅ Built |
| 4 | **Tailor first, approve once**: score ≥ threshold → tailored resume + cover letter → one Telegram message with Approve / Edit / Skip; lower scores listed on the dashboard | ✅ Built |
| 5 | **Dashboard**: pipeline, tailored docs, editor, approval buttons | ✅ Built |
| 6 | **Auto-apply**: fill Greenhouse / Lever / Ashby forms, upload PDFs, submit on Approve, human pacing and daily cap, CAPTCHAs and essay questions handed to the user | ✅ Built (tested on local form copies; needs a live dry run) |
| 6b | **Browser agent** for Workday, iCIMS, Taleo, SmartRecruiters and other portals: Claude fills page by page, stops at the final Submit for review, submits on the user's tap; legal answers only from answers.yaml; CAPTCHAs and verification go to the user | ✅ Built (tested on a local Workday-style form; needs a live dry run) |
| 7 | **Post-apply outreach**: find contacts (Brave LinkedIn search + Apollo), draft notes and emails, email on tap | ✅ Built |
| 8 | **Inbox tracking**: replies matched to contacts; application emails classified into applied / assessment / interview / rejected / offer | ✅ Built |
| 9 | **Daily networking**: 5 founders/recruiters a day, 90-day cooldown, tap to send | ✅ Built |
| 10 | **Follow-ups**: one drafted follow-up after 5 days without a reply | ✅ Built |
| 11 | **Interview prep sheets**: on an interview email, draft company notes, role summary, likely questions and matching stories | ✅ Built |
| 12 | **Weekly review**: response rates by source, fit score, role and contact type; suggested threshold and target changes (Telegram on Sundays + `/review`) | ✅ Built |
| 13 | **More sources**: Indeed, Glassdoor and LinkedIn job search via JobSpy (`discover` service), deduped against company boards | ✅ Built (live check pending: the build sandbox can't reach those sites) |
| 13b | **HN Who's Hiring**, Workday job listings as a source | ⏳ Later |
| 14 | **Deploy**: server setup script, Docker Compose for all 8 services, Tailscale-only access, nightly backups, update script (`docs/deploy.md`) | ✅ Built (needs the user's server; images not built in the sandbox) |
| 15 | **Live run**: real profile, keys and a server; one dry-run day, then real applications | ⏳ Waiting on the user |

## Decisions (from the project chat, 2026-10-09)

- **Telegram + web dashboard**, not a mobile app.
- **Tailor first, approve once** instead of approving each raw job first; nothing is missed
  because low scorers stay visible on the dashboard.
- **Apply first, then ask for a referral**, unless a referral can come within a day or two.
- **No CAPTCHA bots.** CAPTCHAs are handed to the user; pacing keeps volume human.
- **Outreach after applying**: hiring manager first, then engineers, then recruiters.
- **Networking runs separately** from job applications, every morning.
- **Claude builds the profile/tailoring MCP server; the user supplies the seed data** (their info,
  resume rules, keywords, project library) in `profile/`. A different server can still plug in via
  `tailor/provider.py`; guardrails and `answers.yaml` stay on the pipeline side.
- **Discovery covers LinkedIn's logged-out job search** (requested 2026-10-11). It never touches
  the user's account; remove "linkedin" from `[discovery] sites` to stop it.
- **Workday-style portals go to a browser agent** that stops before Submit; an account is only
  created or used with the login the user puts in `.env`.
- **Models**: Haiku for scoring and email classification, Opus for tailoring and drafting
  (switchable to Sonnet in `config.toml` to cut cost).

## User to-do before a real run

- Fill the seed files in `profile/` (me, experience, projects/, skills, format; see `profile/README.md`;
  or upload the real resume and Claude converts it), run `python -m profile_mcp.check`, and fill `profile/answers.yaml`.
- Create a Telegram bot token, an Anthropic API key, and optionally Brave / Apollo keys and a
  Gmail app password; put them in `.env`.
- Install the Claude GitHub App on the repository so the code can be pushed.
- Rent a server (2 vCPU / 4 GB, Ubuntu 24.04) and follow `docs/deploy.md`.
