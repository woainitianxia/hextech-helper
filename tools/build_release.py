# 打包 + 出发布包
#
#   python tools/build_release.py
#
# 产物：
#   dist/海克斯决策助手/            可以直接双击运行
#   release/HextechHelper-win64.zip 传到 GitHub Releases 的那个包
#
# 版本号只改 updater.py 里的 VERSION。

import hashlib
import io
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import updater  # noqa: E402

NAME = "海克斯决策助手"
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
STAGE = os.path.join(ROOT, ".stage")
RELEASE = os.path.join(ROOT, "release")
ZIPNAME = "HextechHelper-win64.zip"


def run(cmd, **kw):
    print("  $", " ".join(cmd) if isinstance(cmd, list) else cmd)
    r = subprocess.run(cmd, cwd=ROOT, **kw)
    if r.returncode != 0:
        raise SystemExit("命令失败，退出码 %d" % r.returncode)


def main():
    print("版本号:", updater.VERSION)

    print("[1/5] 生成 index.html")
    run([sys.executable, "build_html.py"])

    print("[2/5] 准备打包目录")
    shutil.rmtree(STAGE, ignore_errors=True)
    os.makedirs(STAGE, exist_ok=True)
    for f in ["app.py", "ocr_scan.py", "server.py", "live.py", "updater.py",
              "names.json", "index.html"]:
        shutil.copy2(os.path.join(ROOT, f), STAGE)

    print("[3/5] PyInstaller 打包（约 1 分钟）")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed",
         "--name", NAME, "--distpath", DIST, "--workpath", BUILD,
         "--specpath", BUILD,
         "--collect-all", "rapidocr_onnxruntime", "--collect-all", "mss",
         "--collect-all", "pyclipper", "--collect-all", "shapely",
         "--collect-all", "onnxruntime",
         "--add-data", os.path.join(STAGE, "index.html") + ";.",
         "--add-data", os.path.join(STAGE, "names.json") + ";.",
         os.path.join(STAGE, "app.py")])

    app_dir = os.path.join(DIST, NAME)

    # update.json 必须跟着 exe 走，否则装到别人机器上不知道去哪个仓库查新版
    cfg_src = os.path.join(ROOT, "update.json")
    if not os.path.exists(cfg_src):
        raise SystemExit("缺少 update.json（要写 owner / repo）")
    shutil.copy2(cfg_src, os.path.join(app_dir, "update.json"))

    print("[4/5] 压缩发布包")
    os.makedirs(RELEASE, exist_ok=True)
    out = os.path.join(RELEASE, ZIPNAME)
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, _dirs, files in os.walk(app_dir):
            for fn in files:
                fp = os.path.join(base, fn)
                z.write(fp, os.path.join(NAME, os.path.relpath(fp, app_dir)))

    size = os.path.getsize(out)
    h = hashlib.sha256()
    with io.open(out, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    print("[5/5] 完成")
    print("")
    print("  包：%s" % out)
    print("  大小：%.1f MB" % (size / 1024.0 / 1024.0))
    print("  sha256：%s" % h.hexdigest())
    print("")
    print("  发版：GitHub → Releases → Draft a new release")
    print("        tag 填 v%s" % updater.VERSION)
    print("        把 %s 拖进去 → Publish" % ZIPNAME)


if __name__ == "__main__":
    main()
