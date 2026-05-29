from __future__ import annotations

import html
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import time

import numpy as np
import pandas as pd
import streamlit as st
from openai import OpenAI

import db_client
import search_client


def ensure_running_in_streamlit() -> None:
    """避免误用 `python app.py` 直接运行导致组件初始化报错。

    正确方式应为：
      - .\\.venv\\Scripts\\python.exe -m streamlit run app.py
    """

    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        if get_script_run_ctx() is not None:
            return
    except Exception:
        pass

    print(
        "这是一个 Streamlit 应用，不能用 `python app.py` 直接运行。\n"
        "请在项目根目录执行：\n"
        "  .\\.venv\\Scripts\\python.exe -m streamlit run app.py\n"
    )
    raise SystemExit(0)


ensure_running_in_streamlit()

from streamlit_echarts import st_echarts

CORE_COLS = [
    "news_id",
    "content",
    "title",
    "url",
    "dtime",
    "source",
    "author",
    "read_count",
    "keywords",
    "keysentence",
    "summary",
    "scode_list",
    "sname_list",
]

OPTIONAL_COLS = [
    "like_count",
    "keyword",
    "searchtext",
    "status",
    "attach_file2",
    "attach_type2",
    "abstract_text",
    "cover_image",
    "cover_type",
]

KNOWN_COLS = CORE_COLS + OPTIONAL_COLS

FILTER_MATCH_COLS = [
    "title",
    "keywords",
    "keyword",
    "content",
]


def normalize_col(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def find_excel_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for path in root.glob("*.xlsx"):
        if path.name.startswith("~$"):
            continue
        files.append(path)
    return sorted(files)


@st.cache_data(show_spinner=False)
def load_excel(path: str, sheet_name: Optional[str]) -> pd.DataFrame:
    xls = pd.ExcelFile(path)
    if sheet_name is None:
        sheet_name = xls.sheet_names[0]
    df = pd.read_excel(xls, sheet_name=sheet_name)
    return df


def align_columns(df: pd.DataFrame) -> pd.DataFrame:
    norm_map = {normalize_col(col): col for col in df.columns}
    rename_map: Dict[str, str] = {}
    for expected in KNOWN_COLS:
        key = normalize_col(expected)
        if key in norm_map:
            rename_map[norm_map[key]] = expected
    df = df.rename(columns=rename_map)

    if len(rename_map) < 3 and df.shape[1] >= len(CORE_COLS):
        cols = list(df.columns)
        for idx, expected in enumerate(CORE_COLS):
            cols[idx] = expected
        df.columns = cols

    return df


def ensure_columns(df: pd.DataFrame, cols: List[str], fill_value: Any = "") -> pd.DataFrame:
    for col in cols:
        if col not in df.columns:
            df[col] = fill_value
    return df


def prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = align_columns(df)

    ensure_columns(df, FILTER_MATCH_COLS, fill_value="")
    ensure_columns(
        df,
        [
            "news_id",
            "url",
            "source",
            "author",
            "keysentence",
            "summary",
            "scode_list",
            "sname_list",
            "abstract_text",
            "attach_file2",
            "attach_type2",
            "cover_image",
            "cover_type",
            "searchtext",
            "status",
            "like_count",
        ],
        fill_value="",
    )

    if "dtime" in df.columns:
        df["dtime"] = pd.to_datetime(df["dtime"], errors="coerce")
    else:
        df["dtime"] = pd.NaT
    if "read_count" in df.columns:
        df["read_count"] = pd.to_numeric(df["read_count"], errors="coerce").fillna(0)
    else:
        df["read_count"] = 0

    if "status" in df.columns:
        df["status"] = pd.to_numeric(df["status"], errors="coerce")
    if "like_count" in df.columns:
        df["like_count"] = pd.to_numeric(df["like_count"], errors="coerce")

    return df


def safe_contains(series: pd.Series, keyword: str) -> pd.Series:
    return series.fillna("").astype(str).str.contains(keyword, case=False, regex=False, na=False)


def parse_event_nodes(text: str) -> List[Dict[str, str]]:
    nodes: List[Dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            raw_date, label = line.split("|", 1)
        elif " " in line:
            raw_date, label = line.split(" ", 1)
        else:
            continue
        raw_date = raw_date.strip()
        label = label.strip()
        if not raw_date or not label:
            continue
        try:
            parsed = pd.to_datetime(raw_date).date()
        except Exception:
            continue
        nodes.append({"date": parsed.isoformat(), "label": label})
    return nodes


def filter_dataframe(
    df: pd.DataFrame,
    start_date: date,
    end_date: date,
    keyword: str,
) -> pd.DataFrame:
    kw = (keyword or "").strip()

    mask = df["dtime"].notna()
    mask &= df["dtime"].dt.date.between(start_date, end_date)

    if "status" in df.columns:
        status = pd.to_numeric(df["status"], errors="coerce").fillna(0)
        mask &= status >= 0

    if kw:
        # 支持 | 分隔的多关键词 AND 匹配：每个关键词必须在至少一个字段中出现
        keywords = [k.strip() for k in kw.split("|") if k.strip()]
        for k in keywords:
            kw_match = safe_contains(df["title"], k)
            kw_match |= safe_contains(df["keywords"], k)
            kw_match |= safe_contains(df["keyword"], k)
            kw_match |= safe_contains(df["content"], k)
            mask &= kw_match

    return df.loc[mask].copy()


def filter_by_entities(df: pd.DataFrame, entities: List[str]) -> pd.DataFrame:
    """初筛：正文content必须包含全部核心实体词（AND匹配）。"""
    if not entities:
        return df.copy()
    mask = pd.Series(True, index=df.index)
    for entity in entities:
        mask &= df["content"].fillna("").astype(str).str.contains(
            entity, case=False, regex=False, na=False
        )
    return df.loc[mask].copy()


# ── Embedding 粗筛 + LLM 精筛 ──────────────────────────────

def load_embedding_config() -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3-VL-Embedding-8B",
        "api_key": "",
        "batch_size": 32,
        "timeout": 30,
    }
    try:
        import local_settings
        for key in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY",
                     "EMBEDDING_BATCH_SIZE", "EMBEDDING_TIMEOUT"):
            if hasattr(local_settings, key):
                val = getattr(local_settings, key)
                if val is not None and val != "":
                    config[key.lower().replace("embedding_", "")] = val
    except ImportError:
        pass
    return config


def _build_article_text(row: pd.Series) -> str:
    title = str(row.get("title", "") or "")
    summary = str(row.get("summary", "") or "")
    keysentence = str(row.get("keysentence", "") or "")
    content = str(row.get("content", "") or "")[:500]
    return f"{title} {keysentence} {summary} {content}".strip()


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    import numpy as np
    a_arr = np.array(a)
    b_arr = np.array(b)
    dot = np.dot(a_arr, b_arr)
    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _call_embedding_api(texts: List[str], config: Dict[str, Any]) -> Optional[List[List[float]]]:
    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
    try:
        response = client.embeddings.create(
            model=config["model"],
            input=texts,
        )
    except Exception as exc:
        st.warning(f"Embedding API 调用失败：{exc}")
        return None
    return [d.embedding for d in response.data]


def embedding_coarse_filter(
    df: pd.DataFrame,
    keyword: str,
    config: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    meta: Dict[str, Any] = {"enabled": False, "input": len(df), "output": len(df)}
    if not config.get("api_key") or len(df) <= 1:
        return df, meta

    texts = [_build_article_text(row) for _, row in df.iterrows()]
    query_text = keyword.replace("|", " ")

    all_texts = [query_text] + texts
    batch_size = int(config.get("batch_size", 32))
    all_embeddings: List[List[float]] = []

    for i in range(0, len(all_texts), batch_size):
        batch = all_texts[i:i + batch_size]
        batch_embs = _call_embedding_api(batch, config)
        if batch_embs is None:
            return df, meta
        all_embeddings.extend(batch_embs)

    query_emb = all_embeddings[0]
    article_embs = all_embeddings[1:]

    scores = [_cosine_similarity(query_emb, emb) for emb in article_embs]
    df = df.copy()
    df["_embed_score"] = scores

    THRESHOLD = 0.35
    filtered = df[df["_embed_score"] >= THRESHOLD].copy()
    filtered = filtered.drop(columns=["_embed_score"])

    meta["enabled"] = True
    meta["output"] = len(filtered)
    meta["threshold"] = THRESHOLD
    meta["dropped"] = len(df) - len(filtered)
    return filtered, meta


def llm_fine_filter(
    df: pd.DataFrame,
    keyword: str,
    api_key: str,
    base_url: str,
    model: str,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    meta: Dict[str, Any] = {"enabled": False, "input": len(df), "output": len(df)}
    if not api_key or len(df) == 0:
        return df, meta

    client = OpenAI(api_key=api_key, base_url=base_url)
    keep_indices: List[int] = []

    BATCH_SIZE = 5
    rows = df.to_dict(orient="records")

    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start:batch_start + BATCH_SIZE]
        lines = [
            f"判断以下文章是否确实与事件「{keyword}」相关，而非只是恰好包含关键词。",
            "对每篇文章输出JSON：{\"idx\":序号,\"relevant\":true/false}",
            "",
        ]
        for i, row in enumerate(batch):
            title = str(row.get("title", ""))[:200]
            summary = str(row.get("summary", ""))[:300]
            content = str(row.get("content", ""))[:500]
            lines.append(f"--- 文章{i+1} ---")
            lines.append(f"标题：{title}")
            lines.append(f"摘要：{summary}")
            lines.append(f"正文片段：{content}")
        lines.append("")
        lines.append("输出JSON数组：")

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "\n".join(lines)}],
                temperature=0.1,
            )
            content = (response.choices[0].message.content or "").strip()
            results = json.loads(content)
            for item in results:
                idx = int(item.get("idx", 0)) - 1
                if item.get("relevant", False):
                    keep_indices.append(batch_start + idx)
        except Exception:
            # 如果LLM调用失败，保守保留该批次所有文章
            for i in range(len(batch)):
                keep_indices.append(batch_start + i)

    if keep_indices:
        filtered = df.iloc[keep_indices].copy()
    else:
        filtered = df.head(0).copy()

    meta["enabled"] = True
    meta["output"] = len(filtered)
    meta["dropped"] = len(df) - len(filtered)
    return filtered, meta


# ────────────────────────────────────────────────────────────


def build_timeline_option(
    days: List[str],
    counts: List[int],
    event_nodes: List[Dict[str, str]],
    day_articles_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    mark_lines = [
        {"xAxis": node["date"], "name": f"{node['label']}\n{node['date'][5:]}"}
        for node in event_nodes
    ]
    if day_articles_map is None:
        day_articles_map = {}

    series_data = []
    for d, c in zip(days, counts):
        series_data.append({"value": c, "articles": day_articles_map.get(d, "")})

    tooltip_formatter = (
        "function(params){"
        "var p=params[0];"
        "if(!p)return'';"
        "var h=p.axisValue+'<br/>文章数：'+p.value;"
        "if(p.data&&p.data.articles&&p.data.articles.length>0)"
        "h+='<br/>'+p.data.articles;"
        "return h;"
        "}"
    )
    option: Dict[str, Any] = {
        "tooltip": {
            "trigger": "axis",
            "formatter": tooltip_formatter,
        },
        "xAxis": {
            "type": "category",
            "data": days,
            "axisLabel": {
                "rotate": 45,
            },
        },
        "yAxis": {"type": "value", "min": 0},
        "grid": {"left": "3%", "right": "4%", "bottom": "14%", "containLabel": True},
        "dataZoom": [{"type": "inside"}, {"type": "slider"}],
        "series": [
            {
                "name": "文章数",
                "type": "line",
                "smooth": True,
                "data": series_data,
                "itemStyle": {"color": "#3b82f6"},
                "areaStyle": {
                    "color": {
                        "type": "linear",
                        "x": 0,
                        "y": 0,
                        "x2": 0,
                        "y2": 1,
                        "colorStops": [
                            {"offset": 0, "color": "rgba(59,130,246,0.15)"},
                            {"offset": 1, "color": "rgba(59,130,246,0.02)"},
                        ],
                    }
                },
                "label": {
                    "show": True,
                },
                "markLine": {
                    "symbol": ["none", "none"],
                    "lineStyle": {
                        "color": "#d4a855",
                        "width": 1.5,
                        "type": "dashed",
                    },
                    "label": {
                        "show": True,
                        "fontSize": 11,
                        "color": "#92400e",
                        "backgroundColor": "rgba(251,191,36,0.15)",
                        "padding": [2, 4],
                        "borderRadius": 4,
                        "formatter": "{b}",
                    },
                    "data": mark_lines,
                },
            }
        ],
    }
    return option


def extract_clicked_date(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    if isinstance(payload, dict):
        if "click" in payload:
            return payload["click"]
        if "name" in payload:
            return payload["name"]
    if isinstance(payload, str):
        return payload
    return None


def build_llm_prompt(rows: List[Dict[str, Any]]) -> str:
    parts = [
        "你是一位金融资讯分析师。请对以下阅读量最高的10篇文章进行观点聚类分析。",
        "",
        "输入格式（每篇文章）：",
        "- 标题：{TITLE}",
        "- 来源：{SOURCE}",
        "- 阅读量：{READ_COUNT}",
        "- 发布时间：{DTIME}",
        "- 关键句：{KEYSENTENCE}",
        "- 摘要：{SUMMARY}",
        "- 正文前2000字：{CONTENT}",
        "",
        "要求：",
        "1. 将文章中相似的观点聚合成2-4个观点类型，每个类型代表一种不同的分析角度或立场。",
        "2. 同一聚类下的文章应表达相同或相近的核心判断。",
        "3. 如果某篇文章只是新闻报道、没有明确观点，可不纳入任何聚类。",
        "4. 每篇文章可以归属到多个聚类（如果其表达了多种观点）。",
        "5. cluster_name不超过20字，core_viewpoint不超过50字，detail不超过200字。",
        "6. 输出格式为JSON数组，每项包含：",
        "   - cluster_name: 观点类型名称",
        "   - core_viewpoint: 核心观点，精炼概括该聚类所有文章的共同判断",
        "   - detail: 观点详情，包括数据逻辑推演、论据支撑等（有就写，没有则留空）",
        "   - sources: 来源文章数组，每项含 title, source, time, read_count, url",
        "",
        "输出：",
        '[{"cluster_name":"...","core_viewpoint":"...","detail":"...","sources":[{"title":"...","source":"...","time":"...","read_count":0,"url":"..."}]}]',
        "",
    ]
    for row in rows:
        content = str(row.get("content", ""))[:2000]
        time_str = str(row.get("dtime", "") or "")
        parts.append("---")
        parts.append(f"标题：{row.get('title','')}")
        parts.append(f"来源：{row.get('source','')}")
        parts.append(f"阅读量：{row.get('read_count','')}")
        parts.append(f"发布时间：{time_str}")
        parts.append(f"关键句：{row.get('keysentence','')}")
        parts.append(f"摘要：{row.get('summary','')}")
        parts.append(f"正文前2000字：{content}")
    return "\n".join(parts)


def viewpoint_cache_key(base_url: str, model: str, rows: List[Dict[str, Any]]) -> Tuple[str, str]:
    prompt = build_llm_prompt(rows)
    cache_key = hashlib.sha1(f"{model}|{base_url}|{prompt}".encode("utf-8")).hexdigest()
    return cache_key, prompt


def call_deepseek(
    api_key: str,
    base_url: str,
    model: str,
    rows: List[Dict[str, Any]],
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    cache_key, prompt = viewpoint_cache_key(base_url, model, rows)
    cache = st.session_state.setdefault("viewpoint_cache", {})
    if cache_key in cache:
        return cache[cache_key], "cached"

    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
    except Exception as exc:
        return None, str(exc)
    content = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None, content
    cache[cache_key] = data
    return data, "ok"


def recognize_intent(
    api_key: str,
    base_url: str,
    model: str,
    keyword: str,
    search_results: List[Dict[str, str]],
    start_date: date,
    end_date: date,
) -> Tuple[Optional[Dict[str, Any]], str]:
    if not api_key or not search_results:
        return None, "无搜索内容或API Key"

    items: List[str] = []
    for idx, item in enumerate(search_results, start=1):
        items.append(
            f"#{idx}\n标题：{item.get('title','')}\n摘要：{item.get('snippet','')}\n"
            f"内容片段：{str(item.get('content',''))[:500]}"
        )

    prompt = (
        "你是一名资深金融事件分析师。请根据联网检索结果，结合你的金融领域知识，深度理解该事件。\n\n"
        "输出必须是合法JSON对象，不要添加Markdown代码块。\n\n"
        "字段要求：\n\n"
        "- event_name: 规范化事件名。格式：核心主体 + 关键动作 + 对市场/行业的影响。不超过30字。\n"
        "  好的示例：'海湖庄园协议签署引发中美关税升级与全球供应链震荡'\n"
        "  坏的示例：'海湖庄园协议引发全球热议'（太泛，没有具体影响）\n\n"
        "- event_entities: 1-3个实体词，将用于数据库全文检索（AND匹配）筛选文章。\n"
        "  核心判据：每个实体必须是在【所有】讨论该事件的文章中都必然出现的关键词。\n"
        "  - 如果某个词只在部分文章中出现（如具体人名、细节术语），不要输出。\n"
        "  - 如果1个词（如'韬定律'）已经足够唯一地锁定该事件，就只输出1个。\n"
        "  - 如果1个词过于宽泛（如'GPT'会命中大量无关文章），则需加上限定词（如'GPT'+'6.0'）。\n\n"
        "- Industry_asset: 事件关联的可交易标的，必须是能在金融终端检索到的品种。\n"
        "  可选类型：A股板块（如'半导体''稀土永磁'）、个股（如'中芯国际'）、大宗商品（如'沪铜''原油'）、\n"
        "  期货/外汇（如'离岸人民币''COMEX黄金'）。\n"
        "  基于检索内容推断事件对各类资产的影响方向，不限于字面提及的标的。\n"
        "  如无法确定具体个股，至少输出受影响的板块。如果确实无法识别则输出空数组。\n\n"
        "- confidence: 0-1之间的小数，表示对该事件理解的置信度\n\n"
        f"时间区间：{start_date.isoformat()} ~ {end_date.isoformat()}\n"
        f"用户原始关键词：{keyword}\n\n"
        f"联网检索结果（共{len(search_results)}条）：\n\n"
        + "\n\n".join(items)
    )

    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (response.choices[0].message.content or "").strip()
        payload = json.loads(content)
        if not isinstance(payload, dict):
            return None, f"LLM返回格式异常：{content[:300]}"
        return payload, "ok"
    except json.JSONDecodeError:
        return None, f"JSON解析失败：{content[:300]}"
    except Exception as exc:
        return None, str(exc)


def recognize_timeline(
    event_name: str,
    entities: List[str],
    start_date: date,
    end_date: date,
    search_cfg: Dict[str, Any],
    api_key: str,
    base_url: str,
    model: str,
) -> Tuple[List[Dict[str, str]], str]:
    """第二阶段：联网搜索事件时间线，LLM提取关键时间节点。"""
    if not api_key or not search_cfg.get("enabled"):
        return [], "搜索未配置或无API Key"

    # 搜索查询包含时间范围，确保结果聚焦于区间内
    timeline_query = f"{event_name} {start_date.strftime('%Y年%m月')} {end_date.strftime('%Y年%m月')} 关键节点"
    timeline_results, srch_status = search_client.web_search(
        timeline_query, search_cfg, start_date, end_date,
    )
    if srch_status != "ok" or not timeline_results:
        return [], f"时间线搜索失败：{srch_status}"

    items: List[str] = []
    for idx, item in enumerate(timeline_results, start=1):
        items.append(
            f"#{idx}\n标题：{item.get('title','')}\n摘要：{item.get('snippet','')}\n"
            f"内容片段：{str(item.get('content',''))[:800]}"
        )

    # 解析用户提供的时间区间边界
    s_str = start_date.isoformat()
    e_str = end_date.isoformat()

    prompt = (
        "你是一名资深金融事件分析师。请根据联网检索结果，提取该事件在指定时间区间内的关键时间节点。\n\n"
        "输出必须是合法JSON数组，不要添加Markdown代码块。格式：\n"
        '[{"node_name": "节点描述", "time": "yyyy-MM-dd"}, ...]\n\n'
        f"★★★ 硬性约束 ★★★\n"
        f"每个节点的 time 必须严格在 {s_str} ~ {e_str} 范围内！\n"
        f"禁止输出早于 {s_str} 或晚于 {e_str} 的日期。\n"
        f"如果某个节点日期超出此范围，不要收录。\n\n"
        "收录规则：\n"
        "- 必须是事件本身的里程碑节点（宣布/签署/生效/升级/反制/转折）\n"
        "- 按时间升序排列，最多5个\n"
        "- node_name格式：'主体 + 动作'，不超过30字\n"
        "- time格式必须为yyyy-MM-dd\n\n"
        "排除规则（严禁收录）：\n"
        "- 券商/机构的研报、分析、评论——这是观察者视角，不是事件节点\n"
        "- 经济数据发布（CPI/GDP/PMI等）——这是结果/影响，不是事件节点\n"
        "- 市场反应描述（'股市下跌''避险情绪升温'）——这是市场反应，不是事件节点\n"
        "- 与事件无直接因果的同期事件\n\n"
        f"事件名称：{event_name}\n"
        f"核心实体：{', '.join(entities)}\n"
        f"允许时间范围：{s_str} ~ {e_str}\n\n"
        f"联网检索结果（共{len(timeline_results)}条）：\n\n"
        + "\n\n".join(items)
    )

    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (response.choices[0].message.content or "").strip()
        data = json.loads(content)
        if not isinstance(data, list):
            return [], f"LLM返回格式异常：{content[:300]}"
        key_points: List[Dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            node_name = str(item.get("node_name") or item.get("label") or "").strip()
            time_raw = item.get("time") or item.get("date") or ""
            time_str = str(time_raw).strip()
            if not node_name or not time_str:
                continue
            # 后置校验：过滤区间外的日期
            try:
                kp_date = pd.to_datetime(time_str).date()
                if kp_date < start_date or kp_date > end_date:
                    continue
            except Exception:
                continue
            key_points.append({"node_name": node_name, "time": time_str})
        return key_points[:5], "ok"
    except json.JSONDecodeError:
        return [], f"JSON解析失败：{content[:300]}"
    except Exception as exc:
        return [], str(exc)


def parse_key_points(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    items = payload.get("key_points", [])
    if not isinstance(items, list):
        return []
    key_points: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        node_name = str(item.get("node_name") or item.get("label") or "").strip()
        time_raw = item.get("time") or item.get("date") or ""
        time_str = str(time_raw).strip()
        if node_name and time_str:
            key_points.append({"node_name": node_name, "time": time_str})
    return key_points[:5]


def pick_summary(row: pd.Series) -> str:
    abstract_text = str(row.get("abstract_text", "") or "").strip()
    if abstract_text:
        return abstract_text
    summary = str(row.get("summary", "") or "").strip()
    if summary:
        return summary
    keysentence = str(row.get("keysentence", "") or "").strip()
    if keysentence:
        return keysentence[:100]
    return ""


def split_codes(value: Any) -> List[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    parts = re.split(r"[,，;\s]+", str(value))
    return [p.strip() for p in parts if p.strip()]


def parse_stock_codes(series: pd.Series) -> List[str]:
    codes: List[str] = []
    for value in series.dropna().astype(str):
        codes.extend(split_codes(value))
    return sorted(set(codes))


def top_stock_codes(series: pd.Series, top_k: int = 5) -> List[str]:
    counter: Counter[str] = Counter()
    for value in series.dropna().astype(str):
        for code in split_codes(value):
            counter[code] += 1
    return [code for code, _ in counter.most_common(top_k)]


def parse_stock_name_map(series: pd.Series) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if series is None:
        return mapping
    pattern = re.compile(r"(\d{6})\(([^)]+)\)")
    for value in series.dropna().astype(str):
        for code6, name in pattern.findall(value):
            name = name.strip()
            if name:
                mapping[code6] = name
    return mapping


def stock_label(code: str, name_map: Dict[str, str]) -> str:
    m = re.search(r"\d{6}", code)
    if m:
        code6 = m.group(0)
        if code6 in name_map:
            return name_map[code6]
    return code


def mock_ohlc(
    code: str,
    start: date,
    end: date,
) -> Tuple[List[str], List[List[float]]]:
    dates = pd.date_range(start, end, freq="B")
    if dates.empty:
        return [], []

    base = 1800.0 if "600519" in code else 100.0
    seed = int(hashlib.sha1(code.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    price = base
    data: List[List[float]] = []
    for _ in dates:
        open_p = price * (1 + rng.normal(0, 0.003))
        close_p = open_p * (1 + rng.normal(0, 0.012))
        high_p = max(open_p, close_p) * (1 + abs(rng.normal(0, 0.006)))
        low_p = min(open_p, close_p) * (1 - abs(rng.normal(0, 0.006)))
        data.append([
            round(open_p, 2),
            round(close_p, 2),
            round(low_p, 2),
            round(high_p, 2),
        ])
        price = close_p
    return [d.strftime("%Y-%m-%d") for d in dates], data


def build_kline_option(
    dates: List[str],
    ohlc: List[List[float]],
    event_nodes: List[Dict[str, str]],
) -> Dict[str, Any]:
    mark_lines = [
        {"xAxis": node["date"], "name": node["label"]}
        for node in event_nodes
    ]
    option: Dict[str, Any] = {
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "cross"},
            "formatter": """
function(params){
  var p = params && params[0];
  if(!p || !p.data){return '';}
  var d = p.axisValue;
  var o = p.data[0];
  var c = p.data[1];
  var l = p.data[2];
  var h = p.data[3];
  var ch = (c - o);
  var pct = (o ? ch / o * 100 : 0);
  var color = (c >= o) ? '#ef4444' : '#22c55e';
  function f(x){ return (Number(x) || 0).toFixed(2); }
  return d + '<br/>'
    + '开盘：' + f(o) + '　收盘：' + f(c) + '<br/>'
    + '最高：' + f(h) + '　最低：' + f(l) + '<br/>'
    + '<span style="color:' + color + '">涨跌：' + f(ch) + '（' + f(pct) + '%）</span>';
}
""".strip(),
        },
        "xAxis": {
            "type": "category",
            "data": dates,
            "axisLabel": {"rotate": 45},
        },
        "yAxis": {"type": "value", "scale": True},
        "grid": {"left": "3%", "right": "4%", "bottom": "14%", "containLabel": True},
        "series": [
            {
                "type": "candlestick",
                "data": ohlc,
                "itemStyle": {
                    "color": "#ef4444",
                    "color0": "#22c55e",
                    "borderColor": "#ef4444",
                    "borderColor0": "#22c55e",
                },
                "markLine": {
                    "symbol": ["none", "none"],
                    "lineStyle": {
                        "color": "#d4a855",
                        "width": 1.5,
                        "type": "dashed",
                    },
                    "label": {
                        "show": True,
                        "fontSize": 11,
                        "color": "#ffffff",
                        "backgroundColor": "#d4a855",
                        "padding": [2, 4],
                        "borderRadius": 4,
                        "formatter": "{b}",
                    },
                    "data": mark_lines,
                },
            }
        ],
    }
    return option


def format_read_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def format_datetime(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        return value.strftime("%m-%d %H:%M")
    if isinstance(value, datetime):
        return value.strftime("%m-%d %H:%M")
    return str(value)


def format_time_hhmm(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        return value.strftime("%H:%M")
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    try:
        parsed = pd.to_datetime(value)
        if pd.notna(parsed):
            return parsed.strftime("%H:%M")
    except Exception:
        pass
    return ""


def inject_css() -> None:
    st.markdown(
        """
<style>
.hr-card{background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:12px 12px;margin-top:8px;}
.hr-card-title{font-weight:600;color:#111827;font-size:16px;margin-bottom:8px;}
.hr-muted{color:#6b7280;font-size:12px;}

.daily-list{max-height:400px;overflow-y:auto;border-top:1px solid #f3f4f6;}
.daily-row{display:flex;align-items:center;height:48px;border-bottom:1px solid #f3f4f6;padding:0 8px;gap:10px;}
.daily-row:hover{background:#f9fafb;}
.daily-rank{width:32px;text-align:center;font-weight:700;}
.daily-main{flex:1;overflow:hidden;}
.daily-title{font-size:14px;color:#111827;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;}
.daily-source{font-size:12px;color:#6b7280;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.daily-right{width:140px;text-align:right;}
.daily-read{font-size:14px;color:#3b82f6;font-weight:700;}
.daily-time{font-size:12px;color:#9ca3af;}

.vp-list{max-height:500px;overflow-y:auto;border-top:1px solid #f3f4f6;}
.vp-row{display:flex;gap:12px;padding:10px 8px;border-bottom:1px solid #f3f4f6;}
.vp-row:hover{background:#f9fafb;}
.vp-rank{width:32px;text-align:center;font-weight:700;}
.vp-body{flex:1;overflow:hidden;}
.vp-title{font-size:14px;color:#111827;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;}
.vp-meta{font-size:12px;color:#6b7280;margin-top:2px;}
.vp-text{font-size:13px;color:#374151;margin-top:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.vp-link{width:90px;text-align:right;font-size:12px;}
.vp-link a{color:#3b82f6;text-decoration:none;}

.wall-link{text-decoration:none;color:inherit;}
.wall-card{border:1px solid #e5e7eb;border-radius:12px;background:#ffffff;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);transition:transform 200ms ease, box-shadow 200ms ease;}
.wall-card:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,0.1);}
.wall-cover{height:160px;width:100%;object-fit:cover;display:block;background:#f3f4f6;}
.wall-cover-ph{height:160px;width:100%;background:#f3f4f6;display:flex;align-items:center;justify-content:center;color:#9ca3af;font-size:12px;}
.wall-body{padding:12px 16px 12px 16px;}
.wall-title{font-size:14px;color:#111827;font-weight:600;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:40px;}
.wall-summary{font-size:12px;color:#6b7280;margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:32px;}
.wall-footer{display:flex;justify-content:space-between;border-top:1px solid #f3f4f6;padding:10px 16px;font-size:12px;}
.wall-source{color:#3b82f6;}
.wall-time{color:#9ca3af;}
</style>
""",
        unsafe_allow_html=True,
    )


def render_daily_hot_list(day_df: pd.DataFrame, selected_day: date) -> str:
    safe_day = selected_day.strftime("%Y-%m-%d")
    rows_html: List[str] = []

    for idx, (_, row) in enumerate(day_df.iterrows()):
        rank = idx + 1
        rank_color = "#d4a855" if rank <= 3 else "#9ca3af"
        title = html.escape(str(row.get("title", "") or ""))
        source = html.escape(str(row.get("source", "") or ""))
        author = html.escape(str(row.get("author", "") or ""))
        read_count = format_read_count(row.get("read_count", 0))
        time_str = format_time_hhmm(row.get("dtime"))
        url = str(row.get("url", "") or "").strip()
        if url:
            url_attr = html.escape(url, quote=True)
            title_html = f'<a class="daily-title" href="{url_attr}" target="_blank" rel="noopener noreferrer">{title}</a>'
        else:
            title_html = f'<span class="daily-title">{title}</span>'

        meta_parts = [source]
        if author:
            meta_parts.append(author)
        meta_str = " · ".join(meta_parts)

        rows_html.append(
            """
<div class="daily-row">
  <div class="daily-rank" style="color:{rank_color};">{rank}</div>
  <div class="daily-main">
    {title_html}
    <div class="daily-source">{meta_str}</div>
  </div>
  <div class="daily-right">
    <div class="daily-read">{read_count}</div>
    <div class="daily-time">{time_str}</div>
  </div>
</div>
""".format(
                rank=rank,
                rank_color=rank_color,
                title_html=title_html,
                meta_str=html.escape(meta_str),
                read_count=read_count,
                time_str=html.escape(time_str),
            )
        )

    return (
        f'<div class="hr-card">'
        f'<div class="hr-card-title">{safe_day} 当日热点文章（共{len(day_df)}篇）</div>'
        f'<div class="daily-list">{"".join(rows_html)}</div>'
        f'</div>'
    )


def render_viewpoint_clusters(clusters: List[Dict[str, Any]]) -> str:
    rows_html: List[str] = []
    for idx, cluster in enumerate(clusters):
        cluster_name = html.escape(str(cluster.get("cluster_name", "") or ""))
        core_viewpoint = html.escape(str(cluster.get("core_viewpoint", "") or ""))
        detail = html.escape(str(cluster.get("detail", "") or ""))
        sources = cluster.get("sources", [])
        if not isinstance(sources, list):
            sources = []

        source_items: List[str] = []
        for src in sources:
            src_title = html.escape(str(src.get("title", "") or ""))
            src_source = html.escape(str(src.get("source", "") or ""))
            src_time = html.escape(str(src.get("time", "") or ""))
            src_read = format_read_count(src.get("read_count", 0))
            src_url = str(src.get("url", "") or "").strip()
            if src_url:
                src_url_attr = html.escape(src_url, quote=True)
                src_link = f'<a href="{src_url_attr}" target="_blank" rel="noopener noreferrer" style="color:#3b82f6;text-decoration:none;">{src_title}</a>'
            else:
                src_link = src_title
            source_items.append(
                f'<div style="font-size:12px;color:#6b7280;margin-top:2px;">'
                f'{src_link} — {src_source} · {src_time} · 阅读{src_read}'
                f'</div>'
            )

        sources_html = "".join(source_items)

        rows_html.append(
            """
<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin-bottom:12px;">
  <div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:8px;">
    <span style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:50%;background:#3b82f6;color:#fff;font-size:13px;font-weight:700;flex-shrink:0;">{idx}</span>
    <div>
      <div style="font-weight:700;color:#111827;font-size:15px;">{cluster_name}</div>
      <div style="font-size:14px;color:#1d4ed8;margin-top:4px;line-height:1.5;">{core_viewpoint}</div>
    </div>
  </div>
  {detail_html}
  <div style="margin-top:10px;border-top:1px solid #f3f4f6;padding-top:8px;">
    <div style="font-size:12px;color:#9ca3af;margin-bottom:4px;">来源文章：</div>
    {sources_html}
  </div>
</div>
""".format(
                idx=idx + 1,
                cluster_name=cluster_name,
                core_viewpoint=core_viewpoint,
                detail_html=f'<div style="font-size:13px;color:#374151;margin-top:6px;line-height:1.6;">{detail}</div>' if detail else "",
                sources_html=sources_html,
            )
        )

    return (
        '<div class="hr-card">'
        '<div class="hr-card-title">区间热点观点聚类（阅读量Top10）</div>'
        f'<div style="max-height:600px;overflow-y:auto;border-top:1px solid #f3f4f6;padding-top:8px;">{"".join(rows_html)}</div>'
        '</div>'
    )


def render_wall_card(row: pd.Series) -> str:
    title = html.escape(str(row.get("title", "") or ""))
    summary = html.escape(pick_summary(row) or "—")
    source = html.escape(str(row.get("source", "") or ""))
    dtime = html.escape(format_datetime(row.get("dtime")))
    url = str(row.get("url", "") or "").strip()

    cover_url = ""
    for key in ("cover_image", "attach_file2"):
        value = str(row.get(key, "") or "").strip()
        if value:
            cover_url = value
            break

    if cover_url and cover_url.lower().startswith("http"):
        cover_html = f'<img class="wall-cover" src="{html.escape(cover_url, quote=True)}" alt="cover" />'
    else:
        cover_html = '<div class="wall-cover-ph">暂无封面</div>'

    inner = (
        f'<div class="wall-card">'
        f'{cover_html}'
        f'<div class="wall-body">'
        f'<div class="wall-title">{title}</div>'
        f'<div class="wall-summary">{summary}</div>'
        f'</div>'
        f'<div class="wall-footer">'
        f'<div class="wall-source">{source}</div>'
        f'<div class="wall-time">{dtime}</div>'
        f'</div>'
        f'</div>'
    )

    if url:
        return f'<a class="wall-link" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{inner}</a>'
    return inner


st.set_page_config(page_title="热点复盘", layout="wide")

root = Path.cwd()
excel_files = find_excel_files(root)

DEFAULT_EXCEL = "关税战海湖庄园协议_完整字段.xlsx"
DEFAULT_KEYWORD = "关税战|海湖庄园协议"
default_path = root / DEFAULT_EXCEL
default_exists = default_path.exists()

st.title("热点复盘")
inject_css()

if not excel_files:
    st.warning("未找到 Excel 数据文件，请上传至项目根目录。")
    st.stop()

# ── 侧边栏：搜索历史 ──
with st.sidebar:
    st.header("搜索历史")
    history = st.session_state.setdefault("search_history", [])

    if not history:
        st.caption("暂无搜索历史")
    else:
        for i, entry in enumerate(reversed(history)):
            kw_display = entry["keyword"].replace("|", " + ")
            with st.container():
                c1, c2 = st.columns([5, 1])
                with c1:
                    st.markdown(f"**{html.escape(kw_display)}**")
                    st.caption(
                        f"{entry['start_date']} ~ {entry['end_date']}  "
                        f"| {entry['result_count']} 篇  "
                        f"| {entry['timestamp']}"
                    )
                with c2:
                    if st.button("↩", key=f"hist_restore_{i}", help="恢复此搜索"):
                        st.session_state["date_range"] = (
                            pd.to_datetime(entry["start_date"]).date(),
                            pd.to_datetime(entry["end_date"]).date(),
                        )
                        st.session_state["keyword_input"] = entry["keyword"]
                        st.session_state["restore_from_history"] = True
                        st.rerun()

        if st.button("清空历史", key="clear_history"):
            st.session_state["search_history"] = []
            st.rerun()

# ── 后台配置：全部从 local_settings.py / 环境变量读取 ──
def _load_config():
    config = {
        "excel_file": "关税战海湖庄园协议_完整字段.xlsx",
        "excel_sheet": None,
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
    }
    try:
        import local_settings as ls
        if hasattr(ls, "EXCEL_FILE") and ls.EXCEL_FILE:
            config["excel_file"] = ls.EXCEL_FILE
        if hasattr(ls, "EXCEL_SHEET") and ls.EXCEL_SHEET:
            config["excel_sheet"] = ls.EXCEL_SHEET
        if hasattr(ls, "LLM_BASE_URL") and ls.LLM_BASE_URL:
            config["base_url"] = ls.LLM_BASE_URL
        if hasattr(ls, "LLM_MODEL") and ls.LLM_MODEL:
            config["model"] = ls.LLM_MODEL
        for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
            if hasattr(ls, key) and getattr(ls, key):
                config["api_key"] = getattr(ls, key)
                break
    except ImportError:
        pass
    return config

cfg = _load_config()

# 确定数据文件
file_names = [p.name for p in excel_files]
if cfg["excel_file"] in file_names:
    selected_file = cfg["excel_file"]
else:
    selected_file = file_names[0] if file_names else DEFAULT_EXCEL
selected_path = root / selected_file
sheet_name = cfg["excel_sheet"]
base_url = cfg["base_url"]
model = cfg["model"]
api_key = cfg["api_key"]

raw_df = load_excel(str(selected_path), sheet_name)
df = prepare_dataframe(raw_df)

if df["dtime"].notna().any():
    min_date = df["dtime"].min().date()
    max_date = df["dtime"].max().date()
else:
    today = date.today()
    min_date = today
    max_date = today

top_left, top_right = st.columns([1, 1])
with top_left:
    default_dates = (min_date, max_date)
    if st.session_state.pop("restore_from_history", None):
        default_dates = st.session_state.get("date_range", default_dates)
    date_range = st.date_input(
        "时间区间",
        value=default_dates,
        min_value=min_date,
        max_value=max_date,
        key="date_range",
    )
with top_right:
    default_kw = st.session_state.get("keyword_input", "")
    keyword = st.text_input(
        "事件关键词",
        value=default_kw,
        key="keyword_input",
    )

# ── 开始复盘按钮 ──
restore_from_hist = st.session_state.get("restore_from_history", False)
search_triggered = st.session_state.get("search_triggered", False)
test_timeline = st.session_state.get("test_timeline", False)

if not search_triggered and not restore_from_hist and not test_timeline:
    _, btn_col, btn_col2, _ = st.columns([1, 1, 1, 1])
    with btn_col:
        if st.button("开始复盘", type="primary", use_container_width=True):
            st.session_state["test_timeline"] = False
            st.session_state["search_triggered"] = True
            st.rerun()
    with btn_col2:
        if st.button("测试时间轴", use_container_width=True):
            st.session_state["test_timeline"] = True
            st.rerun()
    st.info("请输入事件关键词和时间区间，点击「开始复盘」进行分析。")
    st.stop()

if not test_timeline:
    st.session_state["search_triggered"] = True
st.session_state.pop("restore_from_history", None)

start_date, end_date = date_range
if start_date > end_date:
    st.error("时间区间不合法：开始日期不能晚于结束日期。")
    st.stop()

# ── 测试时间轴模式：跳过所有LLM/联网/精筛，直接用Excel数据展示 ──
if test_timeline:
    st.caption("测试模式：直接展示Excel数据，未经过联网/LLM/Emb/精筛")
    event_nodes = []
    filtered = filter_dataframe(df, start_date, end_date, "")
    filter_signature = hashlib.sha1(
        f"{selected_file}|{sheet_name}|{start_date}|{end_date}|test".encode("utf-8")
    ).hexdigest()

if not test_timeline:

    # ── 默认模式（关键词为空时使用预设关键词）──
    is_default_mode = False
    if not keyword.strip() and default_exists:
        is_default_mode = True
        keyword = DEFAULT_KEYWORD
        st.caption(f"关键词 {DEFAULT_KEYWORD.replace('|', ' + ')}")

    # ── 联网搜索 → LLM意图识别 → 置信度过滤 → 核心实体（两种模式都走）──
    intent_result: Optional[Dict[str, Any]] = None
    event_key_points: List[Dict[str, str]] = []
    if not api_key:
        st.warning("未配置 LLM API Key，跳过联网搜索与意图识别。请在 local_settings.py 中设置 DEEPSEEK_API_KEY。")
    else:
        srch_cfg = search_client.load_search_config()
        if not srch_cfg.get("enabled"):
            st.warning("未配置搜索服务，跳过联网搜索。请在 local_settings.py 中设置 SEARCH_PROVIDER 和 SEARCH_API_KEY。")
        else:
            search_query = keyword.replace("|", " ")
            with st.spinner(f"正在联网搜索「{search_query}」..."):
                search_results, srch_status = search_client.web_search(
                    search_query, srch_cfg, start_date, end_date,
                )
            if srch_status != "ok":
                st.warning(f"联网搜索失败：{srch_status}")
            elif not search_results:
                st.warning(f"联网搜索「{search_query}」未返回结果，请尝试调整关键词或时间区间。")
            else:
                st.caption(f"联网搜索返回 {len(search_results)} 条结果")
                with st.spinner("正在进行LLM意图识别（事件理解）..."):
                    intent_result, intent_status = recognize_intent(
                        api_key, base_url, model, search_query, search_results,
                        start_date, end_date,
                    )
                if intent_status != "ok":
                    st.warning(f"LLM意图识别失败：{intent_status}")
                elif not intent_result:
                    st.warning("LLM意图识别未返回有效结果。")
                else:
                    # 置信度过滤核心实体
                    raw_entities = intent_result.get("event_entities", [])
                    if isinstance(raw_entities, list) and raw_entities:
                        filtered_entities = search_client.filter_entities_by_confidence(
                            raw_entities, search_results, threshold=0.8
                        )
                        if filtered_entities:
                            keyword = "|".join(filtered_entities)
                            intent_result["event_entities"] = filtered_entities

                    # 存入 session_state 供后续展示
                    st.session_state["intent_result"] = intent_result

                    # ── 第二阶段：时间线搜索 → LLM提取关键节点 ──
                    event_name = intent_result.get("event_name", "")
                    final_entities = intent_result.get("event_entities", [])
                    if event_name:
                        with st.spinner(f"正在联网搜索「{event_name}」关键时间节点..."):
                            event_key_points, tl_status = recognize_timeline(
                                event_name, final_entities,
                                start_date, end_date,
                                srch_cfg, api_key, base_url, model,
                            )
                        if tl_status != "ok":
                            st.warning(f"时间线搜索失败：{tl_status}")
                        elif event_key_points:
                            intent_result["key_points"] = event_key_points
                            st.session_state["intent_result"] = intent_result

    # ── 数据获取：默认模式用Excel，非默认模式查DB ──
    db_used = False
    if is_default_mode:
        # 默认模式：使用默认Excel，视为DB返回的数据，继续走完整管线
        pass
    else:
        entities = [k.strip() for k in keyword.split("|") if k.strip()]
        db_cfg = db_client.load_db_config()
        if not db_cfg.get("enabled"):
            st.error("非默认模式需要配置公司数据库。请在 local_settings.py 中填写 DB_HOST、DB_USER、DB_PASSWORD、DB_SERVICE_NAME。")
            st.stop()
        if not entities:
            st.error("未能识别出有效核心实体，无法查询数据库。")
            st.stop()
        with st.spinner("正在从数据库查询文章数据..."):
            db_df, db_status = db_client.query_articles(entities, start_date, end_date, db_cfg)
        if db_status != "ok" or db_df is None or db_df.empty:
            st.error(f"数据库查询失败：{db_status}。请检查DB配置和网络连接。")
            st.stop()
        df = prepare_dataframe(db_df)
        db_used = True
        if df["dtime"].notna().any():
            min_date = df["dtime"].min().date()
            max_date = df["dtime"].max().date()

    # ── 展示意图识别结果 ──
    if st.session_state.get("intent_result"):
        ir = st.session_state["intent_result"]
        event_name = ir.get("event_name", "")
        entities_list = ir.get("event_entities", [])
        assets = ir.get("Industry_asset", [])
        key_points = parse_key_points(ir)

        if event_name:
            st.subheader("事件概况")
            # 第一行：事件名称（通栏）
            st.markdown(f"**事件名称：** {html.escape(event_name)}")
            # 第二行：核心实体 | 关联标的
            cols = st.columns([1, 1])
            with cols[0]:
                if isinstance(entities_list, list) and entities_list:
                    st.markdown(f"**核心实体：** {html.escape(', '.join(entities_list))}")
                else:
                    st.markdown("**核心实体：** 未识别")
            with cols[1]:
                if isinstance(assets, list) and assets:
                    st.markdown(f"**关联标的：** {html.escape(', '.join(assets))}")
                else:
                    st.markdown("**关联标的：** 未识别")
            # 第三行：关键节点
            if key_points:
                kp_lines = "  \n".join(
                    f"- {html.escape(kp['time'])}  {html.escape(kp['node_name'])}"
                    for kp in key_points
                )
                st.markdown(f"**关键节点：**  \n{kp_lines}")
            else:
                st.markdown("**关键节点：** 未识别")

    # ── 事件关键节点（LLM自动生成，可直接用于图表标注）──
    event_nodes: List[Dict[str, str]] = []
    if st.session_state.get("intent_result"):
        kps = parse_key_points(st.session_state["intent_result"])
        for kp in kps:
            event_nodes.append({"date": kp["time"], "label": kp["node_name"]})

    filter_signature = hashlib.sha1(
        f"{selected_file}|{sheet_name}|{start_date}|{end_date}|{keyword}".encode("utf-8")
    ).hexdigest()
    if st.session_state.get("filter_signature") != filter_signature:
        st.session_state["filter_signature"] = filter_signature
        st.session_state["selected_day"] = None
        st.session_state["daily_expanded"] = False
        st.session_state["viewpoint_display_key"] = None


    # ── 初筛：日期范围 + 状态过滤 ──
    filtered = filter_dataframe(df, start_date, end_date, "")

    # ── 核心实体 AND 匹配（正文content必须包含全部核心实体词）──
    intent_entities: List[str] = []
    if st.session_state.get("intent_result"):
        raw_ents = st.session_state["intent_result"].get("event_entities", [])
        if isinstance(raw_ents, list) and raw_ents:
            intent_entities = [str(e).strip() for e in raw_ents if str(e).strip()]

    if not intent_entities:
        # 降级：使用关键词拆分作为实体词
        intent_entities = [k.strip() for k in keyword.split("|") if k.strip()]

    if intent_entities and not filtered.empty:
        before_entity_filter = len(filtered)
        filtered = filter_by_entities(filtered, intent_entities)
        dropped = before_entity_filter - len(filtered)
        if dropped > 0:
            st.caption(f"实体AND初筛：{before_entity_filter} → {len(filtered)} 篇（要求正文包含：{', '.join(intent_entities)}）")

    # ── Embedding 粗筛 + LLM 精筛（默认/非默认模式都走）──
    if not filtered.empty and api_key:
        # 优先使用LLM识别的规范化事件名作为Embedding Query
        emb_query = keyword.replace("|", " ")
        if st.session_state.get("intent_result"):
            event_name = st.session_state["intent_result"].get("event_name", "")
            if event_name:
                emb_query = event_name
        embed_cfg = load_embedding_config()
        if embed_cfg.get("api_key") and len(filtered) > 10:
            with st.spinner(f"正在进行语义粗筛（Embedding）... 候选 {len(filtered)} 篇"):
                filtered, embed_meta = embedding_coarse_filter(filtered, emb_query, embed_cfg)
            if embed_meta.get("enabled"):
                st.caption(f"Embedding 粗筛：{embed_meta['input']} → {embed_meta['output']} 篇（阈值 {embed_meta.get('threshold', 0.35)}）")
        if not filtered.empty and len(filtered) > 1:
            with st.spinner(f"正在进行LLM精筛... 候选 {len(filtered)} 篇"):
                filtered, llm_meta = llm_fine_filter(filtered, emb_query, api_key, base_url, model)
            if llm_meta.get("enabled"):
                st.caption(f"LLM 精筛：{llm_meta['input']} → {llm_meta['output']} 篇")

# ── 保存搜索历史 ──
now = time.time()
last_save = st.session_state.get("_last_history_save", 0)
if now - last_save > 2 and keyword.strip():  # 2秒防抖，避免逐字输入时重复保存
    history = st.session_state.get("search_history", [])
    new_entry = {
        "keyword": keyword,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "result_count": len(filtered) if not filtered.empty else 0,
        "timestamp": datetime.now().strftime("%m-%d %H:%M"),
        "filter_signature": filter_signature,
    }
    if not history or history[-1].get("filter_signature") != filter_signature:
        history.append(new_entry)
        if len(history) > 20:
            history = history[-20:]
        st.session_state["search_history"] = history
    st.session_state["_last_history_save"] = now

if filtered.empty:
    st.info("当前筛选条件下没有命中文章。")
    st.stop()

st.caption(f"命中文章：{len(filtered):,} 篇")

st.subheader("传播时间轴")

filtered["pub_day"] = filtered["dtime"].dt.date
by_day_series = filtered.groupby("pub_day").size()
full_days = pd.date_range(start_date, end_date, freq="D")
days = [d.strftime("%Y-%m-%d") for d in full_days]
counts = [int(by_day_series.get(d.date(), 0)) for d in full_days]
count_by_day_str = dict(zip(days, counts))

# 构建每日文章列表（用于tooltip悬浮展示）
day_articles_map: Dict[str, str] = {}
for d in full_days:
    day_df = filtered[filtered["pub_day"] == d.date()]
    if day_df.empty:
        continue
    top = day_df.sort_values("read_count", ascending=False).head(5)
    lines = []
    for _, r in top.iterrows():
        title = str(r.get("title", "") or "")[:40]
        author = str(r.get("author", "") or "")
        rc = format_read_count(r.get("read_count", 0))
        tm = format_time_hhmm(r.get("dtime"))
        parts = [title]
        if author:
            parts.append(author)
        parts.append(f"{rc}阅读")
        if tm:
            parts.append(tm)
        lines.append(" · ".join(parts))
    day_articles_map[d.strftime("%Y-%m-%d")] = "<br/>".join(lines)

timeline_option = build_timeline_option(days, counts, event_nodes, day_articles_map)

events = {"click": "function(params){return params.name;}"}
clicked = st_echarts(
    timeline_option,
    events=events,
    height="350px",
    key="timeline",
)
st.caption("💡 点击数据点查看当日文章列表")

clicked_day = extract_clicked_date(clicked)
if clicked_day:
    try:
        clicked_day_str = str(clicked_day)
        clicked_count = int(count_by_day_str.get(clicked_day_str, 0))
        if clicked_count > 0:
            clicked_date = pd.to_datetime(clicked_day_str).date()
            prev_day = st.session_state.get("selected_day")
            if prev_day == clicked_date and st.session_state.get("daily_expanded", False):
                st.session_state["daily_expanded"] = False
            else:
                st.session_state["selected_day"] = clicked_date
                st.session_state["daily_expanded"] = True
        else:
            st.session_state["daily_expanded"] = False
    except Exception:
        pass

selected_day = st.session_state.get("selected_day")
if selected_day and selected_day.isoformat() not in count_by_day_str:
    st.session_state["selected_day"] = None
    st.session_state["daily_expanded"] = False

if st.session_state.get("daily_expanded", False) and st.session_state.get("selected_day"):
    selected_day = st.session_state["selected_day"]
    day_df = filtered[filtered["pub_day"] == selected_day].copy()
    day_df = day_df.sort_values("read_count", ascending=False)
    if day_df.empty:
        st.session_state["daily_expanded"] = False
    else:
        st.markdown(render_daily_hot_list(day_df, selected_day), unsafe_allow_html=True)

st.subheader("区间热点观点（阅读量Top10）")
top10 = filtered.sort_values("read_count", ascending=False).head(10)
if top10.empty:
    st.info("当前区间没有可用于观点提炼的文章。")
else:
    rows = top10[[
        "title",
        "source",
        "read_count",
        "keysentence",
        "summary",
        "content",
        "url",
        "dtime",
    ]].to_dict(orient="records")

    current_vp_key, _ = viewpoint_cache_key(base_url, model, rows)

    generate = st.button("生成观点总结（调用大模型）")
    if generate:
        if not api_key:
            st.error("缺少 API Key：请设置环境变量 DEEPSEEK_API_KEY，或在 local_settings.py 中配置。")
        else:
            with st.spinner("正在调用大模型生成观点总结..."):
                result, status = call_deepseek(api_key, base_url, model, rows)
            if status in ("ok", "cached") and isinstance(result, list):
                st.session_state["viewpoint_display_key"] = current_vp_key
                st.success("观点总结已生成。")
            else:
                st.warning("大模型返回结果不是合法 JSON 数组，已展示原始内容：")
                st.code(status)

    display_key = st.session_state.get("viewpoint_display_key")
    cache = st.session_state.get("viewpoint_cache", {})
    if display_key == current_vp_key and current_vp_key in cache and isinstance(cache[current_vp_key], list):
        st.markdown(render_viewpoint_clusters(cache[current_vp_key]), unsafe_allow_html=True)
    elif display_key and display_key != current_vp_key:
        st.info("筛选条件已变化，请重新生成观点总结。")
    else:
        st.caption("点击上方按钮后，将在此处展示Top10观点列表。")

st.subheader("热点文章（阅读量Top3）")
wall = filtered.sort_values("read_count", ascending=False).head(3)
if wall.empty:
    st.info("当前筛选条件下没有可展示的文章。")
else:
    cols = st.columns(min(3, len(wall)))
    for idx, (_, row) in enumerate(wall.iterrows()):
        with cols[idx % 3]:
            st.markdown(render_wall_card(row), unsafe_allow_html=True)

st.subheader("关联标的走势")

# 优先使用LLM意图识别输出的Industry_asset，降级使用数据的scode_list
industry_assets: List[str] = []
ir = st.session_state.get("intent_result")
if ir:
    raw_assets = ir.get("Industry_asset", [])
    if isinstance(raw_assets, list) and raw_assets:
        industry_assets = [str(a).strip() for a in raw_assets if str(a).strip()]

if industry_assets:
    # 使用LLM识别的关联标的（板块/个股/大宗商品/期货等）
    labels = industry_assets
    codes = industry_assets  # mock_ohlc 接受任意字符串作为种子
elif "scode_list" in filtered.columns and not filtered["scode_list"].dropna().empty:
    # 降级：使用数据中的股票代码
    codes = top_stock_codes(filtered["scode_list"], top_k=5)
    if not codes:
        st.info("未识别到关联标的。")
        st.stop()
    name_map = {}
    if "sname_list" in filtered.columns and filtered["sname_list"].dropna().any():
        name_map = parse_stock_name_map(filtered["sname_list"])
    labels = []
    used: Counter[str] = Counter()
    for code in codes:
        base = stock_label(code, name_map)
        used[base] += 1
        labels.append(base if used[base] == 1 else f"{base}（{code}）")
else:
    st.info("未识别到关联标的。")
    st.stop()

if len(codes) == 1:
    dates, ohlc = mock_ohlc(codes[0], start_date, end_date)
    if not dates:
        st.info("当前区间没有可用的交易日数据。")
    else:
        kline_option = build_kline_option(dates, ohlc, event_nodes)
        st_echarts(kline_option, height="350px", key="kline_single")
else:
    tabs = st.tabs(labels)
    for tab, code in zip(tabs, codes):
        with tab:
            dates, ohlc = mock_ohlc(code, start_date, end_date)
            if not dates:
                st.info("当前区间没有可用的交易日数据。")
            else:
                kline_option = build_kline_option(dates, ohlc, event_nodes)
                key = hashlib.sha1(f"{filter_signature}|{code}".encode("utf-8")).hexdigest()[:12]
                st_echarts(kline_option, height="350px", key=f"kline_{key}")
