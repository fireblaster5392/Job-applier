"""Render the tailored resume and cover letter to PDF with the same headless Chromium."""
from pathlib import Path

from profile_mcp.profile import Profile
from tailor.render import cover_letter_html, resume_html
from tailor.schema import TailoredApplication


def render_pdfs(browser, profile: Profile, app: TailoredApplication, company: str, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = "-".join((profile.basics.get("name") or "resume").split())
    files = {"resume": (out_dir / f"{name}-Resume.pdf", resume_html(profile, app)),
             "cover_letter": (out_dir / f"{name}-Cover-Letter.pdf", cover_letter_html(profile, app, company))}
    page = browser.new_page()
    try:
        for path, html in files.values():
            page.set_content(html, wait_until="load")
            page.pdf(path=str(path), format="Letter", print_background=True)
    finally:
        page.close()
    return {k: str(p) for k, (p, _) in files.items()}
