from dataclasses import dataclass, field


@dataclass
class Job:
    ats: str              # greenhouse | lever | ashby
    company: str          # board slug, e.g. "stripe"
    job_id: str           # the ATS's own id, unique within (ats, company)
    title: str
    location: str
    url: str
    apply_url: str = ""
    department: str = ""
    remote: bool = False
    posted_at: str = ""   # ISO-8601 UTC when the ATS gives one
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ats, self.company, self.job_id)
