#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Stars 知识库同步脚本 (JSON + Template 版)
功能：
  1. 从 GitHub API 抓取用户 Star 的项目列表
  2. 增量获取 README 并调用 AI 生成摘要，存储至 JSON 数据集
  3. 使用 Jinja2 模板将 JSON 数据渲染为 Markdown
  4. 支持推送到 Obsidian Vault 仓库
"""

import os
import sys
import json
import time
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml
from openai import OpenAI
from jinja2 import Environment, FileSystemLoader

# ── 日志配置 ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.parent  # 仓库根目录
CONFIG_PATH = SCRIPT_DIR / "config.yml"
DATA_DIR = SCRIPT_DIR / "data"
STARS_JSON_PATH = DATA_DIR / "stars.json"
TEMPLATES_DIR = SCRIPT_DIR / "templates"
DEFAULT_MD_TEMPLATE = "stars.md.j2"
STARS_MD_PATH_DEFAULT = SCRIPT_DIR / "stars.md"

# 确保数据目录存在
DATA_DIR.mkdir(exist_ok=True)


# ════════════════════════════════════════════════════════════
# 配置加载
# ════════════════════════════════════════════════════════════


def load_config() -> dict:
    """加载 config.yml，并用环境变量覆盖敏感字段"""
    if not CONFIG_PATH.exists():
        log.error(f"配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 环境变量优先覆盖配置文件中的值
    if os.environ.get("GH_USERNAME"):
        cfg["github"]["username"] = os.environ["GH_USERNAME"]

    if os.environ.get("AI_BASE_URL"):
        cfg["ai"]["base_url"] = os.environ["AI_BASE_URL"]
    if os.environ.get("AI_API_KEY"):
        cfg["ai"]["api_key"] = os.environ["AI_API_KEY"]
    if os.environ.get("AI_MODEL"):
        cfg["ai"]["model"] = os.environ["AI_MODEL"]

    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        cfg["github"]["token"] = os.environ.get("GH_TOKEN") or os.environ.get(
            "GITHUB_TOKEN"
        )
    else:
        cfg["github"]["token"] = None

    vault = cfg.get("vault_sync", {})
    if os.environ.get("VAULT_SYNC_ENABLED", "").lower() == "true":
        vault["enabled"] = True
    if os.environ.get("VAULT_REPO"):
        vault["repo"] = os.environ["VAULT_REPO"]
    if os.environ.get("VAULT_FILE_PATH"):
        vault["file_path"] = os.environ["VAULT_FILE_PATH"]
    if os.environ.get("VAULT_PAT"):
        vault["pat"] = os.environ["VAULT_PAT"]
    cfg["vault_sync"] = vault

    # 测试限制（可选）
    test_limit = os.environ.get("TEST_LIMIT", "")
    cfg["test_limit"] = int(test_limit) if test_limit.isdigit() else None

    return cfg


# ════════════════════════════════════════════════════════════
# 数据存储
# ════════════════════════════════════════════════════════════


class DataStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"last_updated": "", "repos": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"加载数据文件失败: {e}")
            return {"last_updated": "", "repos": {}}

    def save(self):
        self.data["last_updated"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def update_repo(self, full_name: str, metadata: dict, summary: dict):
        self.data["repos"][full_name] = {
            "metadata": metadata,
            "summary": summary,
            "pushed_at": metadata.get("updated_at", ""),
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

    def get_repo(self, full_name: str) -> Optional[dict]:
        return self.data["repos"].get(full_name)


# ════════════════════════════════════════════════════════════
# GitHub API 客户端
# ════════════════════════════════════════════════════════════


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, username: str, token: Optional[str] = None):
        self.username = username
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, url: str, params: dict = None) -> requests.Response:
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 403 and "rate limit" in resp.text.lower():
                    reset_time = int(
                        resp.headers.get("X-RateLimit-Reset", time.time() + 60)
                    )
                    wait = max(reset_time - int(time.time()), 5)
                    log.warning(f"API 限速，等待 {wait} 秒...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                log.warning(f"请求失败（第 {attempt + 1} 次）: {e}")
                time.sleep(2**attempt)
        raise Exception("多次请求失败")

    def get_starred_repos(self) -> list[dict]:
        repos = []
        page = 1
        log.info(f"正在抓取 @{self.username} 的 Stars...")
        while True:
            url = f"{self.BASE_URL}/users/{self.username}/starred"
            resp = self._get(
                url,
                params={
                    "per_page": 100,
                    "page": page,
                    "sort": "created",
                    "direction": "desc",
                },
            )
            data = resp.json()
            if not data:
                break
            for item in data:
                repos.append(
                    {
                        "full_name": item["full_name"],
                        "name": item["name"],
                        "owner": item["owner"]["login"],
                        "description": item.get("description") or "",
                        "stars": item["stargazers_count"],
                        "language": item.get("language") or "N/A",
                        "url": item["html_url"],
                        "homepage": item.get("homepage") or "",
                        "topics": item.get("topics", []),
                        "updated_at": item.get("pushed_at", "")[:10],
                    }
                )
            log.info(f"  第 {page} 页：获取 {len(data)} 个，共 {len(repos)} 个")
            if "next" not in resp.headers.get("Link", ""):
                break
            page += 1
        return repos

    def get_readme(self, full_name: str, max_length: int) -> str:
        url = f"{self.BASE_URL}/repos/{full_name}/readme"
        try:
            resp = self._get(url)
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
            return content[:max_length]
        except Exception:
            return ""

    def push_file(self, repo: str, path: str, content: str, msg: str, pat: str) -> bool:
        url = f"{self.BASE_URL}/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        }
        sha = None
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                sha = r.json().get("sha")
        except Exception:
            pass
        payload = {
            "message": msg,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        try:
            r = requests.put(url, headers=headers, json=payload, timeout=30)
            r.raise_for_status()
            log.info(f"✅ 已推送至: {repo}/{path}")
            return True
        except Exception as e:
            log.error(f"❌ 推送失败: {e}")
            return False


# ════════════════════════════════════════════════════════════
# AI 摘要生成
# ════════════════════════════════════════════════════════════


class AISummarizer:
    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: int = 60, retry: int = 3
    ):
        self.model = model
        self.retry = retry
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    def summarize(self, repo_name: str, description: str, readme: str) -> dict:
        context = f"Repo: {repo_name}\nDesc: {description}\n\nREADME:\n{readme}"
        prompt = """你是一个技术文档分析专家。请根据 GitHub 仓库信息生成：
1. 专业的**中文摘要**（100字以内），描述核心功能、场景和亮点
2. **关键词标签**（5-8个）

输出 JSON 格式：
{
  "zh": "摘要内容",
  "tags": ["tag1", "tag2"]
}"""
        for attempt in range(self.retry):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": context},
                    ],
                    temperature=0.3,
                    response_format={"type": "json_object"},
                )
                return json.loads(resp.choices[0].message.content)
            except Exception as e:
                if attempt == self.retry - 1:
                    log.error(f"AI 生成失败 [{repo_name}]: {e}")
                    return {"zh": "生成失败", "tags": []}
                log.warning(f"AI 重试 {attempt + 1}...")
                time.sleep(2**attempt)


# ════════════════════════════════════════════════════════════
# 模版生成器
# ════════════════════════════════════════════════════════════


class TemplateGenerator:
    def __init__(self, template_dir: Path):
        self.env = Environment(loader=FileSystemLoader(str(template_dir)))

    def render(self, template_name: str, context: dict) -> str:
        template = self.env.get_template(template_name)
        return template.render(context)


# ════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════


def main():
    log.info("GitHub Stars 知识库同步系统开始运行")
    cfg = load_config()

    gh = GitHubClient(cfg["github"]["username"], cfg["github"].get("token"))
    ai = AISummarizer(
        cfg["ai"]["base_url"],
        cfg["ai"]["api_key"],
        cfg["ai"]["model"],
        cfg["ai"].get("timeout", 60),
        cfg["ai"].get("max_retries", 3),
    )
    store = DataStore(STARS_JSON_PATH)
    generator = TemplateGenerator(TEMPLATES_DIR)

    # 1. 抓取所有 Stars
    all_repos = gh.get_starred_repos()

    # 2. 增量处理
    new_count = 0
    for i, repo in enumerate(all_repos, 1):
        full_name = repo["full_name"]
        existing = store.get_repo(full_name)

        # 如果已存在且 stars 差别不大（或者你想要定期更新也可以在此加入逻辑）
        # 这里演示增量更新：如果 JSON 里没有，则处理
        if not existing:
            # 检查测试限制
            test_limit = cfg.get("test_limit")
            if test_limit is not None and new_count >= test_limit:
                log.info(f"⚠️ 已达到测试限制数量 ({test_limit})，停止处理新项目")
                break

            log.info(f"[{i}/{len(all_repos)}] 正在处理新仓库: {full_name}")
            readme = gh.get_readme(full_name, cfg["ai"].get("max_readme_length", 4000))
            if not readme and not repo["description"]:
                summary = {"zh": "暂无描述。", "tags": []}
            else:
                summary = ai.summarize(full_name, repo["description"], readme)

            store.update_repo(full_name, repo, summary)
            new_count += 1
            time.sleep(1)  # 频率限制
        else:
            # 更新元数据信息（Stars 数等）但保留旧摘要
            existing["metadata"] = repo
            # 可以根据需要判断是否由于 stars 增加很多或时间太久而重新生成摘要

    if new_count > 0:
        store.save()
        log.info(f"✅ 数据保存完成，新增 {new_count} 条记录")
    else:
        log.info("✨ 没有新条目需要处理")

    # 3. 按 Star 时间重新排序（最新 Star 在前）
    # JSON 里的 repos 是无序的，我们按照 all_repos 的顺序来生成（它是倒序的）
    ordered_repos = []
    for r_meta in all_repos:
        entry = store.get_repo(r_meta["full_name"])
        if entry:
            # 合并展示需要的数据
            view_data = {**entry["metadata"], "summary": entry["summary"]}
            ordered_repos.append(view_data)

    # 4. 渲染 Markdown
    context = {
        "last_updated": store.data["last_updated"],
        "repos": ordered_repos,
    }

    output_md_path = SCRIPT_DIR / cfg["output"].get("file_path", "stars.md")
    md_content = generator.render(DEFAULT_MD_TEMPLATE, context)
    output_md_path.write_text(md_content, encoding="utf-8")
    log.info(f"✅ Markdown 生成完成: {output_md_path}")

    # 5. 可选：Vault 同步
    v_cfg = cfg.get("vault_sync", {})
    if v_cfg.get("enabled"):
        gh.push_file(
            v_cfg["repo"],
            v_cfg.get("file_path", "stars.md"),
            md_content,
            v_cfg.get("commit_message", "automated update"),
            v_cfg["pat"],
        )

    log.info("同步任务结束")


if __name__ == "__main__":
    main()
