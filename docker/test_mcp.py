#!/usr/bin/env python3
"""Integration test for the containerized arbor MCP server.

Spawns the container as a subprocess, speaks MCP over stdio, and drives 3 use
cases (baseline eval → experiment+merge → prune+report) with assertions.

Self-contained: builds a throwaway toy benchmark under /tmp, uses a dummy API
key (no tool in the loop calls the LLM), and tears everything down at the end.
ALL git operations (worktrees, commits, merges) are scoped to the toy repo at
<workspace>/test-bench; merges target only its non-protected ``trunk`` branch —
no real repository is ever touched.

Prereq: ``docker compose build`` (from the repo root).
Run:     ``python docker/test_mcp.py``
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import queue
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.yml"
FIXTURE = REPO / "docker" / "test-bench"
CTR = "arbor-mcp-test"
WS = Path("/tmp/arbor-mcp-test-ws")
CWD = "/workspace/test-bench"  # in-container path (WS is mounted at /workspace)


class MCP:
    """Minimal MCP stdio client: spawns the container, parses JSON-RPC over its
    stdout with a streaming decoder (robust to the SDK's keepalive spaces)."""

    def __init__(self, cmd: list[str], env: dict[str, str]):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  env=env, bufsize=-1)
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self._id = 0

    def _reader(self) -> None:
        buf = b""
        dec = json.JSONDecoder()
        while True:
            chunk = self.p.stdout.read1(4096)  # type: ignore[union-attr]
            if not chunk:
                break
            buf += chunk
            s = buf.decode("utf-8", "replace")
            i = 0
            while i < len(s):
                while i < len(s) and s[i] in " \t\r\n\x00":  # skip keepalive
                    i += 1
                if i >= len(s):
                    break
                try:
                    obj, i = dec.raw_decode(s, i)
                    self.q.put(obj)
                except json.JSONDecodeError:
                    break
            buf = s[i:].encode()

    def call(self, method: str, params: dict | None = None, timeout: int = 60) -> dict:
        self._id += 1
        mid = self._id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write((json.dumps(msg) + "\n").encode())  # type: ignore[union-attr]
        self.p.stdin.flush()  # type: ignore[union-attr]
        end = time.time() + timeout
        while time.time() < end:
            try:
                obj = self.q.get(timeout=1)
            except queue.Empty:
                continue
            if obj.get("id") == mid:
                return obj
        raise TimeoutError(f"no response for {method} id={mid}")

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self.p.stdin.write((json.dumps(msg) + "\n").encode())  # type: ignore[union-attr]
        self.p.stdin.flush()  # type: ignore[union-attr]

    def tool(self, name: str, args: dict, timeout: int = 60) -> dict:
        r = self.call("tools/call", {"name": name, "arguments": args}, timeout)
        res = r.get("result", r)
        if res.get("isError"):
            raise RuntimeError(f"{name}: {res['content'][0]['text']}")
        sc = res.get("structuredContent")
        if sc:
            return sc
        txt = res["content"][0]["text"]
        try:
            return json.loads(txt)
        except Exception:
            return {"text": txt}

    def close(self) -> None:
        for _ in range(2):
            try:
                self.p.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def exec_ctr(cmd: str) -> tuple[int, str, str]:
    r = subprocess.run(["docker", "exec", CTR, "sh", "-c", cmd],
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def setup_workspace() -> None:
    if WS.exists():
        shutil.rmtree(WS)
    tb = WS / "test-bench"
    shutil.copytree(FIXTURE, tb)
    g = lambda *a: subprocess.run(a, cwd=tb, check=True,
                                  capture_output=True, text=True)
    g("git", "init", "-q")
    g("git", "add", "-A")
    g("git", "-c", "user.email=t@t.t", "-c", "user.name=T", "commit", "-qm", "baseline")
    g("git", "branch", "-M", "main")
    g("git", "branch", "trunk", "main")  # non-protected target for merges


def main() -> int:
    setup_workspace()
    env = os.environ.copy()
    env.update(ARBOR_WORKSPACE=str(WS),
               ARBOR_UID=str(os.getuid()), ARBOR_GID=str(os.getgid()),
               ARBOR_PROVIDER="openai-chat", ARBOR_MODEL="test",
               ARBOR_API_KEY="dummy")
    subprocess.run(["docker", "rm", "-f", CTR], capture_output=True)

    cmd = ["docker", "compose", "-f", str(COMPOSE), "run", "--rm", "--name", CTR, "-T", "arbor"]
    m = MCP(cmd, env)
    try:
        init = m.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1.0"}})
        si = init["result"]["serverInfo"]
        ntools = len(m.call("tools/list")["result"]["tools"])
        print(f"INIT: {si['name']} {si['version']}  ({ntools} tools)\n")
        m.notify("notifications/initialized")

        results: list[bool] = []

        def check(label: str, cond: bool, detail: str = "") -> None:
            results.append(cond)
            print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  -- {detail}" if not cond else ""))

        # ── UC1: baseline eval ──────────────────────────────────────────
        print("-- UC1: baseline setup + eval --")
        m.tool("tree_set_meta", {"run_name": "bench", "eval_cmd": "python eval.py",
                                 "metric_direction": "maximize", "trunk_branch": "trunk", "cwd": CWD})
        ev = m.tool("eval_run", {"run_name": "bench", "cmd": "python eval.py",
                                 "split": "dev", "set_meta": "baseline", "cwd": CWD})
        check("UC1 baseline eval ran (returncode 0)", ev.get("returncode") == 0, str(ev)[:100])
        check("UC1 baseline score == 19.0", ev.get("score") == 19.0, str(ev.get("score")))
        m.tool("eval_run", {"run_name": "bench", "cmd": "python eval.py",
                            "split": "test", "set_meta": "test_baseline", "cwd": CWD})
        tv = m.tool("tree_view", {"run_name": "bench", "fmt": "compact", "cwd": CWD})
        check("UC1 tree records baseline=19", "baseline=19" in str(tv), str(tv)[:100])

        # ── UC2: hypothesis → worktree → edit → eval → merge into trunk ─
        print("\n-- UC2: hypothesis -> experiment -> merge (trunk, toy repo only) --")
        m.tool("tree_add_node", {"run_name": "bench", "parent_id": "ROOT",
                                 "hypothesis": "set LR=0.1 (peak)", "cwd": CWD})
        wt = m.tool("worktree_create", {"run_name": "bench", "node_id": "1", "cwd": CWD})
        wp, wb = wt["worktree"], wt["branch"]
        check("UC2 worktree created", bool(wp), str(wt)[:100])
        rc, _, err = exec_ctr(f"cd '{wp}' && sed -i 's/LEARNING_RATE = 0.01/LEARNING_RATE = 0.1/' solution.py && "
                              f"git -c user.email=t@t.t -c user.name=T commit -qam 'exp: LR=0.1'")
        check("UC2 edit+commit in worktree", rc == 0, err)
        ev2 = m.tool("eval_run", {"run_name": "bench", "cmd": "python eval.py", "split": "test",
                                  "set_meta": "none", "exec_cwd": wp, "cwd": CWD})
        check("UC2 experiment score == 100.0", ev2.get("score") == 100.0, str(ev2.get("score")))
        try:
            m.tool("git_merge_branch", {"run_name": "bench", "node_id": "1", "source_branch": wb,
                                        "target_branch": "trunk", "test_score": 100.0, "dry_run": True, "cwd": CWD})
            check("UC2 dry-run merge guards pass", True)
        except RuntimeError as e:
            check("UC2 dry-run merge guards pass", False, str(e)[:100])
        mrg = m.tool("git_merge_branch", {"run_name": "bench", "node_id": "1", "source_branch": wb,
                                          "target_branch": "trunk", "test_score": 100.0,
                                          "commit_message": "merge: LR=0.1 (19->100)", "cwd": CWD})
        check("UC2 merge executed (trunk, toy repo)", "merged" in str(mrg).lower() or mrg.get("merged") is not False, str(mrg)[:100])
        _, log, _ = exec_ctr(f"cd {CWD} && git --no-pager log trunk --oneline | head -3")
        check("UC2 trunk contains the experiment commit", "LR=0.1" in log, log)
        m.tool("tree_update_node", {"run_name": "bench", "node_id": "1", "status": "merged", "score": 100.0, "cwd": CWD})

        # ── UC3: bad hypothesis → prune → report → dashboard ────────────
        print("\n-- UC3: bad hypothesis -> prune -> report -> dashboard --")
        m.tool("tree_add_node", {"run_name": "bench", "parent_id": "ROOT",
                                 "hypothesis": "set LR=1.0 (overshoot)", "cwd": CWD})
        wt2 = m.tool("worktree_create", {"run_name": "bench", "node_id": "2", "cwd": CWD})
        wp2 = wt2["worktree"]
        rc, _, err = exec_ctr(f"cd '{wp2}' && sed -i 's/LEARNING_RATE = 0.01/LEARNING_RATE = 1.0/' solution.py && "
                              f"git -c user.email=t@t.t -c user.name=T commit -qam 'exp: LR=1.0'")
        check("UC3 edit+commit bad experiment", rc == 0, err)
        ev3 = m.tool("eval_run", {"run_name": "bench", "cmd": "python eval.py", "split": "dev",
                                  "exec_cwd": wp2, "cwd": CWD})
        check("UC3 bad score < baseline (19)", ev3.get("score", 999) < 19, str(ev3.get("score")))
        m.tool("tree_prune", {"run_name": "bench", "node_id": "2", "reason": "score regressed", "cwd": CWD})
        check("UC3 bad node pruned", True)
        m.tool("generate_report", {"run_name": "bench", "cwd": CWD})
        _, ls, _ = exec_ctr(f"ls {CWD}/.arbor/sessions/bench/REPORT.md 2>/dev/null && echo FOUND")
        check("UC3 REPORT.md written in test-bench", "FOUND" in ls, ls)
        dash = m.tool("open_dashboard", {"run_name": "bench", "cwd": CWD})
        check("UC3 dashboard URL returned", "http" in str(dash), str(dash)[:100])

        passed = sum(1 for c in results if c)
        print(f"\n=== {passed}/{len(results)} checks passed ===")
        return 0 if passed == len(results) else 1
    finally:
        m.close()
        subprocess.run(["docker", "rm", "-f", CTR], capture_output=True)
        shutil.rmtree(WS, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
