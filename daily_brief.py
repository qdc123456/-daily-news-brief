# -*- coding: utf-8 -*-
"""
每日新闻简报 · 云端最小可运行骨架
------------------------------------------------------------------
运行环境：Python 3.12 / 3.13，可直接跑在 GitHub Actions 或云函数上。
设计目标：无状态执行、幂等推送、失败可补发、不依赖本地开机。

【逐段导读 · 给编程初学者】
第 1 段：导入依赖，定义小工具（读配置、读写状态、发 HTTP）
第 2 段：采集 RSS，把每条新闻整理成统一字典
第 3 段：三层去重（网址 → 标题相似 → 已推记录）
第 4 段：分级（重大事件 / 与你相关 / 四个栏目），拼成一份简报
第 5 段：可选 AI 摘要，失败自动降级
第 6 段：多通道推送 + 指数退避重试 + 失败待补
第 7 段：main() 把上面串起来，并写回状态文件与心跳

重要：所有密钥只从环境变量读取，绝不写死在代码里。
"""

# ============================ 第 1 段：导入与基础工具 ============================

import os                      # 读取环境变量（云端 Secrets 会注入到这里）
import io                      # 把二进制响应转成 feedparser 能读的流
import re                      # 正则表达式，用于清洗标题
import json                    # 读写 state.json
import time                    # 重试时的休眠
import hashlib                 # 生成幂等键（指纹）
import difflib                 # 标准库自带的字符串相似度，用于标题去重
import calendar                # 把 RSS 的时间元组转成时间戳
import urllib.parse as up      # 解析网址、去掉跟踪参数
from datetime import datetime, timedelta, timezone

import requests                # 第三方：发 HTTP 请求
import feedparser              # 第三方：解析 RSS / Atom
import yaml                    # 第三方：读取 config.yaml

# 北京时间时区对象。云端 Runner 默认是 UTC，所有"今天/几点"都要用它换算。
# Windows 默认没有系统时区数据库，ZoneInfo 会抛错；中国不实行夏令时，
# 所以降级成固定的 UTC+8 完全等价。requirements.txt 里也带了 tzdata 作双保险。
try:
    from zoneinfo import ZoneInfo
    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    CN_TZ = timezone(timedelta(hours=8))

# 配置文件与状态文件的路径。os.path.dirname(__file__) 表示"本脚本所在目录"，
# 这样无论从哪个工作目录启动都不会找不到文件。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
STATE_PATH = os.path.join(BASE_DIR, "state.json")


def log(msg):
    """统一打印日志。云端会把它收进运行日志，方便排错。"""
    print(f"[daily-brief] {msg}", flush=True)


def load_config():
    """读取 config.yaml，返回字典。文件不存在时直接报错，避免带着空配置静默运行。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(ttl_days):
    """
    读取上一轮留下的状态。状态是这套系统"记忆"的唯一来源，
    因为云端每次运行都是一台全新的机器，内存里的东西会全部消失。
    结构：
      sent    = {新闻指纹: 推送日期}      ← 幂等去重
      pending = [上次推送失败的简报文本]  ← 失败补发
      daily   = {日期: 当天已推条数}      ← 频率控制
      health  = {源名称: 连续失败次数}    ← 源健康分
    """
    if not os.path.exists(STATE_PATH):
        return {"sent": {}, "pending": [], "daily": {}, "health": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        log("状态文件损坏，按空状态重新开始")
        return {"sent": {}, "pending": [], "daily": {}, "health": {}}

    # 清理过期的已推记录：只保留最近 ttl_days 天，防止 state.json 无限膨胀
    cutoff = (datetime.now(CN_TZ) - timedelta(days=ttl_days)).date().isoformat()
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if v >= cutoff}
    # 同样清理过期的日计数器
    state["daily"] = {k: v for k, v in state.get("daily", {}).items() if k >= cutoff}
    state.setdefault("pending", [])
    state.setdefault("health", {})
    state.setdefault("sent", {})
    state.setdefault("daily", {})
    return state


def save_state(state):
    """把状态写回文件。GitHub Actions 版随后会 git commit 并 push 回仓库。"""
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def http_get(url, timeout=8, retries=3):
    """
    带指数退避的 GET。只重试"值得重试"的错误：超时、429、5xx。
    4xx 参数错误之类立即放弃，避免无意义地刷接口。
    """
    headers = {"User-Agent": "DailyBriefBot/1.0 (+personal RSS reader)"}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            # 429 = 被限流；5xx = 对方服务临时故障。尊重 Retry-After 头
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
            else:
                log(f"GET {url} 返回 {r.status_code}，不重试")
                return None
        except requests.RequestException as e:
            log(f"GET {url} 第 {attempt + 1} 次失败：{type(e).__name__}")
            wait = 2 ** attempt
        time.sleep(min(wait, 8) + (attempt * 0.3))  # 加一点抖动，避免整点齐发
    return None


def http_post(url, payload, timeout=10, retries=3, headers=None):
    """带指数退避的 POST，用于推送消息与换取 token。返回 True/False。"""
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers or {}, timeout=timeout)
            if r.status_code < 300:
                # 很多通道 HTTP 200 但业务失败：企业微信用 errcode，Server酱用 code
                try:
                    data = r.json()
                    code = data.get("errcode", data.get("code", 0))
                    if isinstance(code, int) and code not in (0, 200):
                        log(f"通道业务错误：{r.text[:160]}")
                        return False
                except (ValueError, AttributeError):
                    pass
                return True
            log(f"POST 返回 {r.status_code}：{r.text[:160]}")
            if r.status_code != 429 and r.status_code < 500:
                return False            # 参数错 / 额度耗尽，重试也没用
        except requests.RequestException as e:
            log(f"POST 第 {attempt + 1} 次失败：{type(e).__name__}")
        time.sleep(min(2 ** attempt, 8))
    return False


def chunk_by_bytes(text, limit):
    """
    按行把长文本切成若干块，保证每块的 UTF-8 字节数不超过 limit。
    为什么必须按字节：企业微信 text 消息上限约 2048 字节，
    一个中文字符占 3 字节，用 len() 按字符截断会严重超限。
    """
    chunks, buf = [], ""
    for raw in text.splitlines(keepends=True):
        line = raw
        if len(line.encode("utf-8")) > limit:          # 单行超长（例如很长的网址）
            line = line.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
        if len((buf + line).encode("utf-8")) > limit and buf:
            chunks.append(buf)
            buf = ""
        buf += line
    if buf:
        chunks.append(buf)
    return chunks or [text[:200]]


# ============================ 第 2 段：采集与规范化 ============================

TRACK_PARAMS = ("utm_", "fbclid", "gclid", "spm", "from", "share_token", "ref")


def normalize_url(url):
    """
    网址规范化：去掉广告跟踪参数与锚点。
    同一篇新闻常有多个不同 URL（带 utm_source、带 # 锚点），
    不清理的话会被当成多条新闻重复推送。
    """
    try:
        p = up.urlsplit(url.strip())
        kept = [(k, v) for k, v in up.parse_qsl(p.query)
                if not any(k.lower().startswith(t) for t in TRACK_PARAMS)]
        path = p.path.rstrip("/") or "/"
        return up.urlunsplit((p.scheme.lower(), p.netloc.lower(), path,
                              up.urlencode(kept), ""))
    except Exception:
        return url.strip()


def title_key(title):
    """标题归一化：去标点、空白与常见媒体后缀，只留可比对的骨架。"""
    t = re.sub(r"[\s\W_]+", "", title, flags=re.UNICODE)
    t = re.sub(r"(独家|快讯|突发|最新|视频|图集|转载|原创)$", "", t)
    return t.lower()


def fingerprint(item):
    """幂等键：由规范化网址生成短哈希。同一网址在任何一次运行中都得到同一个值。"""
    return hashlib.sha256(normalize_url(item["url"]).encode("utf-8")).hexdigest()[:16]


def fetch_feed(section, source, cfg, state):
    """
    抓取单个 RSS 源，返回标准化新闻列表。
    section 是栏目名（时政/财经/科技/AI），source 是 config.yaml 里的一条源配置。
    """
    name, url = source["name"], source["url"]
    resp = http_get(url, timeout=cfg.get("fetch_timeout", 8))
    if resp is None:
        # 源健康分：连续失败累加；达到阈值后 main() 会在简报里提示
        state["health"][name] = state["health"].get(name, 0) + 1
        log(f"[{section}] 源 {name} 抓取失败，连续失败 {state['health'][name]} 次")
        return []

    state["health"][name] = 0  # 成功即清零
    feed = feedparser.parse(io.BytesIO(resp.content))
    items, max_age_h = [], cfg.get("max_age_hours", 48)
    now_ts = time.time()

    for e in feed.entries[:cfg.get("max_entries_per_feed", 30)]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue

        # 时间校验：只保留近 max_age_h 小时的新闻，防止源站回填旧闻
        tup = e.get("published_parsed") or e.get("updated_parsed")
        if tup:
            ts = calendar.timegm(tup)
            if now_ts - ts > max_age_h * 3600 or ts > now_ts + 3600:
                continue

        items.append({
            "title": title,
            "url": normalize_url(link),
            "source": name,
            "section": section,
            "ts": ts if tup else now_ts,
        })

    log(f"[{section}] 源 {name} 取得 {len(items)} 条")
    return items


# ============================ 第 3 段：三层去重 ============================

def dedupe(items, state, cfg):
    """
    三层去重的第二、三层：
      第一层已在 normalize_url 完成（网址级）
      第二层：标题相似度，把不同媒体报同一件事合并成一条
      第三层：查 state['sent']，已推过的指纹直接丢弃
    """
    out, seen_url, seen_title = [], set(), []
    sim_threshold = cfg.get("title_similarity", 0.84)

    for it in items:
        url, fp = it["url"], fingerprint(it)
        if url in seen_url or fp in state["sent"]:
            continue
        key = title_key(it["title"])
        if len(key) < 6:
            continue
        # difflib 是标准库，几十条数据做 O(n^2) 比对完全够用，不必引入额外依赖
        if any(difflib.SequenceMatcher(None, key, s).ratio() > sim_threshold
               for s in seen_title):
            continue
        seen_url.add(url)
        seen_title.append(key)
        out.append(it)
    return out


# ============================ 第 4 段：分级与排版 ============================

def hits(text, words):
    """判断标题是否命中任一词；支持 '+必须词' 与 '!排除词' 两种简单语法。"""
    if any(w.startswith("!") and w[1:] in text for w in words):
        return False
    plain = [w for w in words if not w.startswith("!") and not w.startswith("+")]
    must = [w[1:] for w in words if w.startswith("+")]
    return (not plain or any(w in text for w in plain)) and all(w in text for w in must)


def classify(item, cfg):
    """给每条新闻打标签：重大 / 与你相关 / 普通。"""
    t = item["title"]
    if hits(t, cfg.get("major_keywords", [])):
        return "重大"
    if hits(t, cfg.get("personal_keywords", [])):
        return "与你相关"
    return "普通"


def build_brief(groups, cfg, state, pending_note=""):
    """
    拼一份简报。刻意做成"一条消息装下全部内容"，
    这是频率控制最有效的手段：一天只打扰一次。
    """
    today = datetime.now(CN_TZ).strftime("%Y年%m月%d日")
    lines = [f"每日简报　{today}", ""]

    if pending_note:
        lines += ["【补发】以下含上一次未送达的内容", pending_note, ""]

    # 重大事件放最前
    if groups["重大"]:
        lines += ["【重大事件】"]
        lines += [f"● {i['title']}　（{i['source']}）\n  {i['url']}"
                  for i in groups["重大"][:cfg.get("max_major", 3)]]
        lines += [""]

    for sec in cfg.get("sections", []):
        picked = groups[sec][:cfg.get("max_per_section", 4)]
        if not picked:
            continue
        lines += [f"【{sec}】"]
        lines += [f"● {i['title']}　（{i['source']}）\n  {i['url']}" for i in picked]
        lines += [""]

    if groups["与你相关"]:
        lines += ["【与你相关】"]
        lines += [f"○ {i['title']}　（{i['source']}）\n  {i['url']}"
                  for i in groups["与你相关"][:cfg.get("max_personal", 3)]]
        lines += [""]

    # 源健康提示：让"某源悄悄挂了"变成可见信息，而不是莫名少了几条新闻
    broken = [k for k, v in state.get("health", {}).items()
              if v >= cfg.get("health_fail_threshold", 3)]
    if broken:
        lines += [f"【源状态】暂不可用：{'、'.join(broken)}", ""]

    lines += ["摘要由程序聚合，事实以原文链接为准。"]
    return "\n".join(lines)


# ============================ 第 5 段：可选 AI 摘要 ============================

def ai_polish(content, cfg):
    """
    可选：让模型把简报压成三句"今天最值得知道的事"。
    任何异常都降级为原文，绝不因为 AI 失败导致当天没有推送。
    """
    if not cfg.get("ai_summary", False) or not os.environ.get("DEEPSEEK_API_KEY"):
        return ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                        base_url="https://api.deepseek.com", timeout=30)
        resp = client.chat.completions.create(
            model=cfg.get("ai_model", "deepseek-chat"),
            messages=[
                {"role": "system", "content": "只做归纳，不编造事实，不添加原文没有的信息，输出三句中文。"},
                {"role": "user", "content": content[:6000]},
            ],
        )
        return "【三句话速览】\n" + resp.choices[0].message.content.strip() + "\n"
    except Exception as e:
        log(f"AI 摘要失败，降级为原文：{type(e).__name__}")
        return ""


# ============================ 第 6 段：多通道推送 ============================

def push_wework(text, cfg):
    """
    通道一：企业微信群机器人。msgtype=text 兼容性最好，可直达个人微信。
    简报较长时会按字节切成两三条连续送达，仍远低于每分钟二十条的配额。
    """
    hook = os.environ.get("WEWORK_WEBHOOK_URL")
    if not hook:
        return False
    msg_type = os.environ.get("WEWORK_MSG_TYPE", "text")
    # markdown 约 4096 字节上限，text 约 2048 字节上限，各留一点余量
    limit = 3900 if msg_type == "markdown" else 1900
    parts = chunk_by_bytes(text, limit)

    all_ok = True
    for idx, part in enumerate(parts, 1):
        if msg_type == "markdown":
            payload = {"msgtype": "markdown", "markdown": {"content": part}}
        else:
            payload = {"msgtype": "text", "text": {"content": part}}
        if not http_post(hook, payload):
            all_ok = False
        if idx < len(parts):
            time.sleep(0.5)     # 轻微限速，避免同一秒连发触到频控
    log(f"企业微信推送{'成功' if all_ok else '失败'}，共 {len(parts)} 段")
    return all_ok


def push_serverchan(text):
    """
    Server酱：本次部署的主通道。微信扫码拿 SendKey，一次 HTTP 请求一条消息。
    desp 支持 Markdown 且上限远大于企业微信，整份简报可一次送达，不用分段。
    免费版每日五条，所以频率控制必须保持在每天一到二条。
    """
    key = os.environ.get("SERVERCHAN_SENDKEY")
    if not key:
        return False
    today = datetime.now(CN_TZ).strftime("%m月%d日")
    ok = http_post(f"https://sctapi.ftqq.com/{key}.send",
                   {"title": f"每日简报　{today}", "desp": text})
    log(f"Server酱推送{'成功' if ok else '失败'}")
    return ok


def push_qq(text):
    """
    通道三：QQ 官方机器人群聊主动消息（REST 直连，不用已停更的 botpy SDK）。
    前置条件：机器人已上线、群聊权限已开、群内开启"机器人主动发言"、
    QQ_GROUP_OPENID 已从事件中一次性取得并存入 Secrets。
    """
    app_id = os.environ.get("QQ_APP_ID")
    secret = os.environ.get("QQ_APP_SECRET")
    openid = os.environ.get("QQ_GROUP_OPENID")
    if not all([app_id, secret, openid]):
        return False
    try:
        r = requests.post("https://bots.qq.com/app/getAppAccessToken",
                          json={"appId": app_id, "clientSecret": secret}, timeout=10)
        token = r.json().get("access_token")
        if not token:
            log(f"QQ token 获取失败：{r.text[:160]}")
            return False
        # 官方建议 access_token 缓存约 7200 秒；个人每日一次可不做缓存
        # QQ 文本消息同样按字节分块，保守取 1000 字节
        ok = True
        parts = chunk_by_bytes(text, 1000)[:4]   # 最多四段，避免刷屏
        for part in parts:
            if not http_post(
                f"https://api.bot.qq.com/v2/groups/{openid}/messages",
                {"msg_type": 0, "content": part},
                headers={"Authorization": f"QQBot {token}"},
            ):
                ok = False
            time.sleep(0.5)
        log(f"QQ 推送{'成功' if ok else '失败'}，共 {len(parts)} 段")
        return ok
    except requests.RequestException as e:
        log(f"QQ 推送异常：{type(e).__name__}")
        return False


def send_with_failover(text, cfg):
    """
    推送策略（本次部署：primary_channel = serverchan）：
      QQ 为"同时送达"的副通道，失败不影响主流程；
      主通道成功即算送达，只有主通道失败才降级到备用通道。
    只要有一个通道成功就返回 True，避免同一份简报被重复补发。
    """
    if cfg.get("qq_enabled", False):
        try:
            push_qq(text)
        except Exception as e:
            log(f"QQ 副通道异常（不影响主流程）：{type(e).__name__}")

    primary = cfg.get("primary_channel", "serverchan")
    chain = ([push_serverchan, lambda t: push_wework(t, cfg)] if primary == "serverchan"
             else [lambda t: push_wework(t, cfg), push_serverchan])
    for fn in chain:
        try:
            if fn(text):
                return True
            log("当前通道失败，尝试下一个通道")
        except Exception as e:
            log(f"通道异常：{type(e).__name__}")

    return False


# ============================ 第 7 段：主流程 ============================

def main():
    cfg = load_config()
    ttl = cfg.get("dedupe_ttl_days", 7)
    state = load_state(ttl)
    today = datetime.now(CN_TZ).date().isoformat()
    hour = datetime.now(CN_TZ).hour

    # ---- 采集 ----
    all_items = []
    for section in cfg.get("sections", []):
        for source in cfg.get("feeds", {}).get(section, []):
            all_items += fetch_feed(section, source, cfg, state)

    # ---- 去重 ----
    fresh = dedupe(all_items, state, cfg)
    log(f"共采集 {len(all_items)} 条，去重后 {len(fresh)} 条")

    # ---- 分级 ----
    groups = {k: [] for k in cfg.get("sections", []) + ["重大", "与你相关"]}
    for it in fresh:
        level = classify(it, cfg)
        # 命中重大或个人关键词的放进专属板块，其余按栏目归类
        bucket = it["section"] if level == "普通" else level
        groups.setdefault(bucket, []).append(it)
    for k in groups:
        groups[k].sort(key=lambda x: x["ts"], reverse=True)

    # ---- 取上一次失败待补的内容 ----
    pending = state.get("pending", [])
    pending_note = pending.pop(0) if pending else ""

    # ---- 内容为空时不打扰：只在确实有新闻时才发 ----
    if not fresh and not pending_note:
        log("无新增内容，跳过推送")
        save_state(state)
        return

    content = build_brief(groups, cfg, state, pending_note)
    summary = ai_polish(content, cfg)
    if summary:
        content = summary + "\n" + content

    # ---- 频率控制：日上限 + 静默时段 ----
    count = state["daily"].get(today, 0)
    quiet = cfg.get("quiet_hours", [23, 7])
    in_quiet = hour >= quiet[0] or hour < quiet[1]
    if count >= cfg.get("max_push_per_day", 2) or in_quiet:
        log(f"触发频率控制（今日已推 {count} 条，静默={in_quiet}），内容转入待补队列")
        state["pending"].append(content)
        save_state(state)
        return

    # ---- 推送 ----
    ok = send_with_failover(content, cfg)
    if ok:
        # 推送成功才标记已推。若推送失败被误标，会永久丢消息。
        for it in fresh:
            state["sent"][fingerprint(it)] = today
        state["daily"][today] = count + 1
        log("推送成功，已更新幂等记录")
        beat = os.environ.get("HEARTBEAT_URL")
        if beat:
            try:
                requests.get(beat, timeout=8)   # 心跳：只有成功才 ping
            except requests.RequestException:
                pass
    else:
        state["pending"].append(content)        # 全通道失败 → 下次补发
        log("全通道失败，内容已入待补队列")

    save_state(state)
    log("本轮结束")
    if not ok:
        # 让工作流显示失败，GitHub 会自动发邮件告警。
        # 状态文件已保存，失败内容进了待补队列，下一次运行会自动补发。
        raise SystemExit(1)


if __name__ == "__main__":
    main()
