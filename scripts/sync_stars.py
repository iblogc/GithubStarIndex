#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Stars Index同步脚本 (JSON + Template 版)
功能：
  1. 从 GitHub API 抓取用户 Star 的项目列表
  2. 增量获取 README 并调用 AI 生成摘要，存储至 JSON 数据集
  3. 使用 Jinja2 模板将 JSON 数据渲染为 Markdown
  4. 支持推送到 Obsidian Vault 仓库
"""

import os
import sys
import json
import re
import time
import base64
import logging
import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import requests
import yaml
from openai import OpenAI
from jinja2 import Environment, FileSystemLoader
from dotenv import load_dotenv

# 加载本地 .env 文件
load_dotenv(override=True)

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
    """加载配置：环境变量优先于 config.yml"""
    # 核心映射：环境变量名 -> (配置路径, 默认值)
    # 配置路径使用点分隔，如 'ai.model'
    env_mapping = {
        "GH_USERNAME": "github.username",
        "GH_TOKEN": "github.token",
        "GITHUB_TOKEN": "github.token",
        "AI_BASE_URL": "ai.base_url",
        "AI_API_KEY": "ai.api_key",
        "AI_MODEL": "ai.model",
        "MAX_CONCURRENCY": "ai.concurrency",
        "OUTPUT_FILENAME": "output.filename",
        "VAULT_SYNC_ENABLED": "vault_sync.enabled",
        "VAULT_REPO": "vault_sync.repo",
        "VAULT_SYNC_PATH": "vault_sync.path",
        "VAULT_PAT": "vault_sync.pat",
        "PAGES_SYNC_ENABLED": "pages_sync.enabled",
        "TEST_LIMIT": "test_limit",
    }

    # 1. 默认基础结构
    cfg = {
        "github": {"username": os.environ.get("GH_USERNAME"), "token": None},
        "ai": {
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": None,
            "concurrency": 5,
        },
        "output": {"filename": "stars"},
        "vault_sync": {
            "enabled": False,
            "repo": None,
            "path": "GitHub-Stars/",
            "pat": None,
            "commit_message": "🤖 自动更新 GitHub Stars 摘要",
        },
        "pages_sync": {"enabled": False},
        "test_limit": None,
    }

    # 2. 从 config.yml 加载 (若存在)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_yml = yaml.safe_load(f) or {}
            # 这里简单处理两层嵌套
            for section in ["ai", "output", "vault_sync", "pages_sync"]:
                if section in user_yml and isinstance(user_yml[section], dict):
                    cfg[section].update(user_yml[section])

    # 3. 环境变量覆盖 (具有最高优先级)
    for env_key, config_path in env_mapping.items():
        val = os.environ.get(env_key)
        if val is not None:
            # 处理类型转换
            if env_key in ["MAX_CONCURRENCY", "TEST_LIMIT"]:
                if val.isdigit():
                    val = int(val)
                else:
                    continue
            elif env_key in ["VAULT_SYNC_ENABLED", "PAGES_SYNC_ENABLED"]:
                val = val.lower() == "true"

            # 更新到字典
            parts = config_path.split(".")
            target = cfg
            for p in parts[:-1]:
                target = target[p]
            target[parts[-1]] = val

    # 4. 必填项校验
    if not cfg["github"]["username"]:
        log.error("❌ 错误: 未配置 GitHub 用户名 (GH_USERNAME)")
        sys.exit(1)
    if not cfg["ai"]["api_key"]:
        log.error("❌ 错误: 未配置 AI API Key (AI_API_KEY)")
        sys.exit(1)

    return cfg


# ════════════════════════════════════════════════════════════
# 数据存储
# ════════════════════════════════════════════════════════════


class DataStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
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
        with self.lock:
            self.data["last_updated"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def update_repo(self, full_name: str, metadata: dict, summary: dict):
        with self.lock:
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


# ── 标签治理配置 ──────────────────────────────────────────
TAG_MAPPING = {
    # ── AI & 大模型 ──
    "LLM": "AI 大模型",
    "Large Language Model": "AI 大模型",
    "Agent": "AI 智能体",
    "Agents": "AI 智能体",
    "AI Agent": "AI 智能体",
    "Generative AI": "生成式 AI",
    "Prompt Engineering": "提示工程",
    "LangChain": "LangChain 框架",
    "RAG": "RAG 检索增强",
    "Stable Diffusion": "AI 图像生成",
    "Image Generation": "AI 图像生成",
    "Text-to-Image": "AI 图像生成",
    "Computer Vision": "计算机视觉",
    "NLP": "自然语言处理",
    "Vector Database": "向量数据库",
    "Fine-tuning": "模型微调",
    "Quantization": "模型量化",
    "Multi-modal": "多模态 AI",
    "Deep Learning": "深度学习",
    "Machine Learning": "机器学习",
    
    # ── 技术栈归一化 ──
    "JS": "JavaScript",
    "TS": "TypeScript",
    "Golang": "Go",
    "Rustlang": "Rust",
    "Vuejs": "Vue.js",
    "Reactjs": "React",
    "Nextjs": "Next.js",
    "Nuxtjs": "Nuxt.js",
    "SvelteKit": "Svelte",
    "TailwindCSS": "Tailwind CSS",
    "Tailwind": "Tailwind CSS",
    "Tauri": "Tauri 框架",
    "Flutter": "Flutter",
    "ReactNative": "React Native",
    "FastAPI": "FastAPI",
    "Django": "Django",
    "Flask": "Flask",
    "SpringBoot": "Spring Boot",
    "Postgres": "PostgreSQL",
    "Redis": "Redis",
    "MongoDB": "MongoDB",
    "SQLite": "SQLite",
    
    # ── 领域/场景 ──
    "Web Scraping": "网页爬虫",
    "Crawler": "网页爬虫",
    "Automation": "自动化工具",
    "DevOps": "运维自动化",
    "Cybersecurity": "网络安全",
    "Data Visualization": "数据可视化",
    "Knowledge Graph": "知识图谱",
    "Microservices": "微服务架构",
    "Docker": "容器化",
    "Kubernetes": "Kubernetes",
    "K8s": "Kubernetes",
    "Serverless": "无服务器",
    "Cloud Computing": "云计算",
    
    # ── 平台与应用类型 ──
    "Browser Extension": "浏览器插件",
    "Chrome Extension": "浏览器插件",
    "VS Code Extension": "VS Code 插件",
    "Desktop App": "桌面应用",
    "Mobile App": "移动端应用",
    "CLI": "命令行工具",
    "Terminal": "命令行工具",
    "API": "开发者接口",
    "SDK": "开发者 SDK",
    
    # ── 其它归口 ──
    "Awesome": "精选资源",
    "Tutorial": "技术教程",
    "Library": "开发库",
    "Framework": "核心框架",
    "Boilerplate": "项目模板",
    "Template": "项目模板",
    "Privacy": "隐私安全",
    "Self-hosted": "私有化部署",
}

# ════════════════════════════════════════════════════════════
# AI 摘要生成
# ════════════════════════════════════════════════════════════

class AISummarizer:
    THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)

    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: int = 60, retry: int = 3
    ):
        self.base_url = (base_url or "").lower()
        self.model = model
        self.retry = retry
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    def normalize_tags(self, tags: list[str]) -> list[str]:
        """标签归一化：去重、合并同义词、统一大小写"""
        normalized = set()
        for t in tags:
            # 1. 基础清洗
            t = t.strip()
            if not t: continue
            
            # 2. 查表合并
            # 尝试 原词、全小写、全大写 的匹配
            mapped = TAG_MAPPING.get(t) or TAG_MAPPING.get(t.upper()) or TAG_MAPPING.get(t.title())
            if mapped:
                normalized.add(mapped)
            else:
                # 3. 兜底处理：如果不在字典里，仅做基础美化
                normalized.add(t)
        
        return sorted(list(normalized))

    def _extract_json_payload(self, content: object) -> dict:
        if content is None:
            raise ValueError("empty content")

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            text = "\n".join(parts).strip()
        else:
            text = str(content).strip()

        if not text:
            raise ValueError("empty content")

        # MiniMax 兼容接口可能会把 CoT 放在 <think>...</think> 中，先剥离。
        text = self.THINK_BLOCK_RE.sub("", text).strip()

        # 兼容 ```json ... ``` 包裹场景。
        text = text.replace("```json", "```").strip()
        if text.startswith("```") and text.endswith("```"):
            text = text[3:-3].strip()

        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch not in "[{":
                continue
            try:
                payload, _ = decoder.raw_decode(text[idx:])
                if isinstance(payload, dict):
                    return payload
            except Exception:
                continue

        raise ValueError("no valid json object found in model content")

    def summarize(self, repo_name: str, description: str, readme: str) -> dict:
        context = f"Repo: {repo_name}\nDesc: {description}\n\nREADME:\n{readme}"
        prompt = """你是一个顶级技术布道师和架构师。请深入分析 GitHub 仓库信息并生成：
1. **中文摘要**（80-100字）：准确提炼核心价值、应用场景与技术亮点，避免空话。
2. **英文摘要**（80-100字）。
3. **高权重标签**（中英文各 2-4 个）：
   - **分层打标**：[1个领域大类] + [1-2个核心技术栈] + [1个核心用途]。
   - **优先选用标准词**：例如 AI智能体, AI大模型, 网页爬虫, 自动化工具, 提示工程, 运维自动化, 跨平台, 数据可视化, 知识图谱, 浏览器插件, 桌面应用。
   - **严控数量**：标签必须是极高权重的，禁止琐碎或重复（如不要同时打 "Python" 和 "Python脚本"）。
   - **语言规范**：中文标签尽量简洁有力，英文标签首字母大写。

输出 JSON 格式：
{
  "zh": "中文摘要",
  "en": "English summary",
  "tags_zh": ["应用领域", "核心技术", "主要特征"],
  "tags_en": ["Domain", "Tech Stack", "Key Feature"]
}"""
        for attempt in range(self.retry):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": context},
                    ],
                    "temperature": 0.3,
                }

                # 对标准 OpenAI 继续启用结构化返回；MiniMax 走文本容错解析。
                if "api.minimaxi.com" not in self.base_url:
                    kwargs["response_format"] = {"type": "json_object"}

                resp = self.client.chat.completions.create(**kwargs)
                data = self._extract_json_payload(resp.choices[0].message.content)
                # 兼容性处理
                if "tags" in data and "tags_zh" not in data:
                    data["tags_zh"] = data["tags"]
                
                # 标签归一化治理
                data["tags_zh"] = self.normalize_tags(data.get("tags_zh", []))
                data["tags_en"] = self.normalize_tags(data.get("tags_en", []))
                
                return data
            except Exception as e:
                if attempt == self.retry - 1:
                    log.error(f"AI 生成失败 [{repo_name}]: {e}")
                    return {
                        "zh": "生成失败",
                        "en": "Generation failed",
                        "tags_zh": [],
                        "tags_en": [],
                    }
                log.warning(f"AI 生成失败 [{repo_name}]，重试中 {attempt + 1}: {e}")
                time.sleep(2**attempt)


# ════════════════════════════════════════════════════════════
# 模版生成器
# ════════════════════════════════════════════════════════════


class TemplateGenerator:
    def __init__(self, template_dir: Path):
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # 添加简单的 JS 转义过滤器
        self.env.filters["escapejs"] = (
            lambda x: x.replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")
        )

    def render(self, template_name: str, context: dict) -> str:
        template = self.env.get_template(template_name)
        return template.render(context)


# ════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="GitHub Stars Index 同步与生成脚本")
    parser.add_argument(
        "--render-only", action="store_true", help="直接使用本地 json 数据重新生成展示文件，不进行 API 抓取"
    )
    args = parser.parse_args()

    log.info("GitHub Stars Index同步系统开始运行")
    cfg = load_config()

    store = DataStore(STARS_JSON_PATH)
    generator = TemplateGenerator(TEMPLATES_DIR)

    if args.render_only:
        log.info("🚀 运行模式: 仅渲染 (Render Only)")
        if not STARS_JSON_PATH.exists():
            log.error(f"❌ 错误: 未找到数据文件 {STARS_JSON_PATH}，无法进行渲染。")
            sys.exit(1)
        # 在仅渲染模式下，我们从 store 中获取所有已处理的项目
        # 注意：这里我们没有 gh 客户端抓取的实时 all_repos 顺序，
        # 所以我们按照 json 中存储的 pushed_at 倒序排列，模拟原有逻辑。
        all_repos_data = []
        for full_name, entry in store.data.get("repos", {}).items():
            repo_meta = entry.get("metadata", {})
            if not repo_meta:
                continue
            all_repos_data.append(repo_meta)

        # 按星标时间或推送时间排序（这里假设 metadata 中有足够信息，通常我们按推送时间排）
        all_repos = sorted(
            all_repos_data, key=lambda x: x.get("updated_at", ""), reverse=True
        )
    else:
        gh = GitHubClient(cfg["github"]["username"], cfg["github"].get("token"))
        ai = AISummarizer(
            cfg["ai"]["base_url"],
            cfg["ai"]["api_key"],
            cfg["ai"]["model"],
            cfg["ai"].get("timeout", 60),
            cfg["ai"].get("max_retries", 3),
        )

        # 1. 抓取所有 Stars
        all_repos = gh.get_starred_repos()

        # 2. 增量处理
        new_repos_to_process = []
        seen_full_names = set()  # 防止 API 返回重复数据
        test_limit = cfg.get("test_limit")

        for repo in all_repos:
            full_name = repo["full_name"]

            if full_name in seen_full_names:
                continue

            existing = store.get_repo(full_name)

            is_processed = False
            if existing:
                summ = existing.get("summary", {})
                if summ and summ.get("zh") and "生成失败" not in summ.get("zh"):
                    is_processed = True

            if not is_processed:
                if test_limit is not None and len(new_repos_to_process) >= test_limit:
                    continue
                new_repos_to_process.append(repo)
                seen_full_names.add(full_name)
            else:
                existing["metadata"] = repo
                seen_full_names.add(full_name)

        def process_repo(args_tuple):
            idx, repo_data = args_tuple
            fname = repo_data["full_name"]
            total = len(new_repos_to_process)

            log.info(f"[{idx}/{total}] 正在处理新仓库: {fname}")
            readme_content = gh.get_readme(
                fname, cfg["ai"].get("max_readme_length", 4000)
            )

            if not readme_content and not repo_data["description"]:
                summ = {"zh": "暂无描述。", "tags": []}
            else:
                summ = ai.summarize(fname, repo_data["description"], readme_content)

            store.update_repo(fname, repo_data, summ)
            return True

        new_count = len(new_repos_to_process)
        if new_count > 0:
            concurrency = cfg["ai"].get("concurrency", 5)
            log.info(f"🚀 开始并发处理 {new_count} 个新仓库 (并发数: {concurrency})")
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                list(executor.map(process_repo, enumerate(new_repos_to_process, 1)))

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
            # 确保 summary 格式正确，防止旧数据或空数据导致模版崩溃
            summary = entry.get("summary") or {}
            if not isinstance(summary, dict):
                summary = {"zh": str(summary), "tags": []}

            # 补全缺失字段
            summary.setdefault("zh", "暂无摘要")
            summary.setdefault("en", summary.get("zh", "No summary available"))
            summary.setdefault("tags_zh", summary.get("tags", []))
            summary.setdefault("tags_en", summary.get("tags", []))

            # 合并展示需要的数据
            view_data = {**entry["metadata"], "summary": summary}
            ordered_repos.append(view_data)

    # 4. 统计语言分布 (取前 5)
    lang_stats = {}
    for r in ordered_repos:
        lang = r.get("language")
        if lang:
            lang_stats[lang] = lang_stats.get(lang, 0) + 1

    # 转换为排序后的列表: [{"name": "Python", "count": 10}, ...]
    top_langs = sorted(
        [{"name": k, "count": v} for k, v in lang_stats.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:5]

    # 5. 渲染 Markdown (多语言版本)
    context = {
        "last_updated": store.data["last_updated"],
        "repos": ordered_repos,
        "top_langs": top_langs,
        "ai_model": cfg["ai"].get("model", "gpt-4o-mini"),
    }
    langs = ["zh", "en"]
    generated_mds = {}

    # 确保 dist 目录存在
    dist_dir = SCRIPT_DIR / "dist"
    dist_dir.mkdir(exist_ok=True)

    for lang in langs:
        lang_context = {**context, "current_lang": lang}
        base_name = cfg["output"].get("filename", "stars")
        output_name = f"{base_name}_{lang}.md"

        # 直接写入 dist 目录
        output_md_path = dist_dir / output_name
        md_content = generator.render(DEFAULT_MD_TEMPLATE, lang_context)

        # 物理写入磁盘
        output_md_path.write_text(md_content, encoding="utf-8")

        generated_mds[lang] = {"path": output_md_path, "content": md_content}
        log.info(f"✅ Markdown ({lang}) 生成完成: {output_md_path}")

    # 5. 可选：Vault 同步
    v_cfg = cfg.get("vault_sync", {})
    if v_cfg.get("enabled"):
        for lang, data in generated_mds.items():
            # 拼接路径: 文件夹 + 文件名 + 语言 + .md
            vault_dir = v_cfg.get("path", "GitHub-Stars/")
            if not vault_dir.endswith("/"):
                vault_dir += "/"

            base_name = cfg["output"].get("filename", "stars")
            vault_path = f"{vault_dir}{base_name}_{lang}.md"

            gh.push_file(
                v_cfg["repo"],
                vault_path,
                data["content"],
                v_cfg.get("commit_message", "automated update"),
                v_cfg["pat"],
            )

    # 6. 可选：GitHub Pages 生成
    p_cfg = cfg.get("pages_sync", {})
    if p_cfg.get("enabled"):
        try:
            out_dir = SCRIPT_DIR / p_cfg.get("output_dir", "dist")
            out_dir.mkdir(exist_ok=True)

            html_template = p_cfg.get("template", "index.html.j2")
            html_content = generator.render(html_template, context)

            html_path = out_dir / p_cfg.get("file_name", "index.html")
            html_path.write_text(html_content, encoding="utf-8")
            log.info(f"✅ HTML 生成完成: {html_path}")
        except Exception as e:
            log.error(f"❌ HTML 生成失败: {e}")

    log.info("同步任务结束")


if __name__ == "__main__":
    main()
