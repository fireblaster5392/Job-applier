# profile/notes/: rough notes in, profile files out

Don't want to write polished bullets yourself? Dump what you know here in any .md or .txt file:
your skills, every job and project, what you built, the tools, and any numbers you remember.
Messy is fine. These files are gitignored.

```bash
python -m profile_mcp.draft            # drafts profile/draft/ from these notes
# read profile/draft/ and QUESTIONS.md, edit anything, answer questions in your notes, re-run
python -m profile_mcp.draft --apply    # copies the draft into profile/ (old files backed up)
python -m profile_mcp.check
```

The draft writes every bullet the way recruiters skim: strong verb, what you built, the tools,
the result. Each job and project gets several bullets from different angles (backend, ML, infra,
product), so each tailored resume can pick the ones that fit the posting. It never invents a
number or claim: a bullet with a number that isn't in your notes is dropped, and where a number
would help, QUESTIONS.md asks you for it.

A starting template:

```
Skills: Python, TypeScript, ...

Project: <name> (Jan 2025 - Apr 2025), team of 3, I owned the backend
Link: https://...
What it does: ...
What I built: ...
Tools: ...
Numbers: 2,000 users, p95 under 100 ms, ...

Job: <company>, <title>, <city> (May 2024 - present)
What I built or owned: ...
Tools: ...
Numbers: ...
```
