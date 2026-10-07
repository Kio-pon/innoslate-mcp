# innoslate-mcp

An MCP server that lets an AI assistant (Claude Code, Antigravity, Cursor, Claude Desktop, or any other MCP client) read and fill an [Innoslate](https://cloud.innoslate.com) project for you.

You log in to Innoslate in a browser window the server opens. You give the assistant your documents (PDF, DOCX, XLSX, slides, meeting transcripts) and the instructor's guide. The assistant surveys the project first, works out where each piece belongs and how it should be numbered, shows you the plan, and writes it only after you say yes.

It works on the free tier. Innoslate refuses API keys there, so every call goes through your own logged-in browser session, the same way the Innoslate web app makes them.

## What it does

**Login.** It opens Chromium on your project and you log in normally, SSO included. If you saved a username and password with `innoslate-mcp setup`, it types them for you. The assistant never sees the password, and no tool accepts one as an argument.

**First-run survey.** `innoslate_initialize` reads every entity, the schema, review comments and members, then writes two files the assistant reads before it touches anything. `PROJECT_MAP.md` has the documents with their full trees, diagrams, numbering in use, who made what, reviewer comments and things that look broken. `NOMENCLATURE.md` has the numbering per document, the numbers your guide PDFs ask for, and where the two clash.

**Reading.** Search, one entity in full, a document as a tree, comments, the schema. Local files: PDF, DOCX (tables kept), XLSX, PPTX, CSV, JSON, Markdown, VTT/SRT transcripts.

**Writing.** `innoslate_apply` takes a plain JSON plan (documents, items, labels, attributes, trace links) and saves it in one request. The first call is always a dry run. The real write needs the token that dry run returned, so nothing is written without a dry run of the exact same plan. After saving, it reads every entity back and checks it.

**Requirement checks.** A local linter catches "should", vague words, two requirements in one sentence, unsourced numbers and duplicates. `innoslate_quality_check` runs Innoslate's own Quality Check and returns each score.

**Files and reports.** It uploads evidence files to an Artifact's File property and downloads reports such as "Basic Document Output (DOCX)" or "RTM Output (XLSX)".

It has no delete tool (delete in the Innoslate UI), it does not guess your project URL, and it does not write without a dry run.

## Install

You need Python 3.10 or newer. The commands are the same on Windows, macOS and Linux unless marked.

### 1. Install the package

With [pipx](https://pipx.pypa.io), which keeps it in its own environment:

```bash
pipx install git+https://github.com/Kio-pon/innoslate-mcp
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/Kio-pon/innoslate-mcp
```

Or with plain pip in a virtual environment:

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install git+https://github.com/Kio-pon/innoslate-mcp
```

### 2. Run setup once

```bash
innoslate-mcp setup
```

This downloads Chromium for Playwright (about 150 MB, once). It then asks whether to save your Innoslate login. Say no if you sign in with Google or Microsoft, because you will do that in the browser window. If you say yes, the password is typed hidden and stored in `~/.innoslate-mcp/.env`, readable only by you.

Check the install:

```bash
innoslate-mcp doctor
```

### 3. Add it to your AI client

If your client does not inherit your shell's PATH, it needs the full path of the `innoslate-mcp` command. Find it with `which innoslate-mcp` (macOS/Linux) or `where innoslate-mcp` (Windows).

Claude Code:

```bash
claude mcp add innoslate -- innoslate-mcp
```

Cursor uses `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project). In Antigravity, open the MCP servers settings and edit the raw config (`mcp_config.json`). In Claude Desktop, go to Settings, Developer, Edit Config (`claude_desktop_config.json`). All three take the same block:

```json
{
  "mcpServers": {
    "innoslate": {
      "command": "innoslate-mcp"
    }
  }
}
```

On Windows a pipx install usually puts the command at `C:\Users\<you>\.local\bin\innoslate-mcp.exe`. In JSON, write the backslashes twice: `"C:\\Users\\<you>\\.local\\bin\\innoslate-mcp.exe"`.

Restart the client after changing its config.

## First run

Open your project in Innoslate and copy the address bar. It looks like `https://cloud.innoslate.com/<org>/p/<projectId>/dashboard`. Then tell the assistant something like:

> Use the innoslate tools. My project is https://cloud.innoslate.com/myorg/p/1234/dashboard. The guides are in ~/Downloads/SEPM guides and my documents are in ~/Documents/D2. Log in, survey the project, read the guides and my documents, then tell me what goes where before writing anything.

Clients that support MCP prompts also have a `setup_innoslate_project` prompt that asks for the same three things.

The assistant then:

1. calls `innoslate_login`, and a browser window opens. Log in there.
2. calls `innoslate_initialize` with your guides folder, then reads `PROJECT_MAP.md` and `NOMENCLATURE.md`.
3. reads your documents and proposes a plan: which document, which numbers, which trace links.
4. runs `innoslate_apply` as a dry run and shows you the summary and warnings.
5. writes it after your yes, and reports the read-back check.

Keep the browser window open while you work. The Innoslate login is a session cookie, so closing the window logs you out and the assistant will ask you to log in again.

## Tools

| Tool | What it does |
|---|---|
| `innoslate_get_started` | The workflow the assistant follows, and the login state |
| `innoslate_login` | Opens the browser on your project and waits for the login |
| `innoslate_status` | Browser running, project, login still valid |
| `innoslate_initialize` | Surveys the project and guides, writes the map and nomenclature |
| `innoslate_read_workspace` | Reads the map, nomenclature, guide text or the write log |
| `innoslate_schema` | Classes, relations with inverses, labels, properties |
| `innoslate_find` | Search by text, class, label, number, number prefix or document |
| `innoslate_get` | One entity in full; a number returns every entity that uses it |
| `innoslate_document_tree` | Everything in a document, in order, with trace links |
| `innoslate_comments` | Review comments and the entity each is on |
| `list_local_files`, `read_local_file` | Your documents as text, in pages |
| `innoslate_lint_requirements` | Wording check for requirements, local and instant |
| `innoslate_apply` | Create or update entities and relations from a plan |
| `innoslate_update` | Change one entity by id |
| `innoslate_attach_file` | Upload a file into an entity's File property |
| `innoslate_quality_check` | Innoslate's Quality Check on a requirements document |
| `innoslate_report` | Download a document report (DOCX, XLSX) |

### The plan format

```json
{
  "entities": [
    {"key": "sr", "class": "Artifact", "number": "SR.1", "name": "Stakeholder Requirements",
     "labels": ["Requirements Document"], "description": "Derived from UN.1."},
    {"key": "r1", "class": "Requirement", "number": "SR.1.1", "name": "Alert list",
     "description": "The system shall list every open alert.",
     "labels": ["Functional Requirement"], "attributes": {"Rationale": "UN.1.2"},
     "document": "sr",
     "relations": {"traced from": [{"number": "UN.1.2", "document": "UN.1"}]}}
  ]
}
```

Class, label, relation and property names are looked up in your project's schema, and a typo comes back with the closest valid name. A reference is a plan `key`, an entity id, or a number together with the document it lives in. Innoslate lets numbers repeat, and a copied reference project often reuses `UN.1` and `SR.1`, so a bare number that more than one entity uses is refused with the list of matches. Existing items are matched by number inside their document only, and they change only with `"update_existing": true`. Every relation is written on both sides in the same save, which Innoslate requires. `examples/` has a longer plan.

## Where things are stored

Everything lives in `~/.innoslate-mcp/` (set `INNOSLATE_MCP_HOME` to move it):

```
.env                   your saved login, if you chose to save one
browser-profile/       the Chromium profile the server uses
<org>-<projectId>/     PROJECT_MAP.md, NOMENCLATURE.md, snapshot.json,
                       guides/*.txt, reports/, audit.log (one line per write)
```

None of it leaves your machine except the calls to your own Innoslate server.

## Settings

Set these in the environment or in `~/.innoslate-mcp/.env`:

| Variable | Default | Meaning |
|---|---|---|
| `INNOSLATE_USERNAME`, `INNOSLATE_PASSWORD` | unset | Typed into the login form for you |
| `INNOSLATE_CDP_URL` | unset | Attach to a Chromium you started with `--remote-debugging-port`, e.g. `http://localhost:9222`, instead of launching one |
| `INNOSLATE_HEADLESS` | `0` | `1` hides the browser. Only useful with saved credentials |
| `INNOSLATE_PROFILE_DIR` | `~/.innoslate-mcp/browser-profile` | Chromium profile folder |
| `INNOSLATE_MAX_UPLOAD_MB` | `25` | Largest file `innoslate_attach_file` sends |
| `INNOSLATE_LOG_LEVEL` | `WARNING` | Server log level, written to stderr |

## Limits

- The API routes and the report and Quality Check clicks were worked out on cloud.innoslate.com in October 2026. A self-hosted Innoslate on another version may differ. If Innoslate renames a menu entry, `innoslate_report` lists the reports it can see so you can pass the right name.
- Reports that ask for options in their dialog (a relation, a column set) are created with Innoslate's defaults.
- Diagrams are read as decomposition trees. The server does not draw diagram layouts. Create the diagram in the UI and let the assistant fill its entities and links.
- Scanned PDFs have no text layer. `read_local_file` says so, and you need an OCR'd copy.

## Troubleshooting

- `innoslate_login` times out: finish logging in in the window, then ask the assistant to call it again. With saved credentials, "typed but did not work" means the password is wrong or the site asked for more (2FA, SSO).
- "The login has expired": the browser window was closed or the session timed out. Call `innoslate_login` again.
- Chromium missing: run `innoslate-mcp setup`, or `python -m playwright install chromium` in the same environment.
- Linux without a desktop: save credentials and set `INNOSLATE_HEADLESS=1`.
- A write refused with "one-way relationship": open an issue with the plan. The server always sends both sides, so this is a bug.

## Development

```bash
git clone https://github.com/Kio-pon/innoslate-mcp
cd innoslate-mcp
pip install -e ".[test]"
python -m playwright install chromium
pytest
```

The tests never touch a real Innoslate project. `tests/fake_innoslate.py` is a local stand-in that copies the behaviour the server depends on: the session-cookie login, 401 without it, search paging, the one-way relation rejection, uploads, and the Reports and Quality Check UI. The suite drives real headless Chromium against it and runs the whole workflow through an MCP client over stdio. Start the fake by hand with `python tests/fake_innoslate.py 8765`.

## License

MIT
