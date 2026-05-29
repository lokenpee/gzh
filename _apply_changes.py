import pathlib

path = pathlib.Path('app.py')
content = path.read_text('utf-8')

# ===== 1. 移除"模拟数据" UI =====
content = content.replace('行情数据为模拟数据，正式上线后对接行情API', '行情数据仅供参考')
content = content.replace('未在项目根目录找到 Excel（.xlsx）文件。请把模拟数据放到当前目录。', '未找到 Excel 数据文件，请上传至项目根目录。')
content = content.replace('关联标的来自模拟数据统计（未经过LLM识别）。', '关联标的来自文章数据统计。')

# ===== 2. 时间轴每日热文 =====
content = content.replace('day_df.head(20).iterrows()', 'day_df.iterrows()')
content = content.replace("f'<div class=\"card-foot\">仅展示前20篇</div>'", '""')

# ===== 3. 页面标题去掉Demo =====
content = content.replace('page_title="热点复盘 Demo"', 'page_title="热点复盘"')
content = content.replace('st.title("热点复盘功能重构 Demo")', 'st.title("热点复盘")')

# ===== 4. 默认模式：去掉force_default_mode =====
old_default = '''event_keyword = event_keyword_input.strip()
force_default_mode = False
if not event_keyword:
    force_default_mode = True
    event_keyword = default_keyword
    if event_keyword:
        st.caption(f"当前关键词：{event_keyword}（默认）")'''
new_default = '''event_keyword = event_keyword_input.strip()
if not event_keyword:
    event_keyword = default_keyword
    if event_keyword:
        st.caption(f"当前关键词：{event_keyword}（默认）")'''
content = content.replace(old_default, new_default)

old_fdm = '''        if force_default_mode:
            demo_fallback = True
            intent = build_default_intent(event_keyword, start_date, end_date)
            st.session_state["intent_result"] = intent
            st.session_state["intent_signature"] = intent_signature
            intent_cache[intent_signature] = intent
            st.session_state["demo_mode"] = True
            run_log.append("默认展示模式：跳过联网检索与意图识别")
        elif (missing_search or missing_llm) and event_keyword == default_keyword:'''
new_fdm = '''        if (missing_search or missing_llm) and event_keyword == default_keyword:'''
content = content.replace(old_fdm, new_fdm)

# ===== 5. build_search_query 去掉"事件""关键节点" =====
old_sq = '''def build_search_query(event_keyword: str, start_date: date, end_date: date) -> str:
    parts = [
        event_keyword,
        "事件",
        "关键节点",
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
    ]
    return " ".join([p for p in parts if p])'''
new_sq = '''def build_search_query(event_keyword: str, start_date: date, end_date: date) -> str:
    parts = [
        event_keyword,
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
    ]
    return " ".join([p for p in parts if p])'''
content = content.replace(old_sq, new_sq)

# ===== 6. QnAIGC time_filter =====
old_tf = '''            elif provider == "qnaigc":
                time_filter = get_setting("SEARCH_TIME_FILTER", "").strip()
                site_filter = parse_site_filter(get_setting("SEARCH_SITE_FILTER", ""))'''
new_tf = '''            elif provider == "qnaigc":
                time_filter = f"{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
                site_filter = parse_site_filter(get_setting("SEARCH_SITE_FILTER", ""))'''
content = content.replace(old_tf, new_tf)

# ===== 7. Embedding/LLM spinners =====
old_sp = '''keyword_count = len(filtered)
embed_cfg = load_embedding_config()
filtered, embed_meta = embedding_coarse_filter(filtered, intent, embed_cfg)
embed_count = len(filtered)
filtered, llm_meta = llm_fine_filter(filtered, intent, base_url, model, api_key)
final_count = len(filtered)'''
new_sp = '''keyword_count = len(filtered)
embed_cfg = load_embedding_config()
with st.spinner(f"正在语义粗筛（Embedding）... 候选 {keyword_count} 篇"):
    filtered, embed_meta = embedding_coarse_filter(filtered, intent, embed_cfg)
embed_count = len(filtered)
with st.spinner(f"正在LLM精筛... 候选 {embed_count} 篇"):
    filtered, llm_meta = llm_fine_filter(filtered, intent, base_url, model, api_key)
final_count = len(filtered)'''
content = content.replace(old_sp, new_sp)

# ===== 8. 意图识别prompt重写 =====
old_ip = '''    lines = [
        "你是金融研究助理。请只根据提供的联网检索结果抽取事件信息。",
        "禁止使用模型记忆或常识推断。",
        "输出必须是合法JSON数组（候选列表），不要添加Markdown代码块。",
        "如只有一种理解，也必须返回数组，长度为1。",
        "最多返回3个候选，按置信度从高到低排序。",
        "字段要求：",
        "- event_name: 规范化事件名，不超过30字",
        "- event_entities: 1-3个核心实体词，按重要性排序，必须包含用户关键词中的核心名词",
        "- key_points: 最多5个，包含node_name和time(yyyy-MM-dd)",
        "- Industry_asset: 事件关联A股板块或标的名称数组",
        "- search_window: {{start,end}}，等于输入时间区间",
        "- confidence: 0-1之间的小数，表示该候选是正确事件理解的置信度，仅依据本次检索结果判断",
        "时间区间：{start} ~ {end}",
        "用户原始关键词：{keyword}",
        "联网检索结果：",
    ]'''
new_ip = '''    lines = [
        "你是一名资深金融事件分析师。请根据联网检索结果，结合你的金融领域知识，深度理解该事件。",
        "输出必须是合法JSON数组（候选列表），不要添加Markdown代码块。",
        "如只有一种理解，也必须返回数组，长度为1。",
        "最多返回3个候选，按置信度从高到低排序。",
        "字段要求：",
        "",
        "- event_name: 规范化事件名。格式：核心主体 + 关键动作 + 对市场/行业的影响。不超过30字。",
        "  示例：'海湖庄园协议签署引发中美关税升级与全球供应链震荡'",
        "  而不是：'海湖庄园协议引发全球热议'（太泛，没有具体影响）",
        "",
        "- event_entities: 1-3个实体词，将用于数据库全文检索（AND匹配）筛选文章。",
        "  核心判据：每个实体必须是在【所有】讨论该事件的文章中都必然出现的关键词。",
        "  - 如果某个词只在部分文章中出现（如具体人名、细节术语），不要输出。",
        "  - 如果1个词（如'韬定律'）已经足够唯一地锁定该事件，就只输出1个。",
        "  - 如果1个词过于宽泛（如'GPT'会命中大量无关文章），则需加上限定词（如'GPT'+'6.0'）。",
        "",
        "- Industry_asset: 事件关联的可交易标的，必须是能在金融终端检索到的品种。可选类型：A股板块（如'半导体''稀土永磁'）、个股（如'中芯国际'）、大宗商品（如'沪铜''原油'）、期货/外汇（如'离岸人民币''COMEX黄金'）。",
        "  基于检索内容推断事件对各类资产的影响方向，不限于字面提及的标的。如无法确定具体个股，至少输出受影响的板块。如果确实无法识别则输出空数组。",
        "",
        "- key_points: 最多5个关键时间节点，每个包含node_name和time(yyyy-MM-dd)。",
        "  质量要求：每个节点必须与事件有【直接因果或时序关系】。如果某件事只是同期发生但与事件无关，不要收录。",
        "  示例：'2025-04-02 美国宣布对华加征34%关税'（直接因果）✓ ；'2025-03-27 某券商发布研报'（仅是评论，非事件节点）✗",
        "",
        "- search_window: {{start,end}}，等于输入时间区间",
        "- confidence: 0-1之间的小数，表示该候选是正确事件理解的置信度",
        "",
        "时间区间：{start} ~ {end}",
        "用户原始关键词：{keyword}",
        "联网检索结果：",
    ]'''
content = content.replace(old_ip, new_ip)

# ===== 9. build_keypoints_prompt =====
old_kp = '    return header + "\\n\\n" + "\\n\\n".join(items)\n\n\ndef extract_json_payload'
new_kp = '''    return header + "\\n\\n" + "\\n\\n".join(items)


def build_keypoints_prompt(event_name, search_results):
    lines = [
        "你是一名金融事件时间线分析师。请根据联网检索结果，提取该事件的关键时间节点。",
        "要求：",
        "1. 每个节点必须是该事件发展中具有标志性意义的时间点（政策发布、关键会议、官方表态、重大市场反应等）。",
        "2. 节点之间应有因果或时序关联，构成完整的事件脉络。",
        "3. 最多5个节点，按时间升序排列。",
        "4. 不要收录仅是媒体评论、券商研报发布、转发报道的日期——这些不是事件本身。",
        "输出JSON对象，不含Markdown代码块：",
        '{"key_points": [{"node_name": "美国宣布对华加征34%关税", "time": "2025-04-02"}, ...]}',
        f"事件名称：{event_name}",
        "联网检索结果：",
    ]
    header = "\\n".join(lines)
    items = []
    for idx, item in enumerate(search_results, start=1):
        items.append("#{idx}\\n标题：{title}\\n摘要：{snippet}\\n链接：{url}".format(
            idx=idx, title=item.title, snippet=item.snippet, url=item.url))
    return header + "\\n\\n" + "\\n\\n".join(items)


def extract_json_payload'''
content = content.replace(old_kp, new_kp)

# ===== 10. parse_keypoints =====
old_pk = '''        confidence=confidence,
    )


def parse_intent_candidates'''
new_pk = '''        confidence=confidence,
    )


def parse_keypoints(payload):
    items = payload.get("key_points", []) if isinstance(payload.get("key_points", []), list) else []
    key_points = []
    for item in items:
        if not isinstance(item, dict):
            continue
        node_name = str(item.get("node_name") or item.get("label") or "").strip()
        time_raw = item.get("time") or item.get("date")
        time_str = normalize_date_str(time_raw)
        if node_name and time_str:
            key_points.append({"node_name": node_name, "time": time_str})
    return key_points[:5]


def parse_intent_candidates'''
content = content.replace(old_pk, new_pk)

# ===== 11. 二阶段检索 enrichment block =====
old_en = 'if not intent:\n    st.info("请在上方输入关键词并点击“开始复盘”。")\n    st.stop()\n\nst.subheader("事件概况")'
new_en = '''if not intent:
    st.info("请在上方输入关键词并点击“开始复盘”。")
    st.stop()

kp_flag = f"kp_enriched_{intent_signature}"
if intent.event_name and search_key and api_key and not st.session_state.get(kp_flag):
    with st.spinner("正在检索事件关键时间线..."):
        timeline_query = f"{intent.event_name} 关键时间节点 事件始末"
        if provider == "qnaigc":
            time_filter_q = f"{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
            site_filter_q = parse_site_filter(get_setting("SEARCH_SITE_FILTER", ""))
            tl_results, tl_status = search_qnaigc(
                timeline_query, search_key, search_endpoint, search_limit, time_filter_q, site_filter_q)
        elif provider == "tavily":
            tl_results, tl_status = search_tavily(timeline_query, search_key, search_limit)
        elif provider == "serpapi":
            tl_results, tl_status = search_serpapi(timeline_query, search_key, search_limit)
        elif provider == "searxng":
            tl_results, tl_status = search_searxng(timeline_query, search_endpoint, search_limit)
        else:
            tl_results, tl_status = search_bing(timeline_query, search_key, search_limit)
    if tl_status == "ok" and tl_results:
        tl_prompt = build_keypoints_prompt(intent.event_name, tl_results)
        tl_payload, tl_llm_status = call_intent_llm(api_key, base_url, model, tl_prompt)
        if tl_llm_status == "ok" and isinstance(tl_payload, dict):
            new_kps = parse_keypoints(tl_payload)
            if new_kps:
                intent.key_points = new_kps
                intent_cache[intent_signature] = intent
                st.session_state["intent_result"] = intent
                last_log = st.session_state.get("last_run_log", [])
                last_log.append(f"时间线检索: {len(tl_results)}条, 提取{len(new_kps)}个关键节点")
                st.session_state["last_run_log"] = last_log
    st.session_state[kp_flag] = True

st.subheader("事件概况")'''
content = content.replace(old_en, new_en)

path.write_text(content, 'utf-8')
print('All changes applied successfully')

# Quick verification
v = path.read_text('utf-8')
checks = [
    ('force_default_mode', 'force_default_mode' in v, False),
    ('build_keypoints_prompt', 'def build_keypoints_prompt' in v, True),
    ('parse_keypoints', 'def parse_keypoints' in v, True),
    ('kp_enriched', 'kp_enriched' in v, True),
]
for name, result, expected in checks:
    status = 'OK' if result == expected else 'FAIL'
    print(f'[{status}] {name}')
