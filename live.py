# -*- coding: utf-8 -*-
"""
读游戏内实时数据。

用的是拳头官方给本地开的一个只读接口（游戏进程自己开的，127.0.0.1:2999）。
它只在对局进行中可用，能读到：你在玩哪个英雄、几级、装备、双方阵容、游戏时间。
读不到：海克斯三选一弹了什么（这个接口不暴露 UI 层的东西）。

程序不注入游戏、不读内存、不改任何东西，只是像浏览器一样请求这个本地地址。
"""
import json
import ssl
import urllib.request

LIVE_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
TIMEOUT = 1.2


def _ctx():
    # 这个本地接口用的是自签名证书，跳过校验（只对 127.0.0.1 用）
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _slug(name):
    """把 'game_character_displayname_Nocturne' 或 'Nocturne' 转成 'nocturne'"""
    if not name:
        return ""
    tail = str(name).split("_")[-1]
    return "".join(ch for ch in tail.lower() if ch.isalnum())


def get_live():
    """返回 {ok, ...}。对局没开或接口不可用时 ok=False，不抛异常。"""
    try:
        req = urllib.request.Request(LIVE_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx()) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return {"ok": False, "reason": "no_game"}

    try:
        active = data.get("activePlayer") or {}
        game = data.get("gameData") or {}
        players = data.get("allPlayers") or []

        me = None
        for p in players:
            if p.get("riotId") and active.get("riotId") and p["riotId"] == active["riotId"]:
                me = p
                break
            if p.get("summonerName") and active.get("summonerName") and \
               p["summonerName"] == active["summonerName"]:
                me = p
                break
        if me is None and players:
            me = players[0]

        my_team = (me or {}).get("team", "")

        out = []
        for p in players:
            champ = p.get("championName") or ""
            key = _slug(p.get("rawChampionName")) or _slug(champ)
            out.append({
                "champion": champ,
                "key": key,
                "team": p.get("team", ""),
                "level": p.get("level"),
                "dead": bool(p.get("isDead")),
                "items": len(p.get("items") or []),
                "me": p is me,
                # 队友/对手，用在我的视角上
                "mine": (p.get("team") == my_team) if my_team else False,
            })

        champ = (me or {}).get("championName") or active.get("championName") or ""
        return {
            "ok": True,
            "champion": champ,
            "championKey": _slug((me or {}).get("rawChampionName")) or _slug(champ),
            "level": (me or {}).get("level") or active.get("level"),
            "gameMode": game.get("gameMode", ""),
            "gameTime": round(float(game.get("gameTime") or 0), 1),
            "team": my_team,
            "players": out,
        }
    except Exception as e:
        return {"ok": False, "reason": "parse_error", "detail": str(e)}


def _key(champ):
    """保留给外部调用：英文英雄名 -> slug"""
    return _slug(champ)


if __name__ == "__main__":
    print(json.dumps(get_live(), ensure_ascii=False, indent=1))
