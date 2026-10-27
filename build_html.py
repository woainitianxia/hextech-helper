# -*- coding: utf-8 -*-
"""把 data3.js 的数据灌进 template.html，生成 index.html 和 海克斯决策助手.html。"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(
    HERE, "..", "想法交付包-X-20261025-01-v1", "04-项目文件包"))

tpl = io.open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
raw = io.open(os.path.join(HERE, "data3.js"), encoding="utf-8").read().strip()
if raw.startswith("var DB="):
    raw = raw[len("var DB="):]
raw = raw.rstrip().rstrip(";")

assert "/*__DATA__*/" in tpl, "模板里找不到注入点"
# 用 str.replace 而不是 re —— 数据里有 $ 和反斜杠，正则替换会出事
out = tpl.replace("/*__DATA__*/", raw, 1)

for name in ("index.html", "海克斯决策助手.html"):
    p = os.path.join(PROJ, name)
    io.open(p, "w", encoding="utf-8").write(out)
    print("%-24s %d 字节" % (name, len(out.encode("utf-8"))))
print("注入数据长度:", len(raw))
