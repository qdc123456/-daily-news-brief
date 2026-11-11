# -*- coding: utf-8 -*-
"""
一键部署脚本（保底路径，零第三方依赖，用 Python 标准库即可运行）

用途：当 WorkBuddy 的 GitHub 连接没有"创建仓库"权限时，用你自己的
      GitHub Personal Access Token 一次性完成「建仓库 + 上传全部文件」。

用法：
  一、去 GitHub 生成令牌
      Settings → Developer settings → Personal access tokens
      → Fine-grained tokens → Generate new token
      → Repository access 选 All repositories
      → Permissions 勾 Repository administration: Read and write
        与 Contents: Read and write
      → 生成后立即复制（只显示一次）
  二、设置环境变量（不要写进任何文件、不要发给别人）
      Git Bash：  export GITHUB_PAT="粘贴你的令牌"
      PowerShell：$env:GITHUB_PAT="粘贴你的令牌"
  三、在本目录执行
      python deploy_to_github.py

成功后会打印仓库地址。之后只需在网页填一个 Secret
（SERVERCHAN_SENDKEY）并手动 Run workflow 一次，详见 README.txt。

安全说明：本脚本只在本地读取令牌并用 HTTPS 发给 api.github.com，
不打印、不保存、不写入任何文件；用完请删除该令牌。
"""

import os
import sys
import json
import base64
import urllib.request
import urllib.error

REPO_NAME = "-daily-news-brief"   # 实际仓库名带一个前导连字符，保持一致避免重复建仓库
REPO_DESC = "每日新闻简报：GitHub Actions 定时聚合时政、财经、科技、AI 新闻并推送到微信"
IS_PRIVATE = False          # 公开仓库定时最稳、Runner 免费
SKIP_DIRS = {".git", "__pycache__", ".venv"}
SKIP_FILES = {".env", "deploy_to_github.py"}   # 令牌脚本本身不必上传
API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_PAT", "").strip()


def api(method, path, data=None):
    """调用 GitHub REST API。返回解析后的 JSON；令牌只放在请求头里。"""
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        API + path, data=body, method=method,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "daily-brief-deployer",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"{method} {path} 返回 {e.code}：{detail}") from None


def collect_files(root):
    """递归收集本目录下所有要上传的文件，返回 (相对路径, 绝对路径) 列表。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES or fn.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append((rel, full))
    return sorted(out)


def main():
    if not TOKEN:
        print("未检测到 GITHUB_PAT。请先按文件顶部说明生成令牌并设置环境变量。")
        return 1

    root = os.path.dirname(os.path.abspath(__file__))
    me = api("GET", "/user")
    owner = me["login"]
    print(f"已认证账号：{owner}")

    # 第一步：创建仓库。auto_init 会生成默认分支与 README.md，便于后续上传
    try:
        repo = api("POST", "/user/repos", {
            "name": REPO_NAME,
            "description": REPO_DESC,
            "private": IS_PRIVATE,
            "auto_init": True,
        })
        print(f"仓库已创建：{repo['html_url']}")
    except RuntimeError as e:
        if "返回 422" in str(e):
            print("仓库已存在，直接上传文件")
        else:
            raise

    info = api("GET", f"/repos/{owner}/{REPO_NAME}")
    branch = info["default_branch"]
    print(f"默认分支：{branch}")

    # 第二步：逐个上传文件。已存在的文件要先取 sha 才能覆盖
    files = collect_files(root)
    for rel, full in files:
        with open(full, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        payload = {
            "message": "初始化每日新闻简报项目",
            "content": encoded,
            "branch": branch,
        }
        try:
            cur = api("GET", f"/repos/{owner}/{REPO_NAME}/contents/{rel}?ref={branch}")
            payload["sha"] = cur["sha"]
        except RuntimeError as e:
            if "返回 404" not in str(e):
                raise
        api("PUT", f"/repos/{owner}/{REPO_NAME}/contents/{rel}", payload)
        print(f"  已上传 {rel}")

    print(f"\n部署完成：https://github.com/{owner}/{REPO_NAME}")
    print(f"共上传 {len(files)} 个文件。")
    print("剩余两步（必须本人操作）：")
    print("  一、Settings → Secrets and variables → Actions → 新建 SERVERCHAN_SENDKEY")
    print("  二、Actions 页手动 Run workflow 一次验证")
    print("提醒：用完请在 GitHub 撤销本次生成的令牌。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
