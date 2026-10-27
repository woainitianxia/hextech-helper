# 海克斯决策助手

《英雄联盟》海克斯大乱斗（ARAM Mayhem）的三选一决策助手：把游戏里弹出来的三张卡
认出来，按胜率、你的英雄、双方阵容给个建议。

- 三选一弹出来时点一下「识别游戏里的三个」，它直接抓游戏窗口（工具窗口挡住也不影响）
- 数据来自 [ARAMKit](https://aramkit.cn/zh-CN/augments)，每次启动自动联网更新
- 不注入游戏、不读内存、不改游戏文件；截图只在内存里过一遍，不落盘

## 本地跑（开发）

需要 Python 3.13 + 依赖：

```
pip install pywebview rapidocr_onnxruntime mss pyclipper shapely onnxruntime pillow numpy
```

```
python build_html.py      # template.html + data3.js → index.html
python app.py             # 直接跑
```

## 打包 + 发版（自带自动更新的关键）

```
python tools/build_release.py
```

它会：
1. 重新生成 `index.html`
2. PyInstaller 打成 `dist/海克斯决策助手/`
3. 把 `update.json` 放进包里（**必须**，更新器靠它知道去哪个仓库查新版）
4. 压成 `release/HextechHelper-win64.zip`，并打印它的 sha256

然后把 zip 传到 GitHub Releases，tag 用 `v<版本号>`，例如 `v2026.10.28`。
**版本号只在 `updater.py` 的 `VERSION` 一处改。**

## 自动更新怎么工作

```
程序启动 → 查 https://api.github.com/repos/<owner>/<repo>/releases/latest
        → tag 比本地 VERSION 新 → 顶栏显示「有新版本 x.y」
        → 点「升级」→ 下 zip 到临时目录 → 解压
        → 点「立即重启并升级」→ 程序退出 → PowerShell 脚本覆盖文件 → 自动重新打开
```

`update.json`（和 exe 放一起，用户可自己改）：

```json
{ "owner": "你的GitHub用户名", "repo": "仓库名", "asset": "HextechHelper-win64.zip" }
```

没有这个文件 → 自动更新自动关闭，其它功能照常。

## 目录

| 文件 | 干什么 |
|---|---|
| `app.py` | 桌面版外壳（pywebview）、窗口、置顶、热键、对外接口 |
| `ocr_scan.py` | 抓图 + 文字检测/识别 + 名字匹配 |
| `updater.py` | 自更新：查版本、下载、解压、替换 |
| `server.py` | 帮页面去 ARAMKit 取数据（本地 8765 端口，网页版用） |
| `live.py` | 从游戏客户端读当前英雄和双方阵容 |
| `template.html` | 页面模板（会被 build_html.py 注入数据） |
| `data3.js` | 海克斯数据快照（离线兜底） |
| `names.json` | 识别用的名字表（索引要和 data3.js 的数组一致） |

## 说明

- 只做整理与对比，不保证任何对局结果
- 数据版权归 ARAMKit；本工具只读取公开页面
