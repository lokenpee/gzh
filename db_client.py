from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


def load_db_config() -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "db_type": "oracle",
        "host": "",
        "port": 1521,
        "service_name": "",
        "user": "",
        "password": "",
        "enabled": False,
    }
    try:
        import local_settings
        for key in ("DB_TYPE", "DB_HOST", "DB_PORT", "DB_SERVICE_NAME",
                     "DB_USER", "DB_PASSWORD"):
            if hasattr(local_settings, key):
                val = getattr(local_settings, key)
                if val is not None and val != "":
                    config[key.lower().replace("db_", "")] = val
    except ImportError:
        pass

    config["enabled"] = bool(
        config["host"] and config["service_name"] and config["user"] and config["password"]
    )
    return config


def query_articles(
    entities: List[str],
    start_date: date,
    end_date: date,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[pd.DataFrame], str]:
    if config is None:
        config = load_db_config()

    if not config.get("enabled"):
        return None, "DB 未配置"

    if not entities:
        return None, "缺少查询实体词"

    entity_conditions = " AND ".join(
        ["c.CONTENT LIKE :e{}".format(i) for i in range(len(entities))]
    )
    params = {"e{}".format(i): "%{}%".format(e) for i, e in enumerate(entities)}
    params["start_date"] = start_date.strftime("%Y-%m-%d")
    params["end_date"] = end_date.strftime("%Y-%m-%d")

    sql = """
        SELECT
            n.ID AS news_id,
            c.CONTENT AS content,
            n.TITLE AS title,
            n.URL AS url,
            n.DTIME AS dtime,
            n.SOURCE AS source,
            n.AUTHOR AS author,
            n.READ_COUNT AS read_count,
            n.KEYWORDS AS keywords,
            n.SUMMARY AS keysentence,
            n.SUMMARY AS summary,
            n.SCODE_LIST AS scode_list,
            n.SNAME_LIST AS sname_list,
            n.ABSTRACT_TEXT AS abstract_text,
            n.ATTACH_FILE2 AS attach_file2
        FROM CJ_NEWS n
        LEFT JOIN CJ_NEWS_CONTENT c ON n.ID = c.NEWS_ID
        LEFT JOIN CJ_NEWS_OPEN o ON n.OPENID = o.ID
        WHERE n.DTIME BETWEEN TO_DATE(:start_date, 'YYYY-MM-DD')
                         AND TO_DATE(:end_date, 'YYYY-MM-DD') + 1
          AND n.CJ_TYPE = 2
          AND ({entity_clause})
        ORDER BY n.READ_COUNT DESC
    """.format(entity_clause=entity_conditions)

    db_type = config.get("db_type", "oracle")

    if db_type == "oracle":
        try:
            import oracledb
            conn = oracledb.connect(
                user=config["user"],
                password=config["password"],
                dsn="{host}:{port}/{service_name}".format(
                    host=config["host"],
                    port=config.get("port", 1521),
                    service_name=config["service_name"],
                ),
            )
            df = pd.read_sql(sql, conn, params=params)
            conn.close()
            return df, "ok"
        except ImportError:
            return None, "oracledb 库未安装，请执行 pip install oracledb"
        except Exception as exc:
            return None, f"数据库查询失败：{exc}"

    if db_type == "mysql":
        try:
            import pymysql
            conn = pymysql.connect(
                host=config["host"],
                port=int(config.get("port", 3306)),
                user=config["user"],
                password=config["password"],
                database=config["service_name"],
                charset="utf8mb4",
            )
            df = pd.read_sql(sql, conn, params=params)
            conn.close()
            return df, "ok"
        except ImportError:
            return None, "pymysql 库未安装，请执行 pip install pymysql"
        except Exception as exc:
            return None, f"数据库查询失败：{exc}"

    return None, f"不支持的数据库类型：{db_type}"
