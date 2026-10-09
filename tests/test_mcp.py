"""The job-search MCP server, called in-process through the official MCP client."""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest

from mcp import Client

from profile_mcp import server
from tests.helpers import store_with_job
from watcher.db import JobStore


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "jobs.db")
        store_with_job(self.db)
        os.environ["DB_PATH"] = self.db

    def tearDown(self):
        os.environ.pop("DB_PATH", None)
        self.tmp.cleanup()

    def call(self, name, args):
        async def go():
            async with Client(server.mcp) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                result = await client.call_tool(name, args)
                return tools, result
        return asyncio.run(go())

    def test_tools_listed_and_search(self):
        tools, result = self.call("search_experience", {"query": "python api", "k": 2})
        self.assertEqual(tools, {"search_experience", "get_profile", "render_resume", "list_jobs", "get_job"})
        self.assertFalse(result.is_error)
        self.assertIn("exco-api", result.content[0].text)

    def test_list_and_get_job(self):
        _, result = self.call("list_jobs", {"limit": 5})
        self.assertFalse(result.is_error)
        jobs = [json.loads(c.text) for c in result.content]
        self.assertEqual(len(jobs), 1)
        _, result = self.call("get_job", {"job_id": jobs[0]["id"]})
        self.assertEqual(json.loads(result.content[0].text)["status"], "discovered")


class MigrationTests(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE jobs (ats TEXT NOT NULL, company TEXT NOT NULL, job_id TEXT NOT NULL,"
                         " title TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
                         " PRIMARY KEY (ats, company, job_id))")
            conn.execute("INSERT INTO jobs VALUES ('lever','acme','1','Engineer','t','t')")
            conn.commit()
            conn.close()
            store = JobStore(path)
            row = store.conn.execute("SELECT status, score, matched FROM jobs").fetchone()
            self.assertEqual((row["status"], row["score"], row["matched"]), ("discovered", None, 0))


if __name__ == "__main__":
    unittest.main()
