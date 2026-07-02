"""The tools a raw API model gets — filesystem + shell + web + skill loading.

Every tool has the signature `fn(args: dict, ctx: Context) -> dict` and returns
`{"output": str, "metadata": dict}` (opencode's ExecuteResult shape, minus the
Effect machinery). `output` is what gets fed back to the model; `metadata` is
for our logs.

Access posture:
  - Structured file tools (read/write/edit/list/glob/grep) resolve paths through
    `safety.clamp_path` and CANNOT leave the workspace — UNLESS the Context sets
    `allow_outside_workspace=True` (the runner's --full-access flag), which makes
    them resolve anywhere, matching M2's CLI providers.
  - `bash_run` / `powershell_run` run REAL shells. They start in the workspace
    but a command can `cd` anywhere — they are full-reach by nature and are NOT
    clamped. "Sandbox" only ever meant the structured file tools.

Network tools (web_search, webfetch) send a browser User-Agent, not the default
Python-urllib one — Cloudflare 403s the latter (the same 1010 that masked
Cerebras as dead).
"""
from __future__ import annotations

import fnmatch
import html
import platform
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from harness.safety import clamp_path, PathEscape

# NOTE: `_kill_tree` is imported lazily inside `_exec` (not at module top) on
# purpose. Importing providers.base here would trigger the whole providers
# package __init__, which now imports the routing adapters that import back into
# harness.tools — a circular import. Lazy import keeps harness.tools standalone.

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass
class Context:
    workspace_root: Path
    skills_root: Path | None = None
    allow_outside_workspace: bool = False  # --full-access: drop the file-tool clamp


def _ok(output: str, **meta) -> dict:
    return {"output": output, "metadata": meta}


def _err(msg: str) -> dict:
    # Errors go back to the model as plain text so it can recover, not as
    # exceptions that would kill the loop.
    return {"output": f"ERROR: {msg}", "metadata": {"error": True}}


def _get_int(args: dict, key: str, default: int, minimum: int) -> int | None:
    """Parse an int tool arg. Model args are untrusted — a bad or out-of-range
    value returns None (the caller turns that into an _err), never an exception."""
    v = args.get(key, default)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= minimum else None


def _safe(ctx: Context, path: str) -> Path:
    """Resolve a model path. Clamped to the workspace unless full access is on,
    in which case relative paths still anchor to the workspace but escapes and
    absolute paths are allowed."""
    if ctx.allow_outside_workspace:
        p = Path(path)
        return p if p.is_absolute() else (ctx.workspace_root / p)
    return clamp_path(ctx.workspace_root, path)


# --- filesystem -------------------------------------------------------------

def read_file(args: dict, ctx: Context) -> dict:
    """Read a text file, optionally a line window (offset is 1-based)."""
    try:
        p = _safe(ctx, args["path"])
    except PathEscape as e:
        return _err(str(e))
    if not p.is_file():
        return _err(f"not a file: {args['path']}")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    offset = _get_int(args, "offset", 1, 1)   # 1-based
    limit = _get_int(args, "limit", 2000, 1)
    if offset is None or limit is None:
        return _err("offset and limit must be integers >= 1")
    window = lines[offset - 1 : offset - 1 + limit]
    return _ok("\n".join(window), lines=len(lines), returned=len(window))


def write_file(args: dict, ctx: Context) -> dict:
    """Create or overwrite a file (parent dirs auto-created)."""
    try:
        p = _safe(ctx, args["path"])
    except PathEscape as e:
        return _err(str(e))
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content") or ""  # tolerate {"content": null}
    if not isinstance(content, str):
        content = str(content)
    p.write_text(content, encoding="utf-8")
    nbytes = len(content.encode("utf-8"))  # real byte count, not char count
    return _ok(f"wrote {nbytes} bytes to {args['path']}", bytes=nbytes)


def edit_file(args: dict, ctx: Context) -> dict:
    """Replace an exact substring. `old` must appear exactly once."""
    try:
        p = _safe(ctx, args["path"])
    except PathEscape as e:
        return _err(str(e))
    if not p.is_file():
        return _err(f"not a file: {args['path']}")
    text = p.read_text(encoding="utf-8")
    old, new = args["old"], args["new"]
    count = text.count(old)
    if count == 0:
        return _err("`old` string not found")
    if count > 1:
        return _err(f"`old` string is not unique ({count} matches) — add context")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return _ok(f"edited {args['path']}")


def list_dir(args: dict, ctx: Context) -> dict:
    """List one directory level. Dirs get a trailing slash."""
    try:
        p = _safe(ctx, args.get("path", "."))
    except PathEscape as e:
        return _err(str(e))
    if not p.is_dir():
        return _err(f"not a directory: {args.get('path', '.')}")
    entries = sorted((c.name + "/" if c.is_dir() else c.name) for c in p.iterdir())
    return _ok("\n".join(entries) or "(empty)", count=len(entries))


def glob(args: dict, ctx: Context) -> dict:
    """Recursive filename match, e.g. pattern='**/*.py'. Paths are relative."""
    try:
        base = _safe(ctx, args.get("path", "."))
    except PathEscape as e:
        return _err(str(e))
    hits = [
        str(p.relative_to(base).as_posix())
        for p in sorted(base.glob(args["pattern"]))
        if p.is_file()
    ]
    shown = hits[:200]  # same cap as grep — don't flood model context
    out = "\n".join(shown) or "(no matches)"
    if len(hits) > 200:
        out += f"\n... (truncated at 200 of {len(hits)} matches)"
    return _ok(out, count=len(hits), shown=len(shown))


def grep(args: dict, ctx: Context) -> dict:
    """Regex content search across files. Optional `glob` filters filenames."""
    try:
        base = _safe(ctx, args.get("path", "."))
    except PathEscape as e:
        return _err(str(e))
    try:
        rx = re.compile(args["pattern"])
    except re.error as e:
        return _err(f"bad regex: {e}")
    name_glob = args.get("glob", "*")
    out: list[str] = []
    files = [base] if base.is_file() else base.rglob("*")
    for f in sorted(files):
        if not f.is_file() or not fnmatch.fnmatch(f.name, name_glob):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                rel = f.relative_to(base).as_posix() if base.is_dir() else f.name
                out.append(f"{rel}:{i}:{line}")
                if len(out) >= 200:  # ponytail: cap at 200 hits, plenty for a model
                    out.append("... (truncated at 200 matches)")
                    return _ok("\n".join(out), matches=len(out))
    return _ok("\n".join(out) or "(no matches)", matches=len(out))


# --- shell ------------------------------------------------------------------

def _exec(cmd, shell: bool, ctx: Context, timeout: int) -> dict:
    """Shared subprocess runner for bash_run/powershell_run. Captures merged
    stdout+stderr, enforces a timeout, kills the whole process tree on hang."""
    from providers.base import _kill_tree  # lazy — see import note at top of file
    try:
        proc = subprocess.Popen(
            cmd,
            shell=shell,
            cwd=str(ctx.workspace_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:  # e.g. powershell.exe absent on non-Windows
        return _err(str(e))
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, _ = proc.communicate()
        return _ok(f"[timed out after {timeout}s]\n{out or ''}", exit_code=None, timed_out=True)
    tail = out if len(out) <= 8000 else out[-8000:]  # keep the end (errors/result)
    return _ok(f"[exit {code}]\n{tail}", exit_code=code)


def active_bash_shell() -> str:
    """What bash_run actually runs *right now*: real bash if on PATH (POSIX
    works), else the platform default. The prompt states this so the model
    isn't misled by the tool name."""
    if shutil.which("bash"):
        return "bash"
    return "cmd.exe" if platform.system() == "Windows" else "/bin/sh"


def bash_run(args: dict, ctx: Context) -> dict:
    """Run a shell command in the workspace. Uses real bash when it's on PATH
    (so POSIX syntax works and the name isn't a lie); otherwise the platform
    default — cmd.exe on Windows, where POSIX operators (&&, /dev/null, $VAR)
    do NOT work (use powershell_run there)."""
    command = args["command"]
    timeout = int(args.get("timeout", 120))
    bash = shutil.which("bash")
    if bash:
        return _exec([bash, "-c", command], False, ctx, timeout)
    return _exec(command, True, ctx, timeout)


def powershell_run(args: dict, ctx: Context) -> dict:
    """Run a command in Windows PowerShell — the path to the registry, COM,
    native modules, and Windows-only CLIs that cmd/sh can't reach."""
    cmd = ["powershell.exe", "-NonInteractive", "-NoProfile",
           "-ExecutionPolicy", "Bypass", "-Command", args["command"]]
    return _exec(cmd, False, ctx, int(args.get("timeout", 120)))


# --- web --------------------------------------------------------------------

def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _ddg_unwrap(href: str) -> str:
    """DDG wraps result links as //duckduckgo.com/l/?uddg=<encoded-url>."""
    m = re.search(r"uddg=([^&]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    return "https:" + href if href.startswith("//") else href


_DDG_LINK = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIP = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _parse_ddg(html_text: str, n: int) -> list[dict]:
    links = _DDG_LINK.findall(html_text)
    snips = _DDG_SNIP.findall(html_text)
    results = []
    for i, (href, title) in enumerate(links[:n]):
        results.append({
            "url": _ddg_unwrap(href),
            "title": _strip_tags(title),
            "snippet": _strip_tags(snips[i]) if i < len(snips) else "",
        })
    return results


def web_search(args: dict, ctx: Context) -> dict:
    """Search the web via DuckDuckGo's HTML endpoint (no API key). Returns the
    top results as title / url / snippet so the model can then webfetch one."""
    n = int(args.get("max_results", 5))
    data = urllib.parse.urlencode({"q": args["query"]}).encode()
    req = urllib.request.Request(
        "https://html.duckduckgo.com/html/", data=data, headers={"User-Agent": _UA}
    )
    try:
        page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — network errors go back to the model
        return _err(f"search failed: {e}")
    results = _parse_ddg(page, n)
    if not results:
        return _ok("(no results)")
    blocks = [f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results]
    return _ok("\n\n".join(blocks), count=len(results))


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = html.unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", h).strip()


def webfetch(args: dict, ctx: Context) -> dict:
    """Fetch a URL and return its text (HTML is stripped to readable text).
    Output capped at 20k chars; bodies capped at 2 MB."""
    url = args["url"]
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        ctype = resp.headers.get("Content-Type", "")
        body = resp.read(2_000_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return _err(f"fetch failed: {e}")
    text = _html_to_text(body) if "html" in ctype.lower() else body
    return _ok(text[:20000], url=url, content_type=ctype)


# --- skills + done ----------------------------------------------------------

def load_skill(args: dict, ctx: Context) -> dict:
    """Return the full SKILL.md body for a named skill (progressive disclosure:
    only the frontmatter index is in the system prompt; the model pulls a body
    on demand). The model can then read referenced files via read_file."""
    if ctx.skills_root is None:
        return _err("no skills directory configured")
    name = args["name"]
    skill_md = ctx.skills_root / name / "SKILL.md"
    if not skill_md.is_file():
        return _err(f"unknown skill: {name}")
    return _ok(skill_md.read_text(encoding="utf-8", errors="replace"), skill=name)


def done(args: dict, ctx: Context) -> dict:
    """Model signals it is finished. `summary` becomes the run's final text."""
    return _ok(args.get("summary", "done"), done=True)


def demo() -> None:
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ctx = Context(workspace_root=Path(d))
        assert write_file({"path": "a/x.py", "content": "print(1)\n"}, ctx)["metadata"]["bytes"] == 9
        assert "print(1)" in read_file({"path": "a/x.py"}, ctx)["output"]
        edit_file({"path": "a/x.py", "old": "print(1)", "new": "print(2)"}, ctx)
        assert "print(2)" in read_file({"path": "a/x.py"}, ctx)["output"]
        assert "x.py" in glob({"pattern": "**/*.py"}, ctx)["output"]
        assert "x.py:1" in grep({"pattern": r"print"}, ctx)["output"]
        # clamp on by default ...
        assert read_file({"path": "../../secret"}, ctx)["metadata"].get("error")
        # ... off under full access (relative escape resolves, just doesn't exist)
        full = Context(workspace_root=Path(d), allow_outside_workspace=True)
        assert "not a file" in read_file({"path": "../nope.txt"}, full)["output"]
        r = bash_run({"command": f'"{sys.executable}" -c "print(42)"'}, ctx)
        assert "42" in r["output"], r["output"]
        # web parsers are pure-string (no network)
        assert _html_to_text("<p>hi <b>there</b></p>") == "hi there"
        assert _ddg_unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com") == "https://a.com"
    print("tools.demo OK")


if __name__ == "__main__":
    demo()
