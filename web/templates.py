from jinja2 import DictLoader, Environment

BASE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Job pipeline{% endblock %}</title>
<style>
  :root { --bg:#fafafa; --fg:#18181b; --muted:#71717a; --card:#fff; --line:#e4e4e7; --accent:#2563eb;
          --ok:#16a34a; --bad:#dc2626; --warn:#d97706; }
  @media (prefers-color-scheme: dark) { :root { --bg:#09090b; --fg:#f4f4f5; --muted:#a1a1aa; --card:#18181b;
          --line:#27272a; --accent:#60a5fa; --ok:#4ade80; --bad:#f87171; --warn:#fbbf24; } }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  main { max-width: 1000px; margin: 0 auto; padding: 16px; }
  a { color: var(--accent); text-decoration: none; }
  header { display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; margin-bottom: 12px; }
  h1 { font-size: 20px; margin: 0; }
  .muted { color: var(--muted); }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin: 8px 0 16px; }
  .chip { border:1px solid var(--line); background:var(--card); border-radius:999px; padding:4px 12px; color:var(--fg); }
  .chip.on { border-color: var(--accent); }
  .chip b { margin-left: 4px; }
  table { width:100%; border-collapse: collapse; background: var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align: top; }
  th { font-size: 12px; text-transform: uppercase; color: var(--muted); }
  .status { font-size: 12px; padding: 2px 8px; border-radius: 999px; border:1px solid var(--line); white-space: nowrap; }
  .s-approved { color: var(--ok); } .s-rejected, .s-failed { color: var(--bad); } .s-tailoring { color: var(--warn); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px; margin-bottom:16px; }
  .actions { display:flex; gap:8px; flex-wrap:wrap; }
  button { font: inherit; padding: 6px 14px; border-radius: 6px; border:1px solid var(--line); background: var(--card); color: var(--fg); cursor: pointer; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .score { font-size: 28px; font-weight: 700; }
  iframe { width:100%; height: 900px; border:1px solid var(--line); border-radius: 8px; background:#fff; }
  pre { white-space: pre-wrap; font: 13px/1.5 ui-monospace, monospace; max-height: 400px; overflow:auto; }
  @media (max-width: 640px) { .hide-sm { display:none; } iframe { height: 600px; } }
</style>
{% block head %}{% endblock %}
</head><body><main>
<header><h1><a href="/" style="color:inherit">Job pipeline</a></h1><span class="muted">{{ subtitle or "" }}</span></header>
{% block body %}{% endblock %}
</main></body></html>"""

INDEX = """{% extends "base" %}
{% block body %}
<div class="chips">
  <a class="chip {{ 'on' if not status }}" href="/">All matches<b>{{ total }}</b></a>
  {% for s in statuses %}
  <a class="chip {{ 'on' if status == s }}" href="/?status={{ s }}">{{ s }}<b>{{ counts.get(s, 0) }}</b></a>
  {% endfor %}
</div>
{% if jobs %}
<table>
<tr><th>Job</th><th class="hide-sm">Location</th><th class="hide-sm">Found</th><th>Status</th></tr>
{% for j in jobs %}
<tr>
  <td><a href="/jobs/{{ j.rowid }}">{{ j.title }}</a><br><span class="muted">{{ j.company }} · {{ j.ats }}</span></td>
  <td class="hide-sm">{{ j.location }}</td>
  <td class="hide-sm muted">{{ j.first_seen[:16].replace("T", " ") }}</td>
  <td><span class="status s-{{ j.status }}">{{ j.status }}</span></td>
</tr>
{% endfor %}
</table>
{% else %}
<div class="card muted">No matching jobs yet. New matches appear here as the watcher finds them.</div>
{% endif %}
{% endblock %}"""

JOB = """{% extends "base" %}
{% block title %}{{ job.title }} · {{ job.company }}{% endblock %}
{% block head %}{% if job.status == "tailoring" %}<meta http-equiv="refresh" content="5">{% endif %}{% endblock %}
{% block body %}
<div class="card">
  <h2 style="margin:0">{{ job.title }}</h2>
  <div class="muted">{{ job.company }} · {{ job.location }} · {{ job.ats }}</div>
  <p><span class="status s-{{ job.status }}">{{ job.status }}</span>
     {% if job.last_error %}<span style="color:var(--bad)">{{ job.last_error }}</span>{% endif %}</p>
  <div class="actions">
    <a href="{{ job.url }}" target="_blank"><button>Open posting</button></a>
    {% if job.status != "tailoring" %}
    <form method="post" action="/jobs/{{ job.rowid }}/tailor"><button {{ 'class=primary' if not t }}>{{ "Re-tailor" if t else "Tailor resume" }}</button></form>
    {% endif %}
    {% if t %}
    <form method="post" action="/jobs/{{ job.rowid }}/status"><input type="hidden" name="status" value="approved"><button class="primary">Approve</button></form>
    <form method="post" action="/jobs/{{ job.rowid }}/status"><input type="hidden" name="status" value="rejected"><button>Reject</button></form>
    {% endif %}
    <form method="post" action="/jobs/{{ job.rowid }}/status"><input type="hidden" name="status" value="skipped"><button>Skip</button></form>
  </div>
  {% if job.status == "approved" %}<p class="muted">Approved. Auto-apply isn't built yet: apply at <a href="{{ job.apply_url or job.url }}" target="_blank">the posting</a> with the files below.</p>{% endif %}
</div>
{% if job.status == "tailoring" %}<div class="card muted">Claude is tailoring this application. This page refreshes on its own.</div>{% endif %}
{% if t %}
<div class="card">
  <div class="score">{{ t.data.fit_score }}/100</div>
  <ul>{% for r in t.data.fit_reasons %}<li>{{ r }}</li>{% endfor %}</ul>
  {% if t.data.gaps %}<p><b>Gaps:</b> {{ t.data.gaps | join("; ") }}</p>{% endif %}
  {% if t.warnings %}<p style="color:var(--warn)"><b>Guardrail fixes:</b></p><ul>{% for w in t.warnings %}<li>{{ w }}</li>{% endfor %}</ul>{% endif %}
  <p class="muted">{{ t.model }} · {{ t.usage.input_tokens }} in / {{ t.usage.output_tokens }} out tokens · {{ t.created_at[:16].replace("T", " ") }}</p>
</div>
<div class="card"><h3 style="margin-top:0">Resume <a href="/jobs/{{ job.rowid }}/resume" target="_blank" style="font-size:14px">open to print</a></h3>
<iframe src="/jobs/{{ job.rowid }}/resume" title="Tailored resume"></iframe></div>
<div class="card"><h3 style="margin-top:0">Cover letter <a href="/jobs/{{ job.rowid }}/cover-letter" target="_blank" style="font-size:14px">open to print</a></h3>
<iframe src="/jobs/{{ job.rowid }}/cover-letter" title="Cover letter" style="height:700px"></iframe></div>
{% endif %}
{% if t and t.description %}<div class="card"><h3 style="margin-top:0">Job description</h3><pre>{{ t.description }}</pre></div>{% endif %}
{% endblock %}"""

env = Environment(loader=DictLoader({"base": BASE, "index": INDEX, "job": JOB}), autoescape=True)
