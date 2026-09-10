# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The eval proctor: stage a document, hand a scenario's brief to a blind Claude Code executor that
holds only the Fusion MCP tools, save what it built into the eval set's cloud folder, and record the
run - transcript, report, screenshot, run.json and one line in results/index.md.

Run:  py -3 tests/live/evals/proctor.py S13_Surfaced-Bottle [--runs 2] [--model opus] [--set NAME]
      [--skill NAME] [--deny mcp__fusion-essentials__<tool>]

Blindness is a property of the launch: an empty scratch cwd, an empty config dir with the login
handed over as an access token by environment, an MCP config naming only the Fusion server, and an
allow list of the Fusion tools.
The proctor grades nothing; the saved document and the executor's report are what a person judges.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIVE = os.path.dirname(_HERE)
sys.path.insert(0, _LIVE)
import cloud_config  # noqa: E402
import tool_verify as harness  # noqa: E402  the sweep's wire: call(tool, args) and health_gate()

SCENARIOS = os.path.join(_HERE, "scenarios")
RESULTS = os.path.join(_HERE, "results")
PREAMBLE = os.path.join(_HERE, "preamble.md")
CONFIG_DIR = os.path.join(_HERE, ".claude-eval")
SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(_LIVE)), ".claude", "skills")
INDEX = os.path.join(RESULTS, "index.md")

MCP_PREFIX = "mcp__fusion-essentials__"
ALLOWED = MCP_PREFIX + "*"
# Denied by name beside the allow list: source access and the CLI's own agency tools, so a
# transcript holds Fusion calls only, plus the Fusion tools no eval may call (cloud deletes, the
# user-selection prompt, the raw script hatch that would paper over a typed-wire gap).
DENIED = ("Bash", "PowerShell", "Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit", "WebFetch",
          "WebSearch", "Task", "Agent", "TodoWrite", "Skill", "ScheduleWakeup", "Monitor",
          "SendMessage", "Workflow", "Artifact", "Cron*", "Task*",
          MCP_PREFIX + "data_delete_file", MCP_PREFIX + "data_delete_folder",
          MCP_PREFIX + "sys_request_selection", MCP_PREFIX + "sys_execute_script")
SKILL_HEADER = ("DESIGN PRACTICE (guidance, not the task - the task is above). Apply what is "
                "relevant while you build; do not recite it, and do not let it displace a single "
                "instruction in the task.")
MAX_TURNS = 300
INIT_DEADLINE_S = 90
HEARTBEAT_S = 60
# The gimbal foundation's planning pause after its first calls runs past six minutes and the run
# then completes; a shorter watch kills a run that was about to build.
STALL_S_DEFAULT = 600
ACTIVE_WAIT_S = 120


# --- the scenario --------------------------------------------------------------------------------

def scenario_path(name):
    """The scenario file for an id, a stem, or a path."""
    if os.path.isfile(name):
        return os.path.abspath(name)
    path = os.path.join(SCENARIOS, name if name.endswith(".md") else name + ".md")
    if os.path.isfile(path):
        return path
    sys.exit(f"no scenario {name!r} - the ids are the file stems under {SCENARIOS}")


def _section(text, title):
    """The body of one '## <title>' section, or ''."""
    m = re.search(r"^## " + re.escape(title) + r"[ \t]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""


def read_scenario(path):
    """(scenario id, fixture scenario id or None, the '## Prompt' body) from one scenario file."""
    text = open(path, encoding="utf-8").read()
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    meta = dict(re.findall(r"^(\w+):[ \t]*(.*?)[ \t]*$", fm.group(1), re.M)) if fm else {}
    prompt = _section(text, "Prompt")
    if not prompt:
        sys.exit(f"{path}: no '## Prompt' section - the brief the executor receives")
    stem = os.path.splitext(os.path.basename(path))[0]
    fixture = meta.get("fixture", "none")
    return meta.get("id") or stem.split(".")[0], (None if fixture == "none" else fixture), prompt


def skill_body(name):
    """A skill's instructions - its SKILL.md below the frontmatter."""
    path = os.path.join(SKILLS_DIR, name, "SKILL.md")
    if not os.path.exists(path):
        sys.exit(f"--skill {name}: {path} does not exist")
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n.*?\n---\n+", text, re.S)
    return (text[m.end():] if m else text).strip()


REPORT_HEADER = ("REPORT FROM THE PREVIOUS STAGE - the agent that built the document you start "
                 "from wrote this about it. Read it as a plan already made; check it against the "
                 "document with fresh reads rather than deriving it again.")


def build_prompt(body, project, folder, skill=None, preamble_path=PREAMBLE, prior_report=""):
    """The bytes the executor receives: the shared preamble, the brief with its tokens filled, the
    previous stage's report when the brief starts from its document, and the named skill's body."""
    preamble = open(preamble_path, encoding="utf-8").read().strip()
    task = body.replace("{{PROJECT}}", project).replace("{{FOLDER}}", folder)
    parts = [preamble, task]
    if prior_report.strip():
        parts.append(REPORT_HEADER + "\n\n" + prior_report.strip())
    if skill:
        parts.append(SKILL_HEADER + "\n\n" + skill_body(skill))
    return "\n\n".join(parts) + "\n"


def prior_report(set_dir, fixture):
    """The report.txt of the newest local run of the fixture scenario in this set, or ''."""
    if not fixture or not os.path.isdir(set_dir):
        return ""
    runs = sorted(d for d in os.listdir(set_dir) if d.startswith(fixture + "_"))
    for name in reversed(runs):
        path = os.path.join(set_dir, name, "report.txt")
        if os.path.isfile(path):
            return open(path, encoding="utf-8", errors="replace").read()
    return ""


# --- the cloud: hub, project, set folder -----------------------------------------------------------

def _lower(name):
    return (name or "").strip().lower()


def ensure_hub(hub, call):
    """Make the configured hub active, or exit naming the hubs this account can see."""
    err, out = call("data_switch_hub", {"action": "list"})
    if err:
        sys.exit(f"the hubs would not list: {out}")
    if _lower((out.get("active_hub") or {}).get("name")) == _lower(hub):
        return
    names = [h.get("name") for h in out.get("hubs") or [] if h.get("name")]
    if _lower(hub) not in [_lower(n) for n in names]:
        sys.exit(f"hub {hub!r} is not one this account can see: {', '.join(names) or '(none)'}")
    err, out = call("data_switch_hub", {"action": "switch", "hub": hub})
    if err or not out.get("switched"):
        sys.exit(f"switching to hub {hub!r} did not take: {out}")
    print(f"  switched to hub {hub}", flush=True)


def ensure_project(project, hub, call):
    """The project's name on the active hub, created and announced when it is not there."""
    err, out = call("data_get", {})
    if err:
        sys.exit(f"the projects would not list: {out}")
    for row in out.get("projects") or []:
        if _lower(row.get("name")) == _lower(project):
            return row.get("name")
    err, made = call("data_create_project", {"name": project})
    if err or not made.get("created"):
        sys.exit(f"project {project!r} is not on hub {hub!r} and could not be created: {made}")
    print(f"  created project {project!r} on hub {hub!r} because it was not found", flush=True)
    return made.get("name") or project


def ensure_folder(project, parent, name, call):
    """The set folder's path under the configured folder, created when missing."""
    err, out = call("data_create_folder",
                    {"folder_name": name, "project": project, "parent_folder": parent})
    if err and "already exists" not in str(out):
        sys.exit(f"folder {name!r} could not be created under {parent!r}: {out}")
    return f"{parent}/{name}" if parent else name


# --- staging ---------------------------------------------------------------------------------------

def stage_empty(call):
    """A new document, read back empty (zero bodies, zero occurrences); its name."""
    err, out = call("doc_new", {})
    if err:
        sys.exit(f"doc_new failed, so nothing was staged: {out}")
    err, out = call("workspace_orient", {})
    if err:
        sys.exit(f"the new document would not read back, so it cannot be called empty: {out}")
    design = out.get("design") or {}
    bodies, occurrences = design.get("bodies"), design.get("total_occurrences")
    name = (out.get("document") or {}).get("name")
    if bodies != 0 or occurrences != 0:
        sys.exit(f"refusing to run: the new document {name!r} reads bodies={bodies}, "
                 f"occurrences={occurrences}, not an empty design")
    return name


def newest_fixture(files, fixture):
    """The file named <fixture>_<nn> with the highest nn, or None."""
    rows = [f for f in files
            if (f.get("name") or "").startswith(fixture + "_") and f.get("id")]
    return max(rows, key=lambda f: f.get("name") or "") if rows else None


def stage_fixture(fixture, project, folder, call, wait_s=ACTIVE_WAIT_S, sleep=time.sleep):
    """Open the newest saved output of the fixture scenario in the set folder; its name."""
    err, out = call("data_get", {"project": project, "folder": folder})
    if err:
        sys.exit(f"the set folder {folder!r} would not list, so no {fixture} output was found: {out}")
    row = newest_fixture(out.get("files") or [], fixture)
    if row is None:
        sys.exit(f"no document named {fixture}_<nn> in {project}/{folder} - run {fixture} into "
                 "this set first")
    err, out = call("doc_open", {"file_id": row["id"], "force_api_open": True})
    if err:
        sys.exit(f"doc_open of {row['name']} failed: {out}")
    deadline = time.time() + wait_s
    while True:
        err, seen = call("workspace_orient", {})
        if not err and ((seen.get("document") or {}).get("name") or "").startswith(row["name"]):
            return row["name"]
        if time.time() >= deadline:
            sys.exit(f"{row['name']} did not become the active document within {wait_s} s")
        sleep(3)


# --- the executor ---------------------------------------------------------------------------------

def stall_limit_s():
    """Seconds without a tool call before the executor is killed (EVAL_STALL_S; 0 disables)."""
    try:
        return max(0, int(os.environ.get("EVAL_STALL_S") or STALL_S_DEFAULT))
    except ValueError:
        return STALL_S_DEFAULT


class Liveness:
    """Tool calls seen in the transcript and when the count last moved. Thinking is not progress:
    an executor thinking its way to the response cap emits thinking events the whole way down, and
    the CLI's MAX_THINKING_TOKENS does not bound a turn (a stalled turn read 42k under a 16k cap)."""

    def __init__(self, now):
        self.calls = 0
        self.last_call = self.start = now
        self.call_times = []

    def read(self, line, now):
        n = line.count('"type":"tool_use"')
        if n:
            self.calls += n
            self.last_call = now
            self.call_times.extend([int(now - self.start)] * n)

    def idle_s(self, now):
        return now - self.last_call


def kill_tree(proc):
    """Kill the executor and its children: the CLI runs the model turn in a node child that
    outlives a kill on the CLI's own pid and keeps the MCP connection open."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            return
        except OSError:
            pass
    proc.kill()


CREDENTIALS = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
TOKEN_MIN_S = 1800


def oauth_token(path=CREDENTIALS, now=None):
    """The CLI's current access token, handed to the executor by environment. A copied credentials
    file dies when the main session rotates its refresh token mid-run; the access token stays valid
    to its own expiry, so a run needs one with time left."""
    try:
        with open(path, encoding="utf-8") as fh:
            oauth = json.load(fh).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        sys.exit(f"no CLI login found at {path} - run 'claude login' once (Windows/Linux keep "
                 "the login there), or set CLAUDE_CODE_OAUTH_TOKEN from 'claude setup-token' or "
                 "ANTHROPIC_API_KEY (any platform), then rerun")
    token, expires_ms = oauth.get("accessToken"), oauth.get("expiresAt")
    if not token or not expires_ms:
        sys.exit(f"{path} carries no access token - run 'claude login' once, or set "
                 "CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY, then rerun")
    left = expires_ms / 1000.0 - (time.time() if now is None else now)
    if left < TOKEN_MIN_S:
        sys.exit(f"the CLI's access token expires in {int(left / 60)} min, too little for a run - "
                 "use the CLI once so it refreshes, then rerun")
    return token


def executor_login(env, creds=CREDENTIALS):
    """The environment additions that sign the executor in: an API key or OAuth token already in
    the environment passes through untouched; otherwise the CLI's own access token is read."""
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return {}
    return {"CLAUDE_CODE_OAUTH_TOKEN": oauth_token(creds)}


def launch(prompt, run_dir, model, deny=(), tool_search=False):
    """Spawn the blind executor on the prompt, watch it, and return (why it ended, the seconds at
    which each tool call landed)."""
    exe = shutil.which("claude")
    if not exe:
        sys.exit("claude CLI not on PATH - install Claude Code and log in, then rerun")
    allowed, disallowed = ALLOWED, ",".join(DENIED + tuple(deny))
    login = executor_login(os.environ)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    cwd = os.path.join(run_dir, "scratch")
    os.makedirs(cwd, exist_ok=True)
    mcp_config = os.path.join(run_dir, "mcp.json")
    with open(mcp_config, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {"fusion-essentials": {"type": "http", "url": harness.MCP}}}, fh)
    cmd = [exe, "-p", "--model", model, "--mcp-config", mcp_config, "--strict-mcp-config",
           "--allowedTools", allowed, "--disallowedTools", disallowed,
           "--output-format", "stream-json", "--verbose", "--max-turns", str(MAX_TURNS)]
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = CONFIG_DIR
    env.update(login)
    # Deferred tool loading (true) sends a few schemas per turn instead of all 192, and has spawned
    # executors with no Fusion tools at all; the init check below ends such a run at once.
    env["ENABLE_TOOL_SEARCH"] = "true" if tool_search else "false"
    env.setdefault("MCP_TIMEOUT", "60000")
    transcript = os.path.join(run_dir, "transcript.jsonl")
    stderr_chunks = []
    out = open(transcript, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", cwd=cwd, env=env)
    threading.Thread(target=lambda: stderr_chunks.extend(proc.stderr), daemon=True).start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass
    start = last_beat = time.time()
    live = Liveness(start)
    limit = stall_limit_s()
    ended = ""
    init_seen = False
    try:
        with open(transcript, "r", encoding="utf-8", errors="replace") as rf:
            while proc.poll() is None and not ended:
                time.sleep(2)
                while True:
                    pos = rf.tell()
                    line = rf.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        rf.seek(pos)
                        break
                    if not init_seen and line.strip():
                        init_seen = True
                        try:
                            init = json.loads(line)
                        except ValueError:
                            init = {}
                        if init.get("type") == "system" and not init.get("tools"):
                            ended = "spawned with no tools"
                        else:
                            print(f"  executor up ({len(init.get('tools') or [])} tools)", flush=True)
                    live.read(line, time.time())
                now = time.time()
                if not init_seen and now - start > INIT_DEADLINE_S:
                    ended = f"no init event within {INIT_DEADLINE_S} s"
                if limit and live.idle_s(now) >= limit:
                    ended = f"stalled: no tool call for {int(live.idle_s(now) / 60)} min at {live.calls} calls"
                if now - last_beat >= HEARTBEAT_S:
                    print(f"  [{int(now - start)}s] {live.calls} tool calls, idle {int(live.idle_s(now))}s",
                          flush=True)
                    last_beat = now
    finally:
        kill_tree(proc)
        proc.wait()
        out.close()
    with open(os.path.join(run_dir, "stderr.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(stderr_chunks))
    if not ended:
        ended = "report" if proc.returncode == 0 else f"executor exited {proc.returncode}"
    return ended, live.call_times


def audit(transcript):
    """(Fusion tool calls, output tokens, the executor's final message) read off the transcript."""
    calls, tokens, final = 0, 0, ""
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and str(block.get("name") or "").startswith(MCP_PREFIX):
                        calls += 1
            elif ev.get("type") == "result":
                final = ev.get("result") or ""
                tokens = int((ev.get("usage") or {}).get("output_tokens") or 0)
    return calls, tokens, final


# --- the record ------------------------------------------------------------------------------------

URL_TRIES = 4


def save_result(name, project, folder, run_dir, call, sleep=time.sleep):
    """Save the active document as <name> into the set folder and grab an iso screenshot; what
    landed, with any refusal quoted rather than hidden."""
    saved = {"name": name, "document_id": None, "web_url": None, "error": None, "screenshot": None}
    err, out = call("doc_save_as", {"name": name, "project": project, "folder": folder,
                                    "create_path": True})
    if err:
        saved["error"] = str(out)[:400]
    else:
        saved["document_id"] = out.get("document_id")
        # The web URL and URN settle a few seconds behind the save.
        for attempt in range(URL_TRIES):
            err, doc = call("doc_get", {})
            active = (doc.get("active") or {}) if not err else {}
            saved["web_url"] = active.get("fusion_web_url")
            saved["document_id"] = saved["document_id"] or active.get("document_id")
            if saved["web_url"] or attempt == URL_TRIES - 1:
                break
            sleep(5)
    err, out = call("view_screenshot", {"view": "iso-top-right",
                                        "file_path": os.path.join(run_dir, "iso.png")})
    saved["screenshot"] = "iso.png" if not err else f"screenshot failed: {str(out)[:200]}"
    return saved


INDEX_HEADER = ("| date | set | scenario | model | harness | denied | calls | out tokens | ended | "
                "document |\n|---|---|---|---|---|---|---|---|---|---|\n")


def harness_label(rec):
    """The run's harness switches in one cell: skill, tool search, preamble; '-' at the defaults."""
    parts = []
    if rec.get("skill"):
        parts.append("skill:" + rec["skill"])
    if rec.get("tool_search"):
        parts.append("tool-search:on")
    if rec.get("preamble") and rec["preamble"] != "preamble":
        parts.append("preamble:" + rec["preamble"])
    if rec.get("label"):
        parts.append("label:" + rec["label"])
    return " ".join(parts) or "-"


def index_row(rec):
    """One index line for a run record."""
    saved = rec["saved"]
    link = saved.get("web_url") or saved.get("document_id") or saved.get("error") or "(not saved)"
    return (f"| {rec['started']} | {rec['set']} | {rec['variant']} | {rec['model']} | "
            f"{harness_label(rec)} | {', '.join(rec['denied']) or '-'} | {rec['calls']} | "
            f"{rec['output_tokens']} | {rec['ended']} | {link} |\n")


def next_run_dir(set_dir, scenario_id):
    """results/<set>/<scenario>_<nn>, the first nn not on disk."""
    n = 1
    while os.path.exists(os.path.join(set_dir, f"{scenario_id}_{n:02d}")):
        n += 1
    return os.path.join(set_dir, f"{scenario_id}_{n:02d}"), f"{n:02d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scenario", help="a scenario id (S13_Surfaced-Bottle), a variant stem "
                                     "(S13_Surfaced-Bottle.B) or a path")
    ap.add_argument("--runs", type=int, default=1, help="repeat the run this many times")
    ap.add_argument("--model", default="opus", help="the executor's model (a CLI alias or id)")
    ap.add_argument("--set", dest="set_name", default=None,
                    help="the eval set sharing one cloud folder (default: the scenario id); a "
                         "chain names one set so each stage finds the last stage's document")
    ap.add_argument("--skill", default=None, help="append this .claude/skills/<name> body")
    ap.add_argument("--tool-search", action="store_true",
                    help="let the CLI defer tool schemas (a few per turn instead of all of them)")
    ap.add_argument("--preamble", default=PREAMBLE,
                    help="the harness-rules file prepended to the brief (default preamble.md)")
    ap.add_argument("--label", default="",
                    help="a word for the index naming a server-side condition this run was "
                         "measured under (a wire mode the proctor cannot set itself)")
    ap.add_argument("--deny", metavar="TOOL", action="append", default=[],
                    help="deny the executor one Fusion tool (repeatable, the full "
                         "mcp__fusion-essentials__<name>)")
    args = ap.parse_args()

    cfg, problem = cloud_config.load_config()
    if problem:
        sys.exit(problem)
    path = scenario_path(args.scenario)
    scenario_id, fixture, body = read_scenario(path)
    variant = os.path.splitext(os.path.basename(path))[0]
    set_name = args.set_name or scenario_id
    set_folder = set_name if set_name.startswith("Eval-") else \
        "Eval-" + time.strftime("%Y%m%d") + "-" + set_name
    try:
        harness.health_gate()
    except OSError:
        sys.exit(f"MCP server not reachable at {harness.BASE} - is Fusion up with the add-in loaded?")
    call = harness.call
    ensure_hub(cfg["hub"], call)
    project = ensure_project(cfg["project"], cfg["hub"], call)
    folder = ensure_folder(project, cfg["folder"], set_folder, call)
    set_dir = os.path.join(RESULTS, set_folder)
    os.makedirs(set_dir, exist_ok=True)

    for _ in range(max(1, args.runs)):
        run_dir, nn = next_run_dir(set_dir, scenario_id)
        os.makedirs(run_dir)
        started = time.strftime("%Y-%m-%d %H:%M")
        preamble_name = os.path.splitext(os.path.basename(args.preamble))[0]
        rec = {"scenario": scenario_id, "variant": variant, "set": set_folder, "model": args.model,
               "skill": args.skill, "tool_search": bool(args.tool_search),
               "preamble": preamble_name, "label": args.label, "denied": list(args.deny),
               "fixture": fixture}
        print(f"run: {run_dir}\n  cloud folder: {project}/{folder}  model: {args.model}  "
              f"harness: {harness_label(rec)}  denied: {', '.join(args.deny) or '-'}", flush=True)
        staged = stage_empty(call) if fixture is None else stage_fixture(fixture, project, folder, call)
        print(f"  staged: {staged}", flush=True)
        prompt = build_prompt(body, project, folder, args.skill, preamble_path=args.preamble,
                              prior_report=prior_report(set_dir, fixture))
        with open(os.path.join(run_dir, "prompt.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(prompt)
        t0 = time.time()
        ended, call_times = launch(prompt, run_dir, args.model, args.deny, args.tool_search)
        calls, tokens, final = audit(os.path.join(run_dir, "transcript.jsonl"))
        with open(os.path.join(run_dir, "report.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(final)
        saved = save_result(f"{scenario_id}_{nn}", project, folder, run_dir, call)
        rec.update({"staged": staged, "started": started, "seconds": int(time.time() - t0),
                    "calls": calls, "output_tokens": tokens, "ended": ended,
                    "call_times_s": call_times, "saved": saved})
        with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1)
        os.makedirs(RESULTS, exist_ok=True)
        new_index = not os.path.exists(INDEX)
        with open(INDEX, "a", encoding="utf-8", newline="\n") as fh:
            if new_index:
                fh.write(INDEX_HEADER)
            fh.write(index_row(rec))
        print(f"  ended: {ended}  calls: {calls}  output tokens: {tokens}  "
              f"seconds: {rec['seconds']}\n  saved: {saved['web_url'] or saved['error']}\n"
              f"== report ==\n{final or '(no final message)'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
