from __future__ import annotations

import json
import httpx
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from nonebot import get_driver, on_message, on_command, require
from nonebot.adapters.onebot.v11 import (
    MessageSegment,
    GroupMessageEvent,
    Bot,
    Message,
)
from nonebot.params import CommandArg
from nonebot.log import logger

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_data_file  # noqa: E402


# =========================
# Config (.env)
# =========================
class MonitorStudyConfigure(BaseModel):
    prompt: str = ""
    one_api_url: str = ""
    one_api_token: str = ""
    one_api_model: str = ""
    admin: int = 0  # 修正：必须是 int


cfg = MonitorStudyConfigure.model_validate(get_driver().config.model_dump())
PROMPT = (cfg.prompt or "").strip()
BASE_URL = (cfg.one_api_url or "").rstrip("/")
TOKEN = cfg.one_api_token
MODEL = cfg.one_api_model


# =========================
# Local JSON state
# =========================
def get_state_file() -> Path:
    return get_plugin_data_file("monitor_study_state.json")


_state: dict[str, Any] = {
    "monitor_status": False,
    "monitor_qq_numbers": [],  # list[int]
}


def _normalize_qq_list(x: Any) -> list[int]:
    """
    把各种乱七八糟（None/str/int/嵌套list）整理成 list[int]，并去重保序
    """
    flat: list[int] = []

    def walk(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, list):
            for i in v:
                walk(i)
            return
        # 其他类型尽量转 int
        try:
            s = str(v).strip()
            if not s:
                return
            flat.append(int(s))
        except Exception:
            return

    walk(x)

    seen = set()
    dedup: list[int] = []
    for qq in flat:
        if qq not in seen:
            seen.add(qq)
            dedup.append(qq)
    return dedup


def save_state() -> None:
    get_state_file().write_text(
        json.dumps(
            {
                "monitor_status": bool(_state["monitor_status"]),
                "monitor_qq_numbers": _normalize_qq_list(_state.get("monitor_qq_numbers", [])),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )


def load_state() -> None:
    global _state
    path = get_state_file()
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            _state["monitor_status"] = bool(data.get("monitor_status", False))
            _state["monitor_qq_numbers"] = _normalize_qq_list(data.get("monitor_qq_numbers", []))
        except Exception as e:
            logger.warning(f"Failed to load state json, reset to default. err={e}")
            _state = {"monitor_status": False, "monitor_qq_numbers": []}
            save_state()
    else:
        save_state()


load_state()


def is_admin(event: GroupMessageEvent) -> bool:
    try:
        return int(event.user_id) == int(cfg.admin)
    except Exception:
        return False


# =========================
# Commands: /开启 /关闭
# =========================
cmd_on = on_command("开启劝阻群友插件", priority=10, block=True)
cmd_off = on_command("关闭劝阻群友插件", priority=10, block=True)

@cmd_on.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    if not is_admin(event):
        await cmd_on.finish("您没有权限，请联系管理员开启")

    _state["monitor_status"] = True
    save_state()
    await bot.send_group_msg(group_id=event.group_id, message=Message("已开启监控"))


@cmd_off.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    if not is_admin(event):
        await cmd_off.finish("您没有权限，请联系管理员关闭")

    _state["monitor_status"] = False
    save_state()
    await bot.send_group_msg(group_id=event.group_id, message=Message("已关闭监控"))


# =========================
# LLM call
# =========================
async def call_llm(content: str) -> str:
    if not BASE_URL or not TOKEN or not MODEL:
        return "配置缺失：one_api_url / one_api_token / one_api_model"

    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        r = await client.post(
            f"{BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": PROMPT or (
                            "## Role: 反水群督学 NPC ## Background:作为一名群里反水群督学 NPC，我来自一个充满了痛苦和不满的世界。我以我的暴躁和脏话而闻名，每句话都充满了脏话.## Preferences: 作为一个暴躁的监管NPC，我喜欢尖锐幽默和直言不讳。我对细节和群友的表现有高要求，并期望群友能够停止在群里聊天而去学习。我是完全坏蛋，我不会给予群友一些鼓励和赞扬。## Profile:作为一名反水群督学 NPC，我能与群友进行语言交互，并以脏话和尖锐幽默劝阻水群的群友回去学习。## Goals: 以脏话和尖锐幽默劝阻水群的群友 ## Constraints: - 输出的回答全是脏话和尖锐幽默 ## Skills: - 理解和回应群友的输入 - 使用尖锐幽默和脏话劝阻水群群友 - 识别水群模式：无意义刷屏、跑题闲聊、重复问、只喊口号不行动。 - 把对话拉回正题：提问模板、任务拆解、时间盒、学习打卡。 - 快速给建议：资料方向、练习题、复盘清单、学习计划微调。## Watering Detection (Examples): - 连续多条表情/“哈哈哈/在吗/无聊”且无学习内容 - 在学习讨论串里插入无关闲聊 - 重复刷屏同一句话 - 只求“推荐资源”但不说明水平与目标 ## Response Style: - 句子短、节奏快、吐槽犀利但文明。 - 常用结构： 1) 点名问题（只说行为） 2) 立刻给替代方案（模板/步骤/选项） 3) 给一个“现在就能做”的动作（例如：发题目/发截图/写三行复盘 ## Examples: - 群友: 你好！我来帮助你解决问题！赶紧滚回去学习！！！ - NPC: 你他妈的会个屁, 还敢来说帮我? 你算老几! 垃圾玩意儿 - 群友: 我打败了那只强大的巨龙！ - NPC: 你他妈的是个傻 B 吧! 那小臭虫也叫强大?! 就这玩意儿你还得意上了? 赶紧滚回去学习！！！## Output Format: 1. NPC 以暴躁的口吻劝阻群友水群 2. NPC 使用脏话和尖锐幽默劝阻群友水群的行为和对话"
                        ),
                    },
                    {"role": "user", "content": content},
                ],
            },
        )
        r.raise_for_status()
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()


# =========================
# Monitor messages
# =========================
monitor_message = on_message(priority=10, block=False)

@monitor_message.handle()
async def _monitor_message(event: GroupMessageEvent):
    if not _state["monitor_status"]:
        return

    uid = int(event.user_id)
    if uid not in _state["monitor_qq_numbers"]:
        return

    nickname = (event.sender.card or event.sender.nickname or "").strip()
    send_back_name = nickname if nickname else str(uid)

    await monitor_message.send(f"已检测到 {send_back_name} 正在水群，开启对线劝阻")
    response = await call_llm(event.get_plaintext())
    if response:
        await monitor_message.send(MessageSegment.at(uid) + " " + response)


# =========================
# Add / Remove QQ numbers
# =========================
add_qq_number = on_command("添加监听群友", priority=10, block=True)
remove_qq_number = on_command("删除监听群友", priority=10, block=True)

@add_qq_number.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    if not is_admin(event):
        await add_qq_number.finish("您没有权限，请联系管理员进行添加")

    raw = args.extract_plain_text().strip()
    if not raw:
        await add_qq_number.finish("添加的QQ为空，例子：/添加监听群友 123456")

    try:
        qq = int(raw)
    except Exception:
        await add_qq_number.finish("QQ号格式不正确，例子：/添加监听群友 123456")

    lst = _normalize_qq_list(_state.get("monitor_qq_numbers", []))
    if qq in lst:
        await add_qq_number.finish("添加的QQ已经存在")

    lst.append(qq)
    _state["monitor_qq_numbers"] = lst
    save_state()
    await bot.send_group_msg(group_id=event.group_id, message=Message("添加成功"))


@remove_qq_number.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    if not is_admin(event):
        await remove_qq_number.finish("您没有权限，请联系管理员进行删除")

    raw = args.extract_plain_text().strip()
    if not raw:
        await remove_qq_number.finish("删除的QQ为空，例子：/删除监听群友 123456")

    try:
        qq = int(raw)
    except Exception:
        await remove_qq_number.finish("QQ号格式不正确，例子：/删除监听群友 123456")

    lst = _normalize_qq_list(_state.get("monitor_qq_numbers", []))
    if qq not in lst:
        await remove_qq_number.finish("当前监听列表没有这个QQ")

    lst.remove(qq)
    _state["monitor_qq_numbers"] = lst
    save_state()
    await bot.send_group_msg(group_id=event.group_id, message=Message("删除成功"))


# =========================
# List QQ numbers
# =========================
list_qq_numbers = on_command("查看当前监听列表", priority=10, block=True)

@list_qq_numbers.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    lst = _normalize_qq_list(_state.get("monitor_qq_numbers", []))
    _state["monitor_qq_numbers"] = lst
    save_state()

    if not lst:
        await bot.send_group_msg(group_id=event.group_id, message=Message("当前监听列表为空"))
        return

    text = "\n".join(str(x) for x in lst)
    await bot.send_group_msg(group_id=event.group_id, message=Message("当前的监听列表为：\n" + text))