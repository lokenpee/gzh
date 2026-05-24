# 热点复盘功能重构 PRD

---

## 一、项目背景与目标

### 1.1 背景
现有热点复盘功能基于知丘数据，通过关键词命中得分呈现高分公众号与文章列表。该模式为静态检索结果聚合，仅回答"谁分高"，无法回答"热度怎么发酵的、每天发生了什么、最火的文章说了什么"。对于投研管理场景，需要更直观地看到事件在时间轴上的传播全貌。

### 1.2 目标
将热点复盘从静态排行榜升级为事件发酵追踪工具，核心解决四个问题：

1. **热度走势**：事件在选定区间内，每天有多少文章？热度是起来了还是下去了？
2. **每日热点**：聚焦每一天，当天阅读量最高的文章是什么？
3. **核心观点**：整个区间内，阅读量最高的10篇文章，各自表达了什么判断？
4. **市场反应**：事件关联的标的，这段时间走势如何？

---

## 二、数据环境

### 2.1 数据来源
数据来自知丘通过UTS推送至西部本地的Oracle数据库，包含公众号文章全量数据。开发团队通过SQL从本地表查询数据构建业务接口，产品经理可在西部现场直接执行SQL验证数据逻辑。

推送范围：从甲方购买产品之日起开始推送，推送全量公众号文章数据。

### 2.2 核心数据表

#### CJ_NEWS（公众号舆情主表）

| 字段 | 类型 | 说明 | 本方案使用场景 |
|------|------|------|--------------|
| ID | NUMBER(38) | 文章唯一ID | 全链路主键 |
| TITLE | VARCHAR2(360) | 文章标题 | 展示、筛选、大模型输入 |
| URL | VARCHAR2(600) | 原文链接 | 点击跳转 |
| DTIME | DATE | 文章发布时间 | 时间轴聚合、日榜筛选 |
| SOURCE | VARCHAR2(50) | 公众号名称 | 展示来源 |
| AUTHOR | VARCHAR2(50) | 作者 | 展示 |
| READ_COUNT | INTEGER | 阅读量（默认0） | **核心排序字段** |
| KEYWORD | VARCHAR2(100) | 资讯关键词 | SQL筛选匹配 |
| KEYWORDS | VARCHAR2(200) | Python自动提取的关键词列表 | SQL筛选匹配 |
| KEYSENTENCE | VARCHAR2(1000) | Python自动提取的关键句/摘要 | 大模型辅助输入 |
| SUMMARY | VARCHAR2(2000) | 识别的核心摘要 | 大模型辅助输入 |
| SEARCHTEXT | VARCHAR2(500) | 文章关键句 | 大模型辅助输入 |
| SCODE_LIST | VARCHAR2(150) | 关联股票代码列表，如"sh600000,sz000001" | 标的提取 |
| SNAME_LIST | VARCHAR2(150) | 关联股票名称列表，如"600000(NAME),000001(NAME)" | 标的展示 |
| STATUS | NUMBER(2) | 数据状态：-1直接插入 / 0人工修改 / -2人工分类 / -3待删除 | SQL过滤条件（取≥0） |
| CJ_TYPE | INTEGER | 2原创 / 1资讯聚合 / 3期货原创 | 本方案不使用 |

#### CJ_NEWS_CONTENT（微信内容表）

| 字段 | 类型 | 说明 | 本方案使用场景 |
|------|------|------|--------------|
| NEWS_ID | INTEGER | 对应CJ_NEWS.ID | JOIN键 |
| CONTENT | CLOB | 文章正文全文 | 大模型观点提炼输入 |

> **注意**：`CJ_NEWS` 表中不存在 `like_count`（点赞数）字段。如后续需要该数据，需确认知丘是否推送或从其他渠道获取。

### 2.3 数据查询SQL

```sql
SELECT 
    n.ID AS news_id,
    n.TITLE AS title,
    c.CONTENT AS content,
    n.URL AS url,
    n.DTIME AS dtime,
    n.SOURCE AS source,
    n.AUTHOR AS author,
    n.READ_COUNT AS read_count,
    n.KEYWORDS AS keywords,
    n.KEYSENTENCE AS keysentence,
    n.SUMMARY AS summary,
    n.SEARCHTEXT AS searchtext,
    n.SCODE_LIST AS scode_list,
    n.SNAME_LIST AS sname_list
FROM CJ_NEWS n
LEFT JOIN CJ_NEWS_CONTENT c ON n.ID = c.NEWS_ID
WHERE n.DTIME BETWEEN TO_DATE(:start_date, 'YYYY-MM-DD') 
                  AND TO_DATE(:end_date, 'YYYY-MM-DD')
  AND n.STATUS >= 0
  AND (
      n.TITLE LIKE '%' || :event_keyword || '%'
      OR n.KEYWORDS LIKE '%' || :event_keyword || '%'
      OR n.KEYWORD LIKE '%' || :event_keyword || '%'
      OR c.CONTENT LIKE '%' || :event_keyword || '%'
  )
ORDER BY n.DTIME ASC;
```

### 2.4 Demo阶段模拟数据

Demo阶段使用Excel模拟数据，字段严格对齐上述真实表结构：

| Excel列 | 字段名 | 对应真实表字段 |
|---------|--------|---------------|
| A | news_id | CJ_NEWS.ID |
| B | content | CJ_NEWS_CONTENT.CONTENT |
| C | title | CJ_NEWS.TITLE |
| D | url | CJ_NEWS.URL |
| E | dtime | CJ_NEWS.DTIME |
| F | source | CJ_NEWS.SOURCE |
| G | author | CJ_NEWS.AUTHOR |
| H | read_count | CJ_NEWS.READ_COUNT |
| I | keywords | CJ_NEWS.KEYWORDS |
| J | keysentence | CJ_NEWS.KEYSENTENCE |
| K | summary | CJ_NEWS.SUMMARY |
| L | scode_list | CJ_NEWS.SCODE_LIST |
| M | sname_list | CJ_NEWS.SNAME_LIST |

建议模拟50-100条数据，覆盖7-15天，体现热度起伏。

---

## 三、功能模块

### 3.1 传播时间轴

#### 功能定义
展示选定时间区间内，每天与该事件相关的文章数量折线，直观反映事件发酵程度——哪天爆了、哪天退了、整体趋势如何。

#### 数据来源

| 展示项 | 来源表.字段 | 计算方式 |
|--------|------------|---------|
| 每日文章数 | CJ_NEWS.DTIME | 按日期（yyyy-MM-dd）分组，COUNT(CJ_NEWS.ID) |
| 关键节点 | 用户输入 / 大模型+联网搜索提取 | 事件关键时间点，如"4月2日特朗普宣布加征关税" |

**Oracle聚合SQL**：
```sql
SELECT 
    TRUNC(DTIME) AS pub_day,
    COUNT(*) AS article_count
FROM CJ_NEWS
WHERE DTIME BETWEEN :start AND :end
  AND STATUS >= 0
  AND (TITLE LIKE '%' || :keyword || '%' 
       OR KEYWORDS LIKE '%' || :keyword || '%' 
       OR KEYWORD LIKE '%' || :keyword || '%')
GROUP BY TRUNC(DTIME)
ORDER BY pub_day;
```

#### 展示规范

- **图表类型**：ECharts 折线图（平滑曲线）
- **X轴**：日期，格式 MM-DD，标签旋转45°防重叠
- **Y轴**：文章数量，整数刻度，从0开始
- **折线颜色**：#3b82f6（蓝色）
- **面积填充**：渐变填充，从 rgba(59,130,246,0.15) 到 rgba(59,130,246,0.02)
- **数据点**：每个日期点显示数量标签（仅count > 0时）
- **高度**：350px

#### 关键节点标注

- **来源**：用户在前端手动输入，或调用大模型+联网搜索自动提取（提取方式见3.3）
- **展示**：金色虚线竖线（#d4a855，线宽1.5px，虚线模式[6,4]）
- **标注内容**：节点名称（顶部，如"特朗普宣布加征关税"）+ 日期（底部，如"04-02"）
- **样式**：节点名称背景 rgba(251,191,36,0.15)，圆角4px，字体11px #92400e

#### 交互设计

- **点击数据点**：点击某天（count > 0），下方展开 **3.2 当日热点文章榜**
- **Tooltip**：悬停显示日期 + 当日文章总数
- **缩放**：支持横轴缩放（dataZoom），默认展示完整区间

---

### 3.2 当日热点文章榜

#### 功能定义
点击时间轴上某一天后，展示该日期（00:00-23:59）内所有文章的阅读量排行榜。一眼看到"4月2号这天最火的文章是哪些"。

#### 数据来源

| 字段 | 来源表.字段 | 说明 |
|------|------------|------|
| title | CJ_NEWS.TITLE | 文章标题 |
| source | CJ_NEWS.SOURCE | 公众号名称 |
| dtime | CJ_NEWS.DTIME | 精确到时分秒 |
| read_count | CJ_NEWS.READ_COUNT | 阅读量 |
| url | CJ_NEWS.URL | 原文链接 |

**数据过滤逻辑**：筛选 DTIME 在当天00:00-23:59 内的文章，按 READ_COUNT 降序排列。

**Oracle查询SQL**：
```sql
SELECT 
    ID AS news_id,
    TITLE AS title,
    SOURCE AS source,
    DTIME AS dtime,
    READ_COUNT AS read_count,
    URL AS url
FROM CJ_NEWS
WHERE DTIME >= TO_DATE('2025-04-02', 'YYYY-MM-DD')
  AND DTIME < TO_DATE('2025-04-03', 'YYYY-MM-DD')
  AND STATUS >= 0
  AND (TITLE LIKE '%' || :keyword || '%' 
       OR KEYWORDS LIKE '%' || :keyword || '%' 
       OR KEYWORD LIKE '%' || :keyword || '%')
ORDER BY READ_COUNT DESC;
```

#### 展示规范

- **布局**：时间轴下方展开的卡片区域，圆角12px，背景#ffffff，边框1px solid #e5e7eb
- **标题**："2025-04-02 当日热点文章（共N篇）"
- **列表样式**：
  - 每行一条文章，行高48px
  - 左侧：排名序号（1-3名金色加粗，4名起灰色）
  - 中间：文章标题（单行截断，14px，#111827）+ 公众号名称（12px，#6b7280）
  - 右侧：阅读量（14px，#3b82f6，加粗，如"15,234"）+ 发布时间（12px，#9ca3af，格式 HH:MM）
  - 行底部分隔线 1px #f3f4f6
- **最大高度**：400px，超出可滚动

#### 交互设计

- **点击标题**：新标签页打开 CJ_NEWS.URL
- **点击外部/再次点击日期点**：收起文章榜
- **展开/收起动画**：200ms 平滑过渡

---

### 3.3 观点总结

#### 功能定义
对整个时间区间内所有文章，按阅读量取Top10，用大模型提炼每篇文章的核心观点。看到的是"这段时间最火的10篇文章，各自说了什么判断"。

#### 数据来源

| 字段 | 来源表.字段 | 说明 |
|------|------------|------|
| title | CJ_NEWS.TITLE | 文章标题 |
| content | CJ_NEWS_CONTENT.CONTENT | 文章正文（CLOB） |
| read_count | CJ_NEWS.READ_COUNT | 阅读量 |
| source | CJ_NEWS.SOURCE | 公众号名称 |
| dtime | CJ_NEWS.DTIME | 发布时间 |
| url | CJ_NEWS.URL | 原文链接 |
| keysentence | CJ_NEWS.KEYSENTENCE | Python自动提取的关键句，辅助大模型理解 |
| summary | CJ_NEWS.SUMMARY | 识别的核心摘要，辅助大模型理解 |

**数据筛选逻辑**：全区间文章按 READ_COUNT 降序取Top10，一次性输入大模型提炼观点。

**Top10查询SQL**：
```sql
SELECT 
    n.ID AS news_id,
    n.TITLE AS title,
    c.CONTENT AS content,
    n.SOURCE AS source,
    n.DTIME AS dtime,
    n.READ_COUNT AS read_count,
    n.URL AS url,
    n.KEYSENTENCE AS keysentence,
    n.SUMMARY AS summary
FROM CJ_NEWS n
LEFT JOIN CJ_NEWS_CONTENT c ON n.ID = c.NEWS_ID
WHERE n.DTIME BETWEEN :start AND :end
  AND n.STATUS >= 0
  AND (n.TITLE LIKE '%' || :keyword || '%' 
       OR n.KEYWORDS LIKE '%' || :keyword || '%' 
       OR n.KEYWORD LIKE '%' || :keyword || '%')
ORDER BY n.READ_COUNT DESC
FETCH FIRST 10 ROWS ONLY;
```

#### 大模型提示词

```
你是一位金融资讯分析师。请对以下阅读量最高的10篇文章进行观点提炼。

输入格式（每篇文章）：
- 标题：{TITLE}
- 来源：{SOURCE}
- 阅读量：{READ_COUNT}
- 关键句：{KEYSENTENCE}
- 摘要：{SUMMARY}
- 正文前2000字：{CONTENT前2000字}

要求：
1. 每篇文章提炼一个核心观点，不超过30字。
2. 观点必须基于文章明确表达的判断，严禁编造。
3. 如果文章只是新闻报道、没有明确观点，标注为"事件报道，无明显观点"。
4. 输出格式为JSON数组，每项包含：title（标题）、source（来源）、read_count（阅读量）、viewpoint（观点，≤30字）、url（链接）。

输出：
[{"title":"...","source":"...","read_count":12345,"viewpoint":"...","url":"..."}, ...]
```

#### 展示规范

- **布局**：独立Card，位于时间轴下方，间距20px
- **标题**："区间热点观点（阅读量Top10）"
- **列表样式**：
  - 每行一条，行高自适应（最小60px）
  - 左侧：阅读量排名（1-3名🏆金色，4-10名数字）
  - 中间区域：
    - 文章标题（14px，#111827，加粗，单行截断）
    - 来源 + 阅读量（12px，#6b7280，如"财联社 · 阅读 15,234"）
    - 观点（13px，#374151，最多2行）
  - 右侧："阅读原文"链接（12px，#3b82f6）
  - 行底部 1px #f3f4f6 分隔线
- **最大高度**：500px，超出可滚动

#### 交互设计

- **点击标题/阅读原文**：新标签页打开 CJ_NEWS.URL
- **悬停行**：背景变为 #f9fafb

---

### 3.4 热点文章卡片墙

#### 功能定义
以卡片形式展示区间内阅读量最高的100篇文章，每篇卡片包含封面图、标题、简短摘要、来源和发布时间。支持横向滑动或瀑布流浏览，直观呈现热点内容的视觉全貌。

#### 数据来源

| 字段 | 来源表.字段 | 说明 |
|------|------------|------|
| title | CJ_NEWS.TITLE | 文章标题 |
| cover_image | CJ_NEWS.ATTACH_FILE2 + CJ_NEWS.ATTACH_TYPE2 | 资讯首图附件名及类型，需拼接为可访问的图片URL |
| summary | CJ_NEWS.ABSTRACT_TEXT / CJ_NEWS.SUMMARY / CJ_NEWS.KEYSENTENCE | 资讯摘要，优先取ABSTRACT_TEXT，为空则取SUMMARY，再空取KEYSENTENCE前100字 |
| source | CJ_NEWS.SOURCE | 公众号名称 |
| dtime | CJ_NEWS.DTIME | 发布时间 |
| read_count | CJ_NEWS.READ_COUNT | 阅读量 |
| url | CJ_NEWS.URL | 原文链接 |

> **封面图获取说明**：知丘推送的 `CJ_NEWS.ATTACH_FILE2` 存储首图文件名，`ATTACH_TYPE2` 存储图片类型。实际图片URL需根据西部本地的文件存储路径规则拼接，如 `https://{storage_domain}/{ATTACH_FILE2}`。具体拼接规则需与开发确认存储服务地址。若图片字段为空，显示默认占位图。

**Top100查询SQL**：
```sql
SELECT 
    n.ID AS news_id,
    n.TITLE AS title,
    n.ATTACH_FILE2 AS cover_image,
    n.ATTACH_TYPE2 AS cover_type,
    COALESCE(n.ABSTRACT_TEXT, n.SUMMARY, SUBSTR(n.KEYSENTENCE, 1, 100)) AS summary,
    n.SOURCE AS source,
    n.DTIME AS dtime,
    n.READ_COUNT AS read_count,
    n.URL AS url
FROM CJ_NEWS n
WHERE n.DTIME BETWEEN :start AND :end
  AND n.STATUS >= 0
  AND (n.TITLE LIKE '%' || :keyword || '%' 
       OR n.KEYWORDS LIKE '%' || :keyword || '%' 
       OR n.KEYWORD LIKE '%' || :keyword || '%')
ORDER BY n.READ_COUNT DESC
FETCH FIRST 100 ROWS ONLY;
```

#### 展示规范

- **布局**：独立区域，位于观点总结下方，间距20px
- **标题**："热点文章（阅读量Top100）"
- **卡片排列**：
  - 桌面端：每行3-4列，等宽卡片，间距16px
  - 平板端：每行2列
  - 移动端：每行1列
  - 整体区域支持横向滑动或纵向瀑布流滚动
- **单卡片样式**：
  - 宽度：自适应列宽
  - 圆角：12px
  - 背景：#ffffff
  - 边框：1px solid #e5e7eb
  - 阴影：0 2px 8px rgba(0,0,0,0.06)
  - 悬停：阴影加深至 0 4px 16px rgba(0,0,0,0.1)，卡片上移2px（transform: translateY(-2px)），过渡200ms
- **卡片内容结构（自上而下）**：
  1. **封面图区域**：
     - 高度：160px（桌面端）/ 140px（移动端）
     - 宽度：100%
     - object-fit: cover
     - 圆角：顶部12px，底部直角
     - 图片加载失败时显示默认灰色占位背景 + "暂无封面"文字
  2. **标题**：
     - 字体：14px，#111827，font-weight 600
     - 行数：最多2行，超出省略
     - 内边距：左右16px，上12px
  3. **摘要**：
     - 字体：12px，#6b7280
     - 行数：最多2行，超出省略
     - 内边距：左右16px，上4px
  4. **底部信息栏**：
     - 布局：flex，space-between
     - 左侧：来源名称（12px，#3b82f6）
     - 右侧：发布时间（12px，#9ca3af，格式 MM-DD HH:MM）
     - 内边距：16px
     - 顶部边框：1px solid #f3f4f6

#### 交互设计

- **点击卡片**：新标签页打开 CJ_NEWS.URL
- **悬停卡片**：整体阴影加深 + 上移效果，光标变为pointer
- **图片懒加载**：卡片进入视口后再加载封面图，减少首屏请求
- **加载更多**：若文章数量超过初始展示量（如超过20张卡片），底部显示"加载更多"按钮，分批加载

#### 降级处理

| 场景 | 处理策略 |
|------|---------|
| ATTACH_FILE2为空 | 显示默认占位图（灰色背景 + 文章首字或"暂无封面"） |
| ABSTRACT_TEXT/SUMMARY/KEYSENTENCE全为空 | 摘要区域显示 "—" 或留空 |
| URL为空 | 点击卡片无跳转，光标恢复默认 |

---

### 3.5 关联标的走势


#### 功能定义
展示事件关联标的在选定时间区间内的日K线走势，并在K线上标注事件关键节点，方便对照"事件发生 vs 市场反应"。

#### 数据来源

**标的提取（从文章中提取）**：

| 字段 | 来源表.字段 | 说明 |
|------|------------|------|
| 股票代码 | CJ_NEWS.SCODE_LIST | 关联股票代码列表，如"sh600000,sz000001" |
| 股票名称 | CJ_NEWS.SNAME_LIST | 关联股票名称列表，如"600000(NAME),000001(NAME)" |

**高频标的提取SQL**：
```sql
SELECT 
    TRIM(REGEXP_SUBSTR(SCODE_LIST, '[^,]+', 1, LEVEL)) AS stk_code,
    COUNT(*) AS mention_count
FROM CJ_NEWS
WHERE DTIME BETWEEN :start AND :end
  AND STATUS >= 0
  AND SCODE_LIST IS NOT NULL
CONNECT BY INSTR(SCODE_LIST, ',', 1, LEVEL - 1) > 0
GROUP BY TRIM(REGEXP_SUBSTR(SCODE_LIST, '[^,]+', 1, LEVEL))
ORDER BY mention_count DESC
FETCH FIRST 5 ROWS ONLY;
```

**行情数据来源**：
- **来源**：同花顺API / Wind API / 东方财富API
- **说明**：行情数据不在知丘UTS推送范围内，需另行接入
- **请求参数**：标的代码 + 时间区间
- **返回字段**：date, open, close, high, low

#### 展示规范

- **布局**：独立Card，Tab切换标的（若多个标的）
- **图表类型**：ECharts K线图（蜡烛图）
- **颜色**：红涨绿跌（收盘≥开盘红色#ef4444，反之绿色#22c55e）
- **高度**：350px（与时间轴等高，保持视觉统一）

#### 关键节点标注

- **展示**：金色虚线竖线（#d4a855，线宽1.5px，虚线模式[6,3]），从顶部贯穿至底部
- **标注内容**：节点名称标签（背景#d4a855，白色文字，11px，圆角4px）
- **对齐**：竖线对齐节点日期

#### Tooltip设计

- **触发**：鼠标在K线区域移动，十字光标吸附最近K线
- **内容**：
  - 日期（yyyy-MM-dd）
  - 开盘 / 收盘 / 最高 / 最低（保留2位小数）
  - 涨跌额 / 涨跌幅（红涨绿跌）

#### 降级处理

| 场景 | 处理策略 |
|------|---------|
| SCODE_LIST全为空 | 隐藏整个模块，或提示"未识别到关联标的" |
| 行情API请求失败 | 显示"行情数据加载失败" + 重试按钮 |
| 行情数据为空 | 显示"暂无行情数据" |

---

## 四、页面布局

```
┌─────────────────────────────────────────────┐
│  顶部：用户输入区                              │
│  时间区间选择器 + 事件关键词输入框              │
├─────────────────────────────────────────────┤
│                                              │
│  模块1：传播时间轴（折线图）                    │
│  ┌──────────────────────────────────────┐   │
│  │  折线：每日文章数量 + 关键节点竖线      │   │
│  │  点击某天 → 展开模块2                  │   │
│  └──────────────────────────────────────┘   │
│                                              │
├─────────────────────────────────────────────┤
│  模块2：当日热点文章榜（点击展开）              │
│  ┌──────────────────────────────────────┐   │
│  │  排名 | 标题 | 来源 | 阅读量 | 时间   │   │
│  │  1    | xxx  | 财联社| 15,234 | 09:30 │   │
│  │  2    | xxx  | 券商中国| 8,932| 10:15 │   │
│  └──────────────────────────────────────┘   │
│                                              │
├─────────────────────────────────────────────┤
│  模块3：观点总结（阅读量Top10）                │
│  ┌──────────────────────────────────────┐   │
│  │  排名 | 标题 | 来源·阅读量 | 观点     │   │
│  │  1    | xxx  | 财联社·15,234| 看多出口链│   │
│  │  2    | xxx  | 远川·8,932  | 关注内需  │   │
│  └──────────────────────────────────────┘   │
│                                              │
├─────────────────────────────────────────────┤
│  模块5：关联标的走势（K线图）                  │
│  ┌──────────────────────────────────────┐   │
│  │  [Tab: 贵州茅台] [Tab: 平安银行]       │   │
│  │  K线图 + 关键节点竖线                   │   │
│  └──────────────────────────────────────┘   │
│                                              │
└─────────────────────────────────────────────┘
```

---

## 五、数据总线

以下字段在全链路模块间共享，每个字段标注来源表.字段名：

| 字段名 | 来源表.字段 | 消费模块 | 说明 |
|--------|------------|---------|------|
| news_id | CJ_NEWS.ID | 全链路 | 文章唯一标识 |
| title | CJ_NEWS.TITLE | 3.2, 3.3, 3.4 | 文章标题 |
| content | CJ_NEWS_CONTENT.CONTENT | 3.3（大模型输入） | 文章正文，CLOB类型 |
| url | CJ_NEWS.URL | 3.2, 3.3 | 原文链接 |
| dtime | CJ_NEWS.DTIME | 3.1, 3.2 | 文章发布时间，DATE类型 |
| source | CJ_NEWS.SOURCE | 3.2, 3.3 | 公众号名称 |
| author | CJ_NEWS.AUTHOR | 3.2 | 作者 |
| read_count | CJ_NEWS.READ_COUNT | 3.1, 3.2, 3.3 | 阅读量，INTEGER，默认0 |
| keywords | CJ_NEWS.KEYWORDS | 3.1, 3.2, 3.3（SQL筛选） | Python自动提取的关键词 |
| keyword | CJ_NEWS.KEYWORD | 3.1, 3.2, 3.3（SQL筛选） | 资讯关键词 |
| keysentence | CJ_NEWS.KEYSENTENCE | 3.3（大模型辅助输入） | Python自动提取的关键句 |
| summary | CJ_NEWS.SUMMARY | 3.3（大模型辅助输入） | 识别的核心摘要 |
| searchtext | CJ_NEWS.SEARCHTEXT | 3.3（大模型辅助输入） | 文章关键句 |
| scode_list | CJ_NEWS.SCODE_LIST | 3.4（标的提取） | 关联股票代码列表 |
| sname_list | CJ_NEWS.SNAME_LIST | 3.4（标的展示） | 关联股票名称列表 |
| status | CJ_NEWS.STATUS | 全链路SQL过滤 | ≥0为有效数据 |
| key_points | 用户输入 / 大模型提取 | 3.1, 3.4 | 事件关键节点 |
| event_name | 用户输入 / 大模型提取 | 3.3（大模型上下文） | 事件名称 |

> **like_count**：CJ_NEWS表中不存在该字段。如需使用点赞数，需确认知丘是否推送或从其他渠道获取。

---

## 六、实施建议

### 6.1 技术栈
- **数据库**：西部本地Oracle（已有）
- **后端**：Java/Python，通过JDBC连接Oracle
- **前端**：ECharts（时间轴、K线图）+ 普通列表组件
- **大模型**：GPT-4 / Claude / 通义千问（仅观点提炼一处调用）

### 6.2 开发优先级

| 优先级 | 模块 | 依赖 | 预估工期 |
|--------|------|------|---------|
| P0 | 传播时间轴 | CJ_NEWS.DTIME聚合 | 2天 |
| P0 | 当日热点文章榜 | CJ_NEWS.READ_COUNT排序 | 1天 |
| P1 | 观点总结 | 大模型API + CJ_NEWS_CONTENT | 2天 |
| P1 | 关联标的走势 | 行情API接入 | 3天 |

### 6.3 数据验证清单（西部现场可执行）

- [ ] 验证目标区间内有数据：`SELECT COUNT(*) FROM CJ_NEWS WHERE DTIME BETWEEN '2025-01-01' AND '2025-01-31' AND STATUS >= 0;`
- [ ] 验证正文表同步：`SELECT COUNT(*) FROM CJ_NEWS_CONTENT c JOIN CJ_NEWS n ON c.NEWS_ID = n.ID WHERE n.DTIME BETWEEN '2025-01-01' AND '2025-01-31';`
- [ ] 验证READ_COUNT非零值占比：`SELECT COUNT(*) FROM CJ_NEWS WHERE READ_COUNT > 0;`
- [ ] 验证KEYWORDS/KEYSENTENCE有数据：`SELECT COUNT(*) FROM CJ_NEWS WHERE KEYWORDS IS NOT NULL;`
- [ ] 验证SCODE_LIST有数据：`SELECT COUNT(*) FROM CJ_NEWS WHERE SCODE_LIST IS NOT NULL;`
