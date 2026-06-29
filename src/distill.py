"""Experience distillation (self-evolution line 2).

Turns a finished run into a consolidated EXPERIENCE.md in its own session folder,
layered by altitude (meta = cross-domain strategy; domain = idea-class wins/losses).
Task-specific findings stay in REPORT/trajectory. Experience is recalled per-session
by recall.find_similar, not registered as global skills. Best-effort: never fail a run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .trajectory import _load_tree


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "general").lower()).strip("-") or "general"


def _domain(tree_meta: dict[str, Any], session_dir: Path) -> str:
    d = tree_meta.get("domain") or tree_meta.get("benchmark") or tree_meta.get("cwd")
    if not d:
        # session lives at <project>/.arbor/sessions/<run>; recover the project name.
        parts = session_dir.resolve().parts
        d = parts[-4] if len(parts) >= 4 and parts[-3:-1] == ("sessions",) else ""
        if not d and ".arbor" in parts:
            d = parts[parts.index(".arbor") - 1]
    return _slug(Path(str(d)).name) if d else "general"


def _frag(name: str, desc: str, when: str, title: str, body: list[str]) -> str:
    return "\n".join(["---", f"name: {name}", f"description: {desc}",
                      f"when_to_apply: {when}", "---", f"\n# {title}\n", *body])


def build_skills(session_dir: Path) -> list[tuple[str, str, str]]:
    """Return [(level, domain, markdown), ...] — empty if nothing worth keeping."""
    session_dir = Path(session_dir)
    tree = _load_tree(session_dir)
    if not tree:
        return []
    root = tree.get("ROOT") or {}
    meta = root.get("meta", {}) if isinstance(root.get("meta"), dict) else {}
    domain = _domain(meta, session_dir)
    out: list[tuple[str, str, list[str]]] = []  # (level, domain, bullets)

    # domain layer — verified wins/losses, transferable within domain
    wins = [n for n in tree.values() if n.get("status") in ("merged", "done") and n.get("insight")]
    dom_bul = []
    if root.get("insight"):
        dom_bul.append(root["insight"].strip().split("\n")[0][:200])
    for n in sorted(wins, key=lambda x: x.get("score") or 0, reverse=True):
        dom_bul.append(f"[{n.get('status')}, score={n.get('score')}] {n.get('insight','').strip()[:200]}")
    if dom_bul:
        out.append(("domain", domain, dom_bul))

    # meta layer — strategy from the tree's shape, transfers across domains
    pruned = [n for n in tree.values() if n.get("status") == "pruned"]
    process = [f"dead-end: {n.get('insight','').strip()[:160]}" for n in pruned if n.get("insight")]
    try:
        from .experience import load_experience
        trail = [e for e in load_experience(session_dir) if e.get("status") in ("pruned", "done")]
        process += [f"{e['node_id']}: {e['insight'][:140]}" for e in trail if e.get("insight")]
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    if process:
        out.append(("meta", "general", process))
    return out


def _run_coro(coro: Any) -> Any:
    """Run an async provider call from sync finalize, even inside a live loop.

    ``asyncio.run`` raises if a loop is already running (which is why the earlier
    abstraction pass silently fell back). Run it in a dedicated thread instead.
    """
    import asyncio
    import concurrent.futures
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _raw_lessons(frags: list[tuple[str, str, list[str]]]) -> list[str]:
    """Flatten layered bullets into raw lessons, dropping markdown-heading noise."""
    raw: list[str] = []
    for _level, _d, bullets in frags:
        for b in bullets:
            b = b.strip()
            if b and not b.lstrip("[").startswith("#") and "##" not in b:
                raw.append(b)
    return raw


_MINE_SYS = (
    "You review one optimization run and surface CONCRETE, situational findings worth "
    "remembering for the next run on this same dataset / task / harness. Keep them "
    "SPECIFIC — a dataset quirk that helped the metric, a trap an executor or the harness "
    "fell into. NOT generic advice or principles. One finding per line, format:\n"
    "[leverage|pitfall] SUBJECT: the concrete finding\n"
    "where SUBJECT is the specific thing it concerns (the dataset, an executor, the "
    "harness, a numpy call...). Return only such lines, or nothing if none."
)


def _mine_findings(provider: Any, raw: list[str]) -> list[dict[str, str]]:
    """B: mine the run's lessons for concrete findings not explicitly logged."""
    if not provider or not raw:
        return []
    try:
        msg = "Run material:\n" + "\n".join(f"- {b}" for b in raw)
        resp = _run_coro(provider.create(system=_MINE_SYS,
                                         messages=[{"role": "user", "content": msg}], max_tokens=700))
        found = []
        for ln in resp.get_text().splitlines():
            m = re.match(r"\s*\[?(leverage|pitfall)\]?\s*([^:]*):\s*(.+)", ln, re.I)
            if m:
                about = m.group(2).strip()
                if about.lower() in ("about", "subject"):  # guard leaked placeholder
                    about = ""
                found.append({"kind": m.group(1).lower(), "about": about, "note": m.group(3).strip()})
        return found
    except Exception:  # pylint: disable=broad-exception-caught
        return []


def distill_to_session(session_dir: Path, provider: Any = None) -> Path | None:
    """Write EXPERIENCE.md: the run's concrete findings (logged live + mined).

    Experience here is specific and situational by design — dataset quirks that
    help, traps to avoid — for the next run on the same/similar target. Combines
    findings the agent logged via RecordFinding (A) with an LLM mining pass (B).
    """
    session_dir = Path(session_dir)
    domain = _domain({}, session_dir)

    findings: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(f: dict[str, str]) -> None:
        note = (f.get("note") or "").strip()
        key = re.sub(r"\W+", "", note.lower())[:60]
        if note and key not in seen:
            seen.add(key)
            findings.append({"kind": f.get("kind", ""), "about": f.get("about", ""), "note": note})

    try:  # A: explicitly logged findings
        from .experience import load_findings
        for f in load_findings(session_dir):
            _add(f)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    raw = _raw_lessons(build_skills(session_dir))  # B: mine the rest from the run
    for f in _mine_findings(provider, raw):
        _add(f)

    if not findings:
        return None
    lines = [f"- **[{(f['kind'] or 'finding')}] {f['about']}** — {f['note']}" if f["about"]
             else f"- **[{f['kind'] or 'finding'}]** {f['note']}" for f in findings]
    md = _frag(f"experience-{domain}", f"Concrete findings from a {domain} run.",
               "reuse when working on this dataset / task / harness again",
               f"Findings: {domain}", lines)
    out = session_dir / "EXPERIENCE.md"
    out.write_text(md, encoding="utf-8")
    return out
