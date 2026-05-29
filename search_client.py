from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import requests


def load_search_config() -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "provider": "",
        "api_key": "",
        "endpoint": "",
        "limit": 10,
        "enabled": False,
    }
    try:
        import local_settings
        for key in ("SEARCH_PROVIDER", "SEARCH_API_KEY", "SEARCH_ENDPOINT", "SEARCH_LIMIT"):
            if hasattr(local_settings, key):
                val = getattr(local_settings, key)
                if val is not None and val != "":
                    config[key.lower().replace("search_", "")] = val
    except ImportError:
        pass

    config["enabled"] = bool(config["provider"] and config["api_key"])
    return config


def _parse_qnaigc_results(data: dict) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    items = data.get("data", data.get("results", data.get("items", [])))
    if isinstance(items, dict):
        items = items.get("items", items.get("results", []))
    if not isinstance(items, list):
        return results
    for item in items:
        results.append({
            "title": str(item.get("title", "") or ""),
            "snippet": str(item.get("snippet", "") or item.get("summary", "") or ""),
            "url": str(item.get("url", "") or item.get("link", "") or ""),
            "content": str(item.get("content", "") or item.get("body", "") or ""),
        })
    return results


def _parse_tavily_results(data: dict) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    items = data.get("results", [])
    if not isinstance(items, list):
        return results
    for item in items:
        results.append({
            "title": str(item.get("title", "") or ""),
            "snippet": str(item.get("content", "") or ""),
            "url": str(item.get("url", "") or ""),
            "content": str(item.get("content", "") or item.get("raw_content", "") or ""),
        })
    return results


def web_search(
    query: str,
    config: Optional[Dict[str, Any]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Tuple[List[Dict[str, str]], str]:
    if config is None:
        config = load_search_config()

    if not config.get("enabled"):
        return [], "搜索未配置"

    provider = config["provider"]
    api_key = config["api_key"]
    endpoint = config.get("endpoint", "")
    limit = int(config.get("limit", 10))

    if provider == "qnaigc":
        try:
            payload: Dict[str, Any] = {"q": query, "num": limit}
            if start_date and end_date:
                payload["time_filter"] = f"{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
            resp = requests.post(
                f"{endpoint}/search",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            results = _parse_qnaigc_results(resp.json())
            return results, "ok"
        except Exception as exc:
            return [], str(exc)

    if provider == "tavily":
        try:
            payload: Dict[str, Any] = {
                "api_key": api_key,
                "query": query,
                "max_results": limit,
            }
            if start_date:
                payload["start_date"] = start_date.strftime("%Y-%m-%d")
            if end_date:
                payload["end_date"] = end_date.strftime("%Y-%m-%d")
            resp = requests.post(
                "https://api.tavily.com/search",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            results = _parse_tavily_results(resp.json())
            return results, "ok"
        except Exception as exc:
            return [], str(exc)

    return [], f"不支持的搜索提供商：{provider}"


def count_entity_hits(entity: str, search_results: List[Dict[str, str]]) -> int:
    entity_lower = entity.lower()
    hits = 0
    for item in search_results:
        title = (item.get("title", "") or "").lower()
        snippet = (item.get("snippet", "") or "").lower()
        content = (item.get("content", "") or "").lower()
        combined = f"{title} {snippet} {content}"
        if entity_lower in combined:
            hits += 1
    return hits


def filter_entities_by_confidence(
    entities: List[str],
    search_results: List[Dict[str, str]],
    threshold: float = 0.8,
) -> List[str]:
    if not search_results:
        return entities[:3]

    total = len(search_results)
    if total == 0:
        return entities[:3]

    scored: List[Tuple[str, float]] = []
    for entity in entities:
        hits = count_entity_hits(entity, search_results)
        score = hits / total
        scored.append((entity, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    qualified = [e for e, s in scored if s >= threshold]

    if not qualified:
        qualified = [scored[0][0]]

    return qualified[:3]
