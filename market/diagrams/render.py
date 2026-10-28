"""Render market/diagrams/*.mmd to PNG with headless Chromium + mermaid.js.

Usage: python3 market/diagrams/render.py market/diagrams market/diagrams-png
mermaid.min.js is downloaded to /tmp on first use; CJK text needs a CJK font (Noto Sans CJK SC).
"""
import html, pathlib, re, subprocess, sys, urllib.request

MERMAID = pathlib.Path("/tmp/mermaid.min.js")
if not MERMAID.exists():
    urllib.request.urlretrieve("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js", MERMAID)

SRC = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
CHROME = ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
          "--virtual-time-budget=20000"]
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>body{{margin:0;padding:24px;background:#fff}} .mermaid{{margin:0}}</style>
<script src="file:///tmp/mermaid.min.js"></script></head><body>
<pre class="mermaid">{code}</pre>
<script>
mermaid.initialize({{startOnLoad:false, theme:'default', securityLevel:'loose',
  fontFamily:'"Noto Sans CJK SC","Noto Sans",sans-serif',
  flowchart:{{useMaxWidth:false, htmlLabels:true, curve:'basis', nodeSpacing:40, rankSpacing:50, wrappingWidth:340}},
  sequence:{{useMaxWidth:false, mirrorActors:false, wrap:true, width:170}}}});
mermaid.run().then(()=>document.body.setAttribute('data-done','1'))
  .catch(e=>{{document.body.setAttribute('data-error', String(e && e.message || e));}});
</script></body></html>"""

for mmd in sorted(SRC.glob("*.mmd")):
    page = pathlib.Path("/tmp") / (mmd.stem + ".html")
    page.write_text(PAGE.format(code=html.escape(mmd.read_text())), encoding="utf-8")
    dom = subprocess.run(CHROME + ["--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=120).stdout
    err = re.search(r'data-error="([^"]*)"', dom)
    if err or "Syntax error" in dom or 'data-done="1"' not in dom:
        print(f"{mmd.name}: RENDER FAILED {err.group(1)[:300] if err else ''}")
        continue
    box = re.search(r'<svg[^>]*viewBox="([\d.\-]+) ([\d.\-]+) ([\d.]+) ([\d.]+)"', dom)
    width, height = int(float(box.group(3))) + 48, int(float(box.group(4))) + 48
    png = OUT / (mmd.stem + ".png")
    subprocess.run(CHROME + ["--force-device-scale-factor=2", f"--window-size={width},{height}",
                             f"--screenshot={png}", page.as_uri()], capture_output=True, timeout=120)
    print(f"{mmd.name}: ok {width}x{height} -> {png.name} ({png.stat().st_size // 1024} KB)")
