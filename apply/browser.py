"""Playwright helpers: open an application form, read its fields, fill them, submit.

Generic and label-driven, so the same code handles Greenhouse, Lever and Ashby forms. Real forms
change; anything it can't read or fill confidently is reported instead of guessed.
"""
import os
import re

from .fields import Field

# Marks every visible field with data-ja-id and returns its label, type, options and required flag.
EXTRACT_JS = r"""
() => {
  const visible = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0 || el.type === 'file'); };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const labelFor = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) return clean(lb.split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' '));
    const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    const box = el.closest('.field, .application-question, .form-group, [class*="question"], [class*="field"], li, div');
    if (box) { const l = box.querySelector('label, .application-label, [class*="label"]'); if (l) return clean(l.innerText); }
    return clean(el.placeholder || el.name || '');
  };
  const out = []; const seenGroups = new Set(); let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select, [role="combobox"]')) {
    const type = el.getAttribute('role') === 'combobox' && el.tagName !== 'INPUT' ? 'combobox'
      : el.tagName === 'TEXTAREA' ? 'textarea' : el.tagName === 'SELECT' ? 'select' : (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type) || !visible(el)) continue;
    if (el.getAttribute('role') === 'combobox' && el.tagName === 'INPUT') { /* react-select style */ }
    if (type === 'radio' || type === 'checkbox') {
      const group = el.name || el.id;
      const members = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
      if (type === 'radio' || members.length > 1) {
        if (seenGroups.has(group)) continue; seenGroups.add(group);
        const id = `ja${n++}`;
        members.forEach((m, i) => { m.dataset.jaId = id; m.dataset.jaOpt = i; });
        const fs = el.closest('fieldset');
        const legend = fs && fs.querySelector('legend') ? clean(fs.querySelector('legend').innerText) : '';
        const box = el.closest('.field, .application-question, [class*="question"], div');
        const boxLabel = box && box.parentElement ? clean((box.parentElement.querySelector('label:not(:has(input)), .application-label, [class*="label"]') || {}).innerText) : '';
        out.push({ id, type, label: legend || boxLabel || el.name, required: members.some(m => m.required) || /\*/.test(legend + boxLabel),
                   options: members.map(m => labelFor(m)) });
        continue;
      }
    }
    const id = `ja${n++}`; el.dataset.jaId = id;
    const label = labelFor(el);
    out.push({ id, type, label, required: el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label),
               options: type === 'select' ? [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--)/i.test(t)) : [] });
  }
  return out;
}
"""

CAPTCHA_SELECTORS = ["iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", "iframe[src*='turnstile']",
                     "iframe[src*='challenges.cloudflare']", ".g-recaptcha", ".h-captcha", ".cf-turnstile",
                     "[data-sitekey]"]
SUCCESS = re.compile(r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?"
                     r"(received|submitted)|we('ve| have) received your application|successfully submitted", re.I)


def launch(playwright):
    exe = os.environ.get("CHROMIUM_PATH")
    return playwright.chromium.launch(**({"executable_path": exe} if exe else {}))


def open_form(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    if page.locator("input[type=file], textarea, input[type=email]").count() == 0:
        # Job page with an "Apply" button in front of the form
        btn = page.get_by_role("link", name=re.compile(r"^apply", re.I)).or_(
            page.get_by_role("button", name=re.compile(r"^apply", re.I)))
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(2000)


def extract_fields(page) -> list[Field]:
    return [Field(**f) for f in page.evaluate(EXTRACT_JS)]


def has_captcha(page) -> bool:
    return any(page.locator(sel).count() for sel in CAPTCHA_SELECTORS)


def fill(page, f: Field, value: str) -> None:
    loc = page.locator(f"[data-ja-id='{f.id}']")
    if f.type == "file":
        loc.first.set_input_files(value)
    elif f.type == "select":
        loc.first.select_option(label=value)
    elif f.type in ("radio", "checkbox") and f.options:
        if f.type == "checkbox" and len(f.options) == 1 and value.lower() in ("yes", "true", "on"):
            loc.first.check()
            return
        idx = f.options.index(value)
        page.locator(f"[data-ja-id='{f.id}'][data-ja-opt='{idx}']").check(force=True)
    elif f.type == "checkbox":
        if value.lower() in ("yes", "true", "on"):
            loc.first.check()
    elif f.type == "combobox":
        loc.first.click()
        loc.first.type(value, delay=20)
        page.keyboard.press("Enter")
    else:
        loc.first.fill(value)


def submit(page) -> bool:
    btn = page.locator("button[type=submit], input[type=submit]").or_(
        page.get_by_role("button", name=re.compile(r"submit", re.I)))
    btn.first.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:  # noqa: BLE001 - some forms never go idle; check the text anyway
        pass
    page.wait_for_timeout(1500)
    return bool(SUCCESS.search(page.inner_text("body")[:20000])) or bool(
        re.search(r"confirm|thank|success", page.url, re.I))
