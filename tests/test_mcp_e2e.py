"""The whole workflow through the MCP protocol: a real client spawns the server over stdio,
the server drives headless Chromium against the fake Innoslate."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fake_innoslate import PASSWORD, USER

EXPECTED_TOOLS = {
    "innoslate_get_started", "innoslate_login", "innoslate_status", "innoslate_initialize", "innoslate_read_workspace",
    "innoslate_schema", "innoslate_find", "innoslate_get", "innoslate_document_tree", "innoslate_comments",
    "list_local_files", "read_local_file", "innoslate_lint_requirements", "innoslate_apply", "innoslate_update",
    "innoslate_attach_file", "innoslate_quality_check", "innoslate_report",
}


class Client:
    def __init__(self, s: ClientSession):
        self.s = s
        self.transcript: list[str] = []

    async def call(self, name: str, args: dict | None = None, expect_error: bool = False):
        r = await self.s.call_tool(name, args or {})
        text = "\n".join(getattr(c, "text", "") for c in r.content)
        self.transcript.append(text)
        err = getattr(r, "is_error", None)
        if err is None:
            err = getattr(r, "isError", False)
        if expect_error:
            assert err, f"{name} should have failed: {text[:300]}"
            return text
        assert not err, f"{name} failed: {text}"
        return json.loads(text)


async def test_full_workflow_over_stdio(fake, tmp_path, isolated_home):
    import docx

    isolated_home.mkdir(parents=True, exist_ok=True)
    env_file = isolated_home / ".env"
    env_file.write_text(f"INNOSLATE_USERNAME={USER}\nINNOSLATE_PASSWORD={PASSWORD}\n")
    os.chmod(env_file, 0o600)

    guides = tmp_path / "guides"
    guides.mkdir()
    d = docx.Document()
    d.add_paragraph("Stakeholder requirements go in SR.7 and are numbered SR.7.n. Trace each to UN.1 with traced from.")
    d.save(guides / "guide.docx")
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "notes.md").write_text("# Meeting\nThe supervisor wants alerts on a phone.\n")
    evidence = tmp_path / "visit.pdf"
    evidence.write_bytes(b"%PDF-1.4 fake evidence")

    errlog_path = tmp_path / "server-stderr.log"
    env = {**os.environ, "INNOSLATE_MCP_HOME": str(isolated_home), "INNOSLATE_HEADLESS": "1",
           "INNOSLATE_PROFILE_DIR": str(tmp_path / "profile"), "INNOSLATE_LOG_LEVEL": "INFO"}
    env.pop("INNOSLATE_USERNAME", None)
    env.pop("INNOSLATE_PASSWORD", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "innoslate_mcp"], env=env)
    with errlog_path.open("w") as errlog:
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                assert "innoslate_login" in (init.instructions or "")
                # wire names (camelCase), so the test runs on mcp 1.x and 2.x clients alike
                tools = {t.name: t.model_dump(by_alias=True) for t in (await s.list_tools()).tools}
                assert set(tools) == EXPECTED_TOOLS
                assert tools["innoslate_find"]["annotations"]["readOnlyHint"] is True
                assert tools["innoslate_apply"]["annotations"]["readOnlyHint"] is False
                assert tools["innoslate_apply"]["annotations"]["destructiveHint"] is False
                assert "password" not in json.dumps(tools["innoslate_login"]["inputSchema"]).lower()
                prompts = [p.name for p in (await s.list_prompts()).prompts]
                assert prompts == ["setup_innoslate_project"]

                c = Client(s)
                start = await c.call("innoslate_get_started")
                assert start["credentials_saved"] is True and start["credentials_file_warning"] is None
                msg = await c.call("innoslate_find", {"text": "x"}, expect_error=True)
                assert "innoslate_login" in msg                                    # the AI is told what to do

                login = await c.call("innoslate_login", {"project_url": fake.project_url, "wait_seconds": 30})
                assert login["logged_in"] and login["project_name"] == "Dashboard Mini Project"
                assert (await c.call("innoslate_status"))["logged_in"] is True

                init_res = await c.call("innoslate_initialize", {"guides_dir": str(guides)})
                assert init_res["entities"] == len(fake.store.E)
                assert {"SR.7.n", "UN.1"} <= {p["pattern"] for p in init_res["guide_patterns"]}
                m = await c.call("innoslate_read_workspace", {"name": "map", "max_chars": 2000})
                assert m["text"].startswith("# Innoslate project map: Dashboard Mini Project") and m["next_offset"]
                g = await c.call("innoslate_read_workspace", {"name": "guides/guide.txt"})
                assert "SR.7.n" in g["text"]
                await c.call("innoslate_read_workspace", {"name": "../../etc/passwd"}, expect_error=True)

                rels = await c.call("innoslate_schema", {"kind": "relations"})
                assert "traced from <-> traced to" in rels["relations"]
                props = await c.call("innoslate_schema", {"kind": "properties", "class_name": "Requirement"})
                assert {"name": "Rationale", "type": "BIG_TEXT", "writable": True} in props["properties"]

                f = await c.call("innoslate_find", {"number": "UN.1.1"})
                assert f["total"] == 2
                f = await c.call("innoslate_find", {"document": "2001", "label": "User Need"})
                assert [x["number"] for x in f["results"]] == ["UN.1.1", "UN.1.1.1", "UN.1.2", "UN.1.3"]
                amb = await c.call("innoslate_find", {"document": "UN.1"}, expect_error=True)
                assert "2 documents match" in amb and "2001" in amb
                f = await c.call("innoslate_find", {"document": "MP. User Needs Document", "number_prefix": "UN.1.1"})
                assert f["total"] == 2
                got = await c.call("innoslate_get", {"ref": "UN.1.1"})
                assert got["matches"] == 2
                one = await c.call("innoslate_get", {"ref": "2101"})
                assert one["entities"][0]["relations"]["sourced by"][0]["label"] == "UN.1 MP. User Needs Document"
                tree = await c.call("innoslate_document_tree", {"document": "2001"})
                assert tree["items"] == 4 and tree["tree"][1]["depth"] == 1
                com = await c.call("innoslate_comments", {"unresolved_only": True})
                assert com["total"] == 1 and com["comments"][0]["on"] == "UN.1 MP. User Needs Document"

                files = await c.call("list_local_files", {"folder": str(mine)})
                assert files["files"][0]["readable"]
                txt = await c.call("read_local_file", {"path": str(mine / "notes.md")})
                assert "alerts on a phone" in txt["text"]

                lint = await c.call("innoslate_lint_requirements", {"items": [
                    {"number": "SR.7.1", "name": "Alerts", "text": "The system shall list every open alert."},
                    {"number": "SR.7.2", "name": "Phone", "text": "The system should send alerts to a phone quickly."}]})
                assert lint["clean"] == ["SR.7.1"] and lint["errors"] >= 2

                plan = {"entities": [
                    {"key": "sr", "class": "Artifact", "number": "SR.7", "name": "MP. Stakeholder Requirements",
                     "labels": ["Requirements Document"], "description": "From UN.1."},
                    {"key": "r1", "class": "Requirement", "number": "SR.7.1", "name": "Alerts", "document": "sr",
                     "description": "The system shall list every open alert.", "labels": ["Functional Requirement"],
                     "attributes": {"Rationale": "UN.1.2"},
                     "relations": {"traced from": [{"number": "UN.1.2", "document": "2001"}]}},
                    {"key": "r2", "class": "Requirement", "number": "SR.7.2", "name": "Phone", "document": "sr",
                     "description": "The system should be fast.", "labels": ["Functional Requirement"]},
                ]}
                dry = await c.call("innoslate_apply", {"plan": plan})
                assert dry["dry_run"] and dry["summary"]["create"] == 3 and len(dry["confirm_token"]) == 16
                posts = len(fake.store.posts)
                no_tok = await c.call("innoslate_apply", {"plan": plan, "dry_run": False}, expect_error=True)
                assert "confirm_token" in no_tok
                changed = json.loads(json.dumps(plan))
                changed["entities"][1]["name"] = "Sneaky change"
                await c.call("innoslate_apply", {"plan": changed, "dry_run": False, "confirm_token": dry["confirm_token"]},
                             expect_error=True)
                assert len(fake.store.posts) == posts                                # nothing written by the refusals
                done = await c.call("innoslate_apply", {"plan": plan, "dry_run": False, "confirm_token": dry["confirm_token"]})
                assert done["verify"]["ok"] and set(done["created_ids"]) == {"sr", "r1", "r2"}
                sr_id = done["created_ids"]["sr"]

                bad_plan = {"entities": [{"key": "x", "class": "Requirement", "name": "x", "relations": {"traced from": ["UN.1.1"]}}]}
                err = await c.call("innoslate_apply", {"plan": bad_plan}, expect_error=True)
                assert "2 entities match" in err

                up = await c.call("innoslate_update", {"id": done["created_ids"]["r2"],
                                                       "fields": {"description": "The system shall send each alert to the supervisor's phone."}})
                up2 = await c.call("innoslate_update", {"id": done["created_ids"]["r2"], "dry_run": False,
                                                        "fields": {"description": "The system shall send each alert to the supervisor's phone."},
                                                        "confirm_token": up["confirm_token"]})
                assert up2["verify"]["ok"] and up2["summary"]["update"] == 1

                att = await c.call("innoslate_attach_file", {"id": 3001, "path": str(evidence)})
                assert att["replaces_existing_file"] is True and att["dry_run"]
                att2 = await c.call("innoslate_attach_file", {"id": 3001, "path": str(evidence), "dry_run": False,
                                                              "confirm_token": att["confirm_token"]})
                assert att2["verify"]["ok"]
                assert fake.store.files[att2["file_key"]]["data"] == evidence.read_bytes()
                await c.call("innoslate_attach_file", {"id": 2101, "path": str(evidence)}, expect_error=True)  # Statement has no File

                qc = await c.call("innoslate_quality_check", {"document": "SR.7", "timeout_seconds": 60})
                assert qc["scored"] == 2 and qc["average_score"] == 100.0

                rep = await c.call("innoslate_report", {"document": str(sr_id), "report_name": "Basic Document Output (DOCX)"})
                assert rep["ok"] and Path(rep["file"]).read_bytes().startswith(b"PK")
                assert Path(rep["file"]).parent == isolated_home / "acme-42" / "reports"

                audit = await c.call("innoslate_read_workspace", {"name": "audit"})
                events = [json.loads(line)["event"] for line in audit["text"].splitlines()]
                assert events == ["apply", "update", "attach_file", "quality_check"]

    # The password must not leak anywhere the AI or a log can see it.
    everything = "\n".join(c.transcript) + errlog_path.read_text()
    for f in isolated_home.rglob("*"):
        if f.is_file() and f.name != ".env" and "profile" not in f.parts:
            everything += f.read_text(errors="ignore")
    assert PASSWORD not in everything
    assert "Traceback" not in errlog_path.read_text()
