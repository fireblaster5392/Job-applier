"""Setup check: tells you exactly what's missing before you start the pipeline.

    python doctor.py            # offline checks
    python doctor.py --online   # also checks the Telegram token and Anthropic key work

Exit code 1 if anything required is missing.
"""
import argparse
import os
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
REQUIRED = {
    "ANTHROPIC_API_KEY": "console.anthropic.com -> API keys",
    "TELEGRAM_BOT_TOKEN": "message @BotFather in Telegram, send /newbot",
    "TELEGRAM_CHAT_ID": "send your bot a message, then: python -m watcher.telegram_setup",
}
OPTIONAL = {
    "SMTP_USER": "outreach email + inbox tracking (Gmail address)",
    "SMTP_PASSWORD": "Gmail app password (myaccount.google.com/apppasswords)",
    "BRAVE_API_KEY": "finding people to contact; company news in prep sheets",
    "APOLLO_API_KEY": "work emails for contacts",
    "APPLY_LOGIN_EMAIL": "lets the browser agent sign in to Workday-style portals",
}


def read_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return {**env, **{k: v for k, v in os.environ.items() if k in REQUIRED or k in OPTIONAL}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true")
    args = ap.parse_args()
    bad, warn, ok = [], [], []

    env_path = ROOT / ".env"
    if not env_path.exists():
        bad.append(".env is missing: cp .env.example .env, then fill it in")
    env = read_env(env_path)
    for key, how in REQUIRED.items():
        (ok if env.get(key) else bad).append(f"{key}" + ("" if env.get(key) else f" is empty ({how})"))
    for key, why in OPTIONAL.items():
        if not env.get(key):
            warn.append(f"{key} is empty (optional: {why})")

    sys.path.insert(0, str(ROOT))
    from profile_mcp.profile import Profile, check_seed
    errors, warnings = check_seed(ROOT / "profile")
    bad += [f"profile: {e}" for e in errors]
    warn += [f"profile: {w}" for w in warnings]
    if not errors:
        p = Profile.load(ROOT / "profile")
        if p.basics.get("name") == "Alex Placeholder":
            bad.append("profile: still the placeholder (Alex Placeholder); fill profile/ or upload your resume to Claude")
        else:
            ok.append(f"profile: {p.basics.get('name')}, {len(p.bullets)} bullets")

    answers = ROOT / "profile" / "answers.yaml"
    if not answers.exists():
        bad.append("profile/answers.yaml is missing: cp profile/answers.example.yaml profile/answers.yaml and fill it")
    else:
        a = yaml.safe_load(answers.read_text()) or {}
        legal = a.get("legal") or {}
        if not legal.get("authorized_to_work") or not legal.get("requires_sponsorship"):
            bad.append("answers.yaml: fill legal.authorized_to_work and legal.requires_sponsorship")
        else:
            ok.append(f"answers.yaml: legal answers for {', '.join(legal['authorized_to_work'])}")

    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    boards = sum(len(cfg.get("companies", {}).get(a, [])) for a in ("greenhouse", "lever", "ashby"))
    searches = len(cfg.get("discovery", {}).get("searches", [])) if cfg.get("discovery", {}).get("enabled", True) else 0
    if boards + searches == 0:
        bad.append("config.toml: no companies under [companies] and no [discovery] searches")
    else:
        ok.append(f"config.toml: {boards} company boards, {searches} searches, threshold "
                  f"{cfg.get('pipeline', {}).get('score_threshold', 70)}")
    if not cfg.get("apply", {}).get("dry_run", False):
        warn.append("config.toml: [apply] dry_run = false, so approved jobs really submit. "
                    "Use true for the first run.")

    if args.online and env.get("TELEGRAM_BOT_TOKEN"):
        import httpx
        try:
            r = httpx.get(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/getMe", timeout=15).json()
            (ok if r.get("ok") else bad).append("Telegram token " + (f"works (@{r['result']['username']})"
                                                                     if r.get("ok") else "is invalid"))
        except httpx.HTTPError as exc:
            warn.append(f"couldn't reach Telegram: {exc}")
    if args.online and env.get("ANTHROPIC_API_KEY"):
        import anthropic
        try:
            anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"]).models.list(limit=1)
            ok.append("Anthropic key works")
        except anthropic.AuthenticationError:
            bad.append("ANTHROPIC_API_KEY is invalid")
        except anthropic.APIError as exc:
            warn.append(f"couldn't check the Anthropic key: {exc}")

    for line in ok:
        print(f"  ok       {line}")
    for line in warn:
        print(f"  warning  {line}")
    for line in bad:
        print(f"  MISSING  {line}")
    print("\nReady to start." if not bad else f"\n{len(bad)} thing(s) to fix before starting.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
