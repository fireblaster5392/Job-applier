"""Browser-agent fallback for application forms the generic filler can't handle (Workday, iCIMS,
SmartRecruiters, Taleo, multi-page company portals).

Claude drives the already-open Playwright page through a few DOM tools: look at the page, fill,
choose, check, click, upload the tailored PDFs, and finish. It works within the same rules as the
rest of the pipeline:

- Legal and EEO questions only get answers copied from answers.yaml; anything else stops with
  needs_you. Facts come from the profile; free text it writes is shown to you before submitting.
- It never submits on the first pass. It stops at the final review/submit step with "ready", and
  you get the answers and a screenshot. Only after your Submit tap does a second pass fill the
  form again with your approved answers and click Submit.
- CAPTCHAs, email verification and anything it can't do confidently come back to you. It never
  tries to get around a site's checks.
- Sign-in or account creation uses APPLY_LOGIN_EMAIL / APPLY_LOGIN_PASSWORD from .env if you set
  them. The password is typed by the code and never shown to Claude.
"""
import json
import logging
import os
import random
import re

import anthropic

from profile_mcp.profile import Profile
from tailor.schema import TailoredApplication

from .browser import CAPTCHA_SELECTORS, SUCCESS

log = logging.getLogger(__name__)
DEFAULT_AGENT_MODEL = "claude-sonnet-5-5"
MAX_STEPS = 60
FINAL_SUBMIT = re.compile(r"^\s*(submit( (my )?application)?|send application|apply now|complete application|finish)\s*$", re.I)

# Tags every visible interactive element with data-ja-el and describes it.
SNAPSHOT_JS = r"""
() => {
  const visible = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0 || el.type === 'file'); };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const labelFor = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) return clean(lb.split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' '));
    const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    if (['BUTTON', 'A'].includes(el.tagName) || el.getAttribute('role')) return clean(el.innerText || el.value || el.title);
    const box = el.closest('[data-automation-id], .field, .form-group, [class*="question"], [class*="field"], li, div');
    if (box) { const l = box.querySelector('label, [class*="label"]'); if (l) return clean(l.innerText); }
    return clean(el.placeholder || el.name || el.title || '');
  };
  const sel = 'input, textarea, select, button, a[href], [role="button"], [role="combobox"], [role="option"], ' +
              '[role="checkbox"], [role="radio"], [role="listbox"], [role="menuitem"], [role="tab"]';
  for (const old of document.querySelectorAll('[data-ja-el]')) delete old.dataset.jaEl;   // ids from the last look
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (!visible(el) || el.disabled) continue;
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('role') || (tag === 'input' ? (el.type || 'text').toLowerCase() : tag);
    if (type === 'hidden') continue;
    const id = `e${n++}`; el.dataset.jaEl = id;
    const item = { id, type, label: labelFor(el) };
    if (['text', 'email', 'tel', 'url', 'number', 'textarea', 'date', 'combobox'].includes(type) && el.value) item.value = clean(el.value);
    if (type === 'checkbox' || type === 'radio') item.checked = el.checked || el.getAttribute('aria-checked') === 'true';
    if (type === 'password') item.value = el.value ? '(filled)' : '';
    if (tag === 'select') item.options = [...el.options].map(o => clean(o.text)).filter(Boolean).slice(0, 60);
    if (el.required || el.getAttribute('aria-required') === 'true') item.required = true;
    if (el.getAttribute('aria-invalid') === 'true') item.invalid = true;
    out.push(item);
    if (out.length >= 250) break;
  }
  return out;
}
"""

TOOLS = [
    {"name": "look", "description": "See the current page: URL, visible text, and every interactive element with "
     "an id to act on. Call it after anything that changes the page.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "fill", "description": "Type a value into a text field or textarea (replaces what's there).",
     "input_schema": {"type": "object", "properties": {"element": {"type": "string"}, "value": {"type": "string"},
                      "label": {"type": "string", "description": "the question, for the record"},
                      "source": {"type": "string", "enum": ["profile", "answers", "tailored", "claude"],
                                 "description": "where the value comes from; 'claude' = you wrote it"}},
                      "required": ["element", "value", "label", "source"]}},
    {"name": "choose", "description": "Pick an option in a select, combobox/dropdown or radio group by its visible text.",
     "input_schema": {"type": "object", "properties": {"element": {"type": "string"}, "option": {"type": "string"},
                      "label": {"type": "string"},
                      "source": {"type": "string", "enum": ["profile", "answers", "tailored", "claude"]}},
                      "required": ["element", "option", "label", "source"]}},
    {"name": "check", "description": "Tick or untick a checkbox.",
     "input_schema": {"type": "object", "properties": {"element": {"type": "string"}, "checked": {"type": "boolean"},
                      "label": {"type": "string"}}, "required": ["element", "checked", "label"]}},
    {"name": "click", "description": "Click a button, link or tab (Next, Save and continue, Apply, Add...). "
     "The final Submit is refused unless the user already approved this application.",
     "input_schema": {"type": "object", "properties": {"element": {"type": "string"}}, "required": ["element"]}},
    {"name": "upload", "description": "Attach the tailored resume or cover letter PDF to a file input.",
     "input_schema": {"type": "object", "properties": {"element": {"type": "string"},
                      "file": {"type": "string", "enum": ["resume", "cover_letter"]}}, "required": ["element", "file"]}},
    {"name": "sign_in_field", "description": "Type the user's saved login email or password into a sign-in or "
     "create-account field. You never see the password.",
     "input_schema": {"type": "object", "properties": {"element": {"type": "string"},
                      "field": {"type": "string", "enum": ["email", "password"]}}, "required": ["element", "field"]}},
    {"name": "finish", "description": "Stop. outcome: 'ready' = everything is filled and the next click would be the "
     "final submit; 'submitted' = you clicked the approved final submit; 'needs_you' = a CAPTCHA, email "
     "verification, a question you may not answer, or anything you can't do confidently; 'failed' = the "
     "page is broken.",
     "input_schema": {"type": "object", "properties": {
         "outcome": {"type": "string", "enum": ["ready", "submitted", "needs_you", "failed"]},
         "reasons": {"type": "array", "items": {"type": "string"},
                     "description": "short, for the user: what's left or what went wrong"}},
         "required": ["outcome", "reasons"]}},
]

SYSTEM = """You fill in one job application in a web browser for the candidate below, using tools.

Rules, in order of priority:
1. Never get around a site's protections. If you see a CAPTCHA, a bot check, or a request to verify
   an email or phone, call finish with needs_you.
2. Legal and demographic questions (work authorization, sponsorship, visa, citizenship, criminal
   history, gender, race/ethnicity, veteran, disability) are answered ONLY with the matching value in
   "Saved answers", copied exactly or the option that says the same thing. If there's no matching
   saved answer, call finish with needs_you and name the question. Never infer one.
3. Contact details, work history, education and links come from the profile, exactly as written.
   Never invent a fact, date, employer, number or skill.
4. Salary, notice period and start date come only from Saved answers; if blank, needs_you.
5. Custom free-text questions ("Why this company?") may be answered from the profile and the job
   description, first person, specific, 2-5 sentences, source "claude". If the profile can't support
   an answer to a required question, needs_you.
6. {submit_rule}
7. Sign in or create an account only with sign_in_field, and only if the user has saved a login
   ({login}). Otherwise, if the site requires an account, call finish with needs_you.
8. Prefer the site's own "Autofill with resume" option only if it doesn't need anything else; then
   check and correct every autofilled field against the profile.

Work page by page: look, fill everything on the page, click Next / Save and continue, look again.
If a field shows an error after you continue, fix it. Keep going until rule 6 tells you to stop.

Job: {title} at {company}
Application URL: {url}

Saved answers (answers.yaml; legal answers must be copied exactly):
{answers}

Contact details: {basics}

Cover letter (for a cover letter text box; otherwise upload the PDF):
{cover_letter}

Profile:
{profile}
{approved}"""

SUBMIT_FIRST = ("Do NOT click the final Submit / Submit application / Send / Apply button. When the "
                "only thing left is that final click (often a Review page), call finish with 'ready'.")
SUBMIT_APPROVED = ("The user has approved this application. Fill everything again using the approved "
                   "answers below, then click the final Submit and call finish with 'submitted'.")


class AgentBrowser:
    """The tools, executed against a Playwright page."""

    def __init__(self, page, files: dict[str, str], allow_submit: bool, login: dict[str, str] | None = None,
                 pause=(250, 900)):
        self.page, self.files, self.allow_submit, self.login = page, files, allow_submit, login or {}
        self.pause = pause
        page.set_default_timeout(10000)   # a missing element fails fast, so Claude can look again
        self.answers: list[dict] = []
        self.submitted = False

    def _el(self, element: str):
        loc = self.page.locator(f"[data-ja-el='{element}']")
        if loc.count() == 0:
            raise ValueError(f"no element {element!r} on the page now; call look")
        return loc.first

    def _wait(self):
        self.page.wait_for_timeout(random.randint(*self.pause))

    def captcha(self) -> bool:
        return any(self.page.locator(s).count() for s in CAPTCHA_SELECTORS)

    def _record(self, label, answer, source, type_="text"):
        self.answers = [a for a in self.answers if a["label"] != label]
        self.answers.append({"label": label, "type": type_, "key": f"agent|{label.lower()}",
                             "answer": answer, "source": source})

    def look(self) -> str:
        if self.captcha():
            return json.dumps({"captcha": True, "note": "A CAPTCHA is on the page: call finish with needs_you."})
        elements = self.page.evaluate(SNAPSHOT_JS)
        text = re.sub(r"\n\s*\n+", "\n", self.page.inner_text("body"))[:4000]
        return json.dumps({"url": self.page.url, "title": self.page.title(), "text": text, "elements": elements})

    def fill(self, element, value, label, source):
        el = self._el(element)
        el.fill(value)
        self._record(label, value, source, "textarea" if el.evaluate("e => e.tagName") == "TEXTAREA" else "text")
        self._wait()
        return "ok"

    def choose(self, element, option, label, source):
        el = self._el(element)
        tag = el.evaluate("e => e.tagName")
        if tag == "SELECT":
            el.select_option(label=option)
        elif el.get_attribute("type") == "radio" or el.get_attribute("role") == "radio":
            el.check(force=True) if tag == "INPUT" else el.click()
        else:   # custom dropdown: open it, then click the option by its text
            el.click()
            self.page.wait_for_timeout(400)
            target = self.page.get_by_role("option", name=option, exact=True)
            if target.count() == 0:
                target = self.page.get_by_text(option, exact=True)
            if target.count() == 0:
                self.page.keyboard.press("Escape")
                return f"option {option!r} not found; call look to see the options"
            target.first.click()
        self._record(label, option, source, "choice")
        self._wait()
        return "ok"

    def check(self, element, checked, label):
        el = self._el(element)
        if el.evaluate("e => e.tagName") == "INPUT":
            el.set_checked(checked, force=True)
        elif (el.get_attribute("aria-checked") == "true") != checked:
            el.click()
        self._record(label, "yes" if checked else "no", "profile", "checkbox")
        self._wait()
        return "ok"

    def click(self, element):
        el = self._el(element)
        text = (el.inner_text() or el.get_attribute("value") or "").strip()
        if FINAL_SUBMIT.match(text):
            if not self.allow_submit:
                return "refused: that's the final submit. Call finish with 'ready'."
            el.click()
            try:
                self.page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(1500)
            self.submitted = bool(SUCCESS.search(self.page.inner_text("body")[:20000]))
            return "clicked submit; " + ("the page confirms the application." if self.submitted
                                         else "no confirmation yet; call look.")
        el.click()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        self._wait()
        return "ok"

    def upload(self, element, file):
        path = self.files.get(file)
        if not path:
            return f"no {file} file"
        el = self._el(element)
        if el.get_attribute("type") != "file":   # a styled button in front of a hidden input
            with self.page.expect_file_chooser(timeout=5000) as fc:
                el.click()
            fc.value.set_files(path)
        else:
            el.set_input_files(path)
        self._wait()
        return "ok"

    def sign_in_field(self, element, field):
        value = self.login.get(field)
        if not value:
            return "no saved login; call finish with needs_you"
        self._el(element).fill(value)
        self._wait()
        return "ok"


def run_agent(page, *, job: dict, profile: Profile, app: TailoredApplication, files: dict[str, str],
              model: str = DEFAULT_AGENT_MODEL, client: anthropic.Anthropic | None = None,
              approved: list[dict] | None = None, max_steps: int = MAX_STEPS) -> dict:
    """Returns {outcome: submitted|needs_review|needs_you|failed, answers, reasons, usage}."""
    client = client or anthropic.Anthropic()
    login = {k: v for k, v in (("email", os.environ.get("APPLY_LOGIN_EMAIL")),
                               ("password", os.environ.get("APPLY_LOGIN_PASSWORD"))) if v}
    tools = AgentBrowser(page, files, allow_submit=approved is not None, login=login)
    approved_text = ""
    if approved is not None:
        approved_text = "\nApproved answers (use exactly):\n" + "\n".join(
            f"- {a['label']}: {a['answer']}" for a in approved)
    system = SYSTEM.format(
        submit_rule=SUBMIT_APPROVED if approved is not None else SUBMIT_FIRST,
        login="saved" if login.get("email") and login.get("password") else "none saved",
        title=job["title"], company=job["company"], url=page.url,
        answers=json.dumps(profile.answers, indent=1, default=str) if profile.answers else "(none saved)",
        basics=json.dumps(profile.basics, default=str), cover_letter=app.cover_letter,
        profile=profile.as_prompt_text(), approved=approved_text)
    messages = [{"role": "user", "content": "Start with look."}]
    usage = {"model": model, "input_tokens": 0, "output_tokens": 0, "steps": 0}
    for _ in range(max_steps):
        try:
            resp = client.messages.create(model=model, max_tokens=4000, tools=TOOLS,
                                          system=[{"type": "text", "text": system,
                                                   "cache_control": {"type": "ephemeral"}}],
                                          messages=messages)
        except anthropic.APIError as exc:
            return _result("failed", tools, [f"Claude API error: {exc}"], usage)
        usage["steps"] += 1
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            return _result("needs_you", tools, ["The browser agent stopped without finishing."], usage)
        results = []
        for call in calls:
            if call.name == "finish":
                return _finish(call.input, tools, usage)
            try:
                out = getattr(tools, call.name)(**call.input)
            except Exception as exc:  # noqa: BLE001 - tell Claude, let it adapt
                out = f"error: {type(exc).__name__}: {str(exc)[:300]}"
            if tools.captcha() and call.name != "look":
                return _result("needs_you", tools, ["The form showed a CAPTCHA."], usage)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": out})
        # Keep the context small: old page snapshots are replaced with a stub.
        for m in messages[:-1]:
            if m["role"] == "user" and isinstance(m["content"], list):
                for r in m["content"]:
                    if r.get("type") == "tool_result" and str(r.get("content", "")).startswith('{"url"'):
                        r["content"] = "(old page snapshot)"
        messages.append({"role": "user", "content": results})
    return _result("needs_you", tools, [f"The browser agent didn't finish in {max_steps} steps."], usage)


def _finish(args: dict, tools: AgentBrowser, usage: dict) -> dict:
    outcome, reasons = args.get("outcome"), list(args.get("reasons") or [])
    if outcome == "submitted":
        if tools.submitted:
            return _result("submitted", tools, [], usage)
        return _result("failed", tools, reasons + ["Submit was clicked but the page didn't confirm it."], usage)
    if outcome == "ready":
        return _result("needs_review", tools, ["Filled by the browser agent; check the answers before it submits."]
                       + reasons, usage)
    return _result("needs_you" if outcome == "needs_you" else "failed", tools,
                   reasons or ["The browser agent couldn't finish."], usage)


def _result(outcome, tools: AgentBrowser, reasons, usage) -> dict:
    return {"outcome": outcome, "answers": tools.answers, "reasons": reasons, "usage": usage}
