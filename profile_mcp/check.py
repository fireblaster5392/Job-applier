"""Check your profile seed files: python -m profile_mcp.check [profile-dir]"""
import sys
from pathlib import Path

from .profile import PROFILE_DIR, Profile, check_seed


def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else PROFILE_DIR
    errors, warnings = check_seed(directory)
    for e in errors:
        print(f"ERROR    {e}")
    for w in warnings:
        print(f"warning  {w}")
    if errors:
        print(f"\n{len(errors)} error(s): fix them before running the pipeline.")
        return 1
    p = Profile.load(directory)
    print(f"OK: {p.basics.get('name')}, {len(p.data.get('experience', []))} roles, "
          f"{len(p.data.get('projects', []))} projects, {len(p.bullets)} bullets, "
          f"{sum(len(v) for v in p.data.get('skills', {}).values())} skills.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
