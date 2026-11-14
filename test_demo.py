#!/usr/bin/env python3
"""Privacy, data, live dispatch, authentication and browser checks."""
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parent
RANGES = ("24h", "7d", "all")
CHROME = os.environ.get("CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SENSITIVE = ["".join(parts) for parts in (
    ("rola", "nd"), ("rw", "sobest"), ("rw", "macmini"), ("tail", "9fe984"),
    ("04", " 人脉"), ("ls", "ss"), ("lau", "ren"), ("kirs", "ten"), ("is", "aac"),
)]


def free_ports():
    sockets = []
    try:
        for _ in range(2):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def request(url, **kwargs):
    with urlopen(url, timeout=8, **kwargs) as response:
        return response.status, response.read()


BROWSER_SCRIPT = r'''const [port, fileUrl, httpUrl] = process.argv.slice(2);
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(targets.find(x => x.type === "page").webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let id = 0; const pending = new Map();
ws.onmessage = ({data}) => { const m = JSON.parse(data); if (m.id && pending.has(m.id)) { const [ok, fail] = pending.get(m.id); pending.delete(m.id); m.error ? fail(Error(m.error.message)) : ok(m.result); } };
function cmd(method, params = {}) { return new Promise((ok, fail) => { const n = ++id; pending.set(n, [ok, fail]); ws.send(JSON.stringify({id:n, method, params})); }); }
async function run(expression) { const r = await cmd("Runtime.evaluate", {expression, returnByValue:true, awaitPromise:true}); if (r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails)); return r.result.value; }
async function until(expression) { for (let i=0; i<120; i++) { try { if (await run(expression)) return; } catch (_) {} await new Promise(r=>setTimeout(r,100)); } throw Error("Timed out: " + expression); }
function check(ok, what) { if (!ok) throw Error(what); }
await cmd("Page.enable"); await cmd("Runtime.enable");
await cmd("Page.navigate", {url:fileUrl});
await until('document.querySelectorAll(".accounts .card").length > 0');
await run(`document.querySelector('[data-range="24h"]').click()`);
await until('latestData && latestData.range === "24h"');
await run('document.querySelector("[data-tab=dispatch]").click()');
await until(`!!document.querySelector('[data-quest-idx="2"]')`);
await run(`document.querySelector('[data-quest-idx="2"]').click()`);
check(await run('document.querySelector(".ins-head").textContent.includes("#2")'), "quest inspector");
await run('document.querySelector("[data-tab=models]").click()');
await until('!!document.querySelector("[data-model-row=claude-fable-5-2]")');
await run('document.querySelector("[data-eff-model=claude-fable-5-2][data-eff-val=max]").click()');
check(await run('document.querySelector("[data-model-row=claude-fable-5-2]").textContent.includes("65") && document.querySelector("[data-model-row=claude-fable-5-2] .cost-val").textContent.includes("26.54")'), "effort score and cost");
const before = await run('[...document.querySelectorAll("[data-model-row]")].map(x=>x.dataset.modelRow).join(",")');
await run('document.querySelector("[data-sort=cost]").click()');
const after = await run('[...document.querySelectorAll("[data-model-row]")].map(x=>x.dataset.modelRow).join(",")');
check(before !== after, "sort order");
await run('document.querySelector("[data-tier-filter=low]").click()');
check(await run('document.querySelectorAll("[data-model-row]").length < 18'), "tier filter");
await cmd("Page.navigate", {url:httpUrl});
await until('!!document.querySelector(".accounts .card")');
await run(`document.querySelector('[data-range="24h"]').click()`);
await until('latestData && latestData.range === "24h"');
await cmd("Page.addScriptToEvaluateOnNewDocument", {source:'window.fetch = async () => new Response("missing", {status:404});'});
await cmd("Page.navigate", {url:httpUrl});
await until('!!document.querySelector(".accounts .card")');
check(await run('!document.querySelector(".banner") && latestData.range === "24h"'), "HTTP fallback");
ws.close(); console.log("browser interactions passed");
'''


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix=".test-", dir=ROOT)
        cls.temp = Path(cls.tmp.name)
        cls.dispatch = cls.temp / "dispatch"
        shutil.copytree(ROOT / "demo-data" / "dispatch", cls.dispatch)
        cls.port, cls.public_port = free_ports()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.public = f"http://127.0.0.1:{cls.public_port}"
        cls.env = os.environ.copy()
        cls.env.update(USAGE_DISPATCH_DIR=str(cls.dispatch), USAGE_AUTH_DIR=str(cls.temp / "auth"), PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([sys.executable, "-c", "import server; server.save_auth_config('demo-test-password')"], cwd=ROOT, env=cls.env, check=True)
        cls.proc = subprocess.Popen([sys.executable, "server.py", "--demo", "--port", str(cls.port), "--public-port", str(cls.public_port), "--no-open"], cwd=ROOT, env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                if request(cls.base + "/")[0] == 200:
                    return
            except (OSError, URLError):
                if cls.proc.poll() is not None:
                    break
                time.sleep(0.1)
        cls.tearDownClass()
        raise RuntimeError("demo server failed to start")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "proc"):
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait()
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    def test_01_sensitive_text_and_identifiers(self):
        allowed = {".md", ".html", ".js", ".py", ".json", ".tsv", ".txt"}
        findings = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(p in {".git", "__pycache__", ".state", ".data", ".omp"} or p.startswith(".test-") for p in path.relative_to(ROOT).parts):
                continue
            if path.suffix not in allowed and path.name not in {"LICENSE", ".gitignore"}:
                continue
            text = path.read_text(encoding="utf-8").lower()
            for word in SENSITIVE:
                if word.lower() in text:
                    findings.append((str(path.relative_to(ROOT)), word))
        self.assertEqual(findings, [])

    def test_02_three_complete_consistent_snapshots(self):
        data = {}
        for name in RANGES:
            data[name] = json.loads((ROOT / "demo-data" / "snapshots" / f"{name}.json").read_text())
            snap = data[name]
            self.assertEqual(snap["range"], name)
            self.assertGreater(len(snap["accounts"]), 0)
            self.assertGreater(snap["spend"]["requests"], 0)
            self.assertGreater(snap["spend"]["cost"], 0)
            self.assertEqual(len(snap["dispatch"]["routes"]), 13)
            self.assertEqual(len(snap["dispatch"]["capabilities"]), 18)
            self.assertTrue(all(a.get("email", "").endswith("@demo.local") for a in snap["accounts"]))
            self.assertTrue(all(not a.get("accountId") or a["accountId"].startswith("demo-account-") for a in snap["accounts"]))
        script = (ROOT / "demo-data" / "demo-snapshots.js").read_text()
        self.assertTrue(script.startswith("window.DEMO_SNAPSHOTS = "))
        self.assertEqual(json.loads(script.split("= ", 1)[1].removesuffix(";\n")), data)

    def test_03_http_assets_and_live_dispatch(self):
        for endpoint in ("/", "/vendor-icons.js", "/assets/zhuque.woff2", "/demo-data/demo-snapshots.js", "/api/snapshot?range=7d"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(request(self.base + endpoint)[0], 200)
        def payload():
            return json.loads(request(self.base + "/api/snapshot?range=7d")[1])
        original_snapshot = json.loads((ROOT / "demo-data" / "snapshots" / "7d.json").read_text())
        live = payload()
        source_counts = {c["model"]: (c["mainRequests"], c["subRequests"]) for c in original_snapshot["dispatch"]["capabilities"]}
        self.assertEqual(source_counts, {c["model"]: (c["mainRequests"], c["subRequests"]) for c in live["dispatch"]["capabilities"]})
        table = self.dispatch / "任务类型表.md"
        old = table.read_text()
        card = self.dispatch / "模型卡" / "claude-fable-5-2.md"
        old_card = card.read_text()
        aa = next(self.dispatch.glob("AA排行榜-*.tsv"))
        old_aa = aa.read_text()
        try:
            self.assertIn("| 1 | 长会话主持与协调 |", old)
            table.write_text(old.replace("| 1 | 长会话主持与协调 |", "| 1 | 演示关卡变更 |", 1))
            card.write_text(old_card.replace("- 标签：", "- 标签：演示标签、", 1))
            aa.write_text(old_aa.replace("Claude Fable 5.2 (high)\t1M\tAnthropic\t62\t", "Claude Fable 5.2 (high)\t1M\tAnthropic\t61\t", 1))
            updated = payload()
            self.assertEqual(updated["dispatch"]["routes"][0]["task"], "演示关卡变更")
            cap = next(c for c in updated["dispatch"]["capabilities"] if c["model"] == "claude-fable-5-2")
            self.assertIn("演示标签", cap["tags"])
            self.assertEqual(next(e for e in cap["efforts"] if e["effort"] == "high")["index"], "61")
        finally:
            table.write_text(old)
            card.write_text(old_card)
            aa.write_text(old_aa)
            payload()

    def test_04_generated_status_bounded(self):
        request(self.base + "/api/snapshot?range=all")
        status = (self.dispatch / "当前状态.md").read_text()
        self.assertLessEqual(len(status.splitlines()), 40)
        self.assertIn("## 余量", status)
        self.assertIn("## 提示", status)

    def test_05_javascript_syntax(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for syntax checks")
        html = (ROOT / "index.html").read_text()
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", html, re.I)
        inline = self.temp / "inline.js"
        inline.write_text("\n".join(scripts))
        for path in (ROOT / "vendor-icons.js", ROOT / "demo-data" / "demo-snapshots.js", inline):
            with self.subTest(path=path.name):
                subprocess.run([node, "--check", str(path)], check=True, capture_output=True)

    def test_06_headings_style_and_screenshots(self):
        forbidden = re.compile(r"不是[^。；\n，]{1,30}，(?:而)?是|不[^。；\n，]{1,20}，只|不在[^。；\n，]{1,20}，在")
        for path in list(ROOT.rglob("*.md")) + [ROOT / "index.html"]:
            if any(p.startswith(".test-") or p == ".git" for p in path.relative_to(ROOT).parts):
                continue
            text = path.read_text()
            self.assertIsNone(forbidden.search(text), path.name)
            self.assertNotRegex(text, r"font-style\s*:\s*italic")
            for heading in re.findall(r"(?m)^#{1,3}\s+(.+)$", text):
                self.assertNotRegex(heading.strip(), r"[。，；,;.]$")
            for heading in re.findall(r"<h[1-3](?:\s[^>]*)?>([^<]+)</h[1-3]>", text, re.I):
                self.assertNotRegex(heading.strip(), r"[。，；,;.]$")
        sizes = {"01-overview.png": (1420, 1650), "02-quest-pipeline.png": (1420, 1380), "03-models-matrix.png": (1420, 1450), "04-mobile-view.png": (500, 1150)}
        for name, expected in sizes.items():
            path = ROOT / "screenshots" / name
            self.assertGreater(path.stat().st_size, 50_000, name)
            with path.open("rb") as f:
                head = f.read(24)
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", head[16:24]), expected)

    def test_07_public_listener_authentication(self):
        with self.assertRaises(HTTPError) as error:
            request(self.public + "/api/snapshot?range=7d")
        self.assertEqual(error.exception.code, 401)
        self.assertEqual(request(self.public + "/login")[0], 200)
        jar = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        req = Request(self.public + "/auth/login", data=b"password=demo-test-password", method="POST")
        with opener.open(req, timeout=8) as response:
            self.assertEqual(response.status, 200)
        with opener.open(self.public + "/api/snapshot?range=7d", timeout=8) as response:
            self.assertTrue(json.load(response)["publicSession"])
        opener.open(Request(self.public + "/auth/logout", data=b"", method="POST"), timeout=8).close()
        with self.assertRaises(HTTPError) as error:
            opener.open(self.public + "/api/snapshot?range=7d", timeout=8)
        self.assertEqual(error.exception.code, 401)

    @unittest.skipUnless(Path(CHROME).is_file() and shutil.which("node"), "Chrome and Node.js are required")
    def test_08_browser_interactions(self):
        profile = self.temp / "chrome-profile"
        profile.mkdir()
        chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = profile / "DevToolsActivePort"
            for _ in range(100):
                if portfile.exists():
                    break
                time.sleep(0.1)
            self.assertTrue(portfile.exists(), "Chrome DevTools did not start")
            port = portfile.read_text().splitlines()[0]
            script = self.temp / "browser.mjs"
            script.write_text(BROWSER_SCRIPT)
            result = subprocess.run(["node", str(script), port, (ROOT / "index.html").as_uri() + "#overview", self.base + "/#overview"], cwd=ROOT, timeout=45, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
