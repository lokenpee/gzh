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

import numpy as np
import pandas as pd
import streamlit as st
from openai import OpenAI


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
        match = safe_contains(df["title"], kw)
        match |= safe_contains(df["keywords"], kw)
        match |= safe_contains(df["keyword"], kw)
        match |= safe_contains(df["content"], kw)
        mask &= match

    return df.loc[mask].copy()


def build_timeline_option(
    days: List[str],
    counts: List[int],
    event_nodes: List[Dict[str, str]],
) -> Dict[str, Any]:
    mark_lines = [
        {"xAxis": node["date"], "name": f"{node['label']}\n{node['date'][5:]}"}
        for node in event_nodes
    ]
    option: Dict[str, Any] = {
        "tooltip": {
            "trigger": "axis",
            "formatter": "function(params){var p=params&&params[0];if(!p){return '';}return p.axisValue + '<br/>文章数：' + p.data;}",
        },
        "xAxis": {
            "type": "category",
            "data": days,
            "axisLabel": {
                "rotate": 45,
                "formatter": "function(value){return String(value).slice(5);}",
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
                "data": counts,
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
                    "formatter": "function(params){return params.data>0?params.data:'';}"
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
        "你是一位金融资讯分析师。请对以下阅读量最高的10篇文章进行观点提炼。",
        "",
        "输入格式（每篇文章）：",
        "- 标题：{TITLE}",
        "- 来源：{SOURCE}",
        "- 阅读量：{READ_COUNT}",
        "- 关键句：{KEYSENTENCE}",
        "- 摘要：{SUMMARY}",
        "- 正文前2000字：{CONTENT}",
        "",
        "要求：",
        "1. 每篇文章提炼一个核心观点，不超过30字。",
        "2. 观点必须基于文章明确表达的判断，严禁编造。",
        "3. 如果文章只是新闻报道、没有明确观点，标注为\"事件报道，无明显观点\"。",
        "4. 输出格式为JSON数组，每项包含：title（标题）、source（来源）、read_count（阅读量）、viewpoint（观点，≤30字）、url（链接）。",
        "",
        "输出：",
        "[{\"title\":\"...\",\"source\":\"...\",\"read_count\":12345,\"viewpoint\":\"...\",\"url\":\"...\"}, ...]",
        "",
    ]
    for row in rows:
        content = str(row.get("content", ""))[:2000]
        parts.append("---")
        parts.append(f"标题：{row.get('title','')}")
        parts.append(f"来源：{row.get('source','')}")
        parts.append(f"阅读量：{row.get('read_count','')}")
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
        "graphic": [
            {
                "type": "text",
                "left": "center",
                "top": "middle",
                "style": {
                    "text": "行情数据为模拟数据，正式上线后对接同花顺/Wind API",
                    "fill": "rgba(0,0,0,0.15)",
                    "fontSize": 14,
                    "fontWeight": 600,
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
        read_count = format_read_count(row.get("read_count", 0))
        time_str = format_time_hhmm(row.get("dtime"))
        url = str(row.get("url", "") or "").strip()
        if url:
            url_attr = html.escape(url, quote=True)
            title_html = f'<a class="daily-title" href="{url_attr}" target="_blank" rel="noopener noreferrer">{title}</a>'
        else:
            title_html = f'<span class="daily-title">{title}</span>'

        rows_html.append(
            """
<div class="daily-row">
  <div class="daily-rank" style="color:{rank_color};">{rank}</div>
  <div class="daily-main">
    {title_html}
    <div class="daily-source">{source}</div>
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
                source=source,
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


def render_viewpoint_list(items: List[Dict[str, Any]]) -> str:
    rows_html: List[str] = []
    for idx, item in enumerate(items):
        rank = idx + 1
        rank_color = "#d4a855" if rank <= 3 else "#9ca3af"
        title = html.escape(str(item.get("title", "") or ""))
        source = html.escape(str(item.get("source", "") or ""))
        read_count = format_read_count(item.get("read_count", 0))
        viewpoint = html.escape(str(item.get("viewpoint", "") or ""))
        url = str(item.get("url", "") or "").strip()
        if url:
            url_attr = html.escape(url, quote=True)
            title_html = f'<a class="vp-title" href="{url_attr}" target="_blank" rel="noopener noreferrer">{title}</a>'
            link_html = f'<a href="{url_attr}" target="_blank" rel="noopener noreferrer">阅读原文</a>'
        else:
            title_html = f'<span class="vp-title">{title}</span>'
            link_html = ""

        rows_html.append(
            """
<div class="vp-row">
  <div class="vp-rank" style="color:{rank_color};">{rank}</div>
  <div class="vp-body">
    {title_html}
    <div class="vp-meta">{source} · 阅读 {read_count}</div>
    <div class="vp-text">{viewpoint}</div>
  </div>
  <div class="vp-link">{link_html}</div>
</div>
""".format(
                rank=rank,
                rank_color=rank_color,
                title_html=title_html,
                source=source,
                read_count=read_count,
                viewpoint=viewpoint,
                link_html=link_html,
            )
        )

    return (
        '<div class="hr-card">'
        '<div class="hr-card-title">区间热点观点（阅读量Top10）</div>'
        f'<div class="vp-list">{"".join(rows_html)}</div>'
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


st.set_page_config(page_title="热点复盘 Demo", layout="wide")

root = Path.cwd()
excel_files = find_excel_files(root)

st.title("热点复盘功能重构 Demo")
inject_css()

if not excel_files:
    st.warning("未在项目根目录找到 Excel（.xlsx）文件。请把模拟数据放到当前目录。")
    st.stop()

with st.sidebar:
    st.header("数据源（Demo）")
    file_names = [p.name for p in excel_files]
    selected_file = st.selectbox("Excel 文件", file_names, index=0)
    selected_path = root / selected_file

    xls = pd.ExcelFile(selected_path)
    sheet_name = st.selectbox("Sheet", xls.sheet_names, index=0)

    st.header("大模型（观点提炼）")
    base_url = st.text_input("Base URL", value="https://api.deepseek.com/v1")
    model = st.text_input("Model", value="deepseek-chat")
    api_key_env = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    api_key = st.text_input(
        "API Key（建议用环境变量 DEEPSEEK_API_KEY）",
        value=api_key_env,
        type="password",
    )

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
    date_range = st.date_input(
        "时间区间",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        key=f"date_range|{selected_file}|{sheet_name}",
    )
with top_right:
    keyword = st.text_input(
        "事件关键词",
        value="",
        key=f"keyword|{selected_file}|{sheet_name}",
    )

with st.expander("事件关键节点（可选）", expanded=False):
    event_text = st.text_area(
        "每行一个：YYYY-MM-DD | 节点名称",
        value="",
        height=120,
        key=f"event_nodes|{selected_file}|{sheet_name}",
    )

start_date, end_date = date_range
if start_date > end_date:
    st.error("时间区间不合法：开始日期不能晚于结束日期。")
    st.stop()

filter_signature = hashlib.sha1(
    f"{selected_file}|{sheet_name}|{start_date}|{end_date}|{keyword}".encode("utf-8")
).hexdigest()
if st.session_state.get("filter_signature") != filter_signature:
    st.session_state["filter_signature"] = filter_signature
    st.session_state["selected_day"] = None
    st.session_state["daily_expanded"] = False
    st.session_state["wall_limit"] = 20
    st.session_state["viewpoint_display_key"] = None

event_nodes = parse_event_nodes(event_text)
filtered = filter_dataframe(df, start_date, end_date, keyword)

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

timeline_option = build_timeline_option(days, counts, event_nodes)

events = {"click": "function(params){return params.name;}"}
clicked = st_echarts(
    timeline_option,
    events=events,
    height="350px",
    key="timeline",
)

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
    ]].to_dict(orient="records")

    current_vp_key, _ = viewpoint_cache_key(base_url, model, rows)

    generate = st.button("生成观点总结（调用大模型）")
    if generate:
        if not api_key:
            st.error("缺少 API Key：请设置环境变量 DEEPSEEK_API_KEY，或在侧边栏粘贴。")
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
        st.markdown(render_viewpoint_list(cache[current_vp_key]), unsafe_allow_html=True)
    elif display_key and display_key != current_vp_key:
        st.info("筛选条件已变化，请重新生成观点总结。")
    else:
        st.caption("点击上方按钮后，将在此处展示Top10观点列表。")

st.subheader("热点文章（阅读量Top100）")
wall = filtered.sort_values("read_count", ascending=False).head(100)
if wall.empty:
    st.info("当前筛选条件下没有可展示的文章。")
else:
    wall_limit = int(st.session_state.get("wall_limit", 20))
    wall_limit = max(1, min(wall_limit, len(wall)))
    wall_show = wall.head(wall_limit)

    cols = st.columns(3)
    for idx, (_, row) in enumerate(wall_show.iterrows()):
        with cols[idx % 3]:
            st.markdown(render_wall_card(row), unsafe_allow_html=True)

    if wall_limit < len(wall):
        if st.button("加载更多"):
            st.session_state["wall_limit"] = min(wall_limit + 20, len(wall))

st.subheader("关联标的走势")
if "scode_list" not in filtered.columns or filtered["scode_list"].dropna().empty:
    st.info("未识别到关联标的（scode_list 为空）。")
else:
    codes = top_stock_codes(filtered["scode_list"], top_k=5)
    if not codes:
        st.info("未识别到关联标的（scode_list 为空）。")
    else:
        name_map = {}
        if "sname_list" in filtered.columns and filtered["sname_list"].dropna().any():
            name_map = parse_stock_name_map(filtered["sname_list"])

        labels: List[str] = []
        used: Counter[str] = Counter()
        for code in codes:
            base = stock_label(code, name_map)
            used[base] += 1
            labels.append(base if used[base] == 1 else f"{base}（{code}）")

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
