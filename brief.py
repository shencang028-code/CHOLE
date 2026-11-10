"""Generate a daily Markdown brief and optionally deliver it via Server酱 Turbo."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import json
import math
import os
from pathlib import Path
import re
import sys
from threading import Lock
from time import monotonic, sleep
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


CHINA = timezone(timedelta(hours=8))
CONFIG = Path(__file__).with_name("config.json")
NEWS_API_LOCK = Lock()
next_news_request = 0.0


def request(url, fields=None, headers=None):
    data = urlencode(fields).encode() if fields is not None else None
    req = Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 DailyBrief/1.0", **(headers or {})})
    with urlopen(req, timeout=15) as response:
        payload = response.read(8_000_001)
    if len(payload) > 8_000_000:
        raise ValueError("响应过大")
    return payload


def markdown(value):
    return re.sub(r"([\\`*_{}\[\]<>|!~])", r"\\\1", " ".join(value.split()))


def link(label, url):
    if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
        return markdown(label)
    return f"[{markdown(label)}]({quote(url, safe=':/?#[]@!$&\'*+,;=%')})"


def rss(url):
    root = ElementTree.fromstring(request(url))
    if root.find("channel") is None:
        raise ValueError("需要 RSS 2.0 订阅源")
    items = []
    for node in root.findall("./channel/item"):
        title = node.findtext("title", "").strip()
        href = node.findtext("link", "").strip()
        if not href:
            enclosure = node.find("enclosure")
            href = enclosure.get("url", "") if enclosure is not None else ""
        try:
            published = parsedate_to_datetime(node.findtext("pubDate", ""))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
        if title and href:
            items.append((published, title, href, node.findtext("source", "")))
    return sorted(items, reverse=True)


def news_with_details(config, now, api_key):
    global next_news_request
    params = urlencode({
        "q": config["query"], "lang": "zh",
        "from": (now - timedelta(hours=48)).astimezone(timezone.utc).isoformat(),
        "max": 10, "sortby": "publishedAt", "nullable": "image,content",
    })
    # The free plan allows one request per second.
    with NEWS_API_LOCK:
        sleep(max(0, next_news_request - monotonic()))
        next_news_request = monotonic() + 1.1
        response = request(f"https://gnews.io/api/v4/search?{params}",
                           headers={"X-Api-Key": api_key})
    lines, seen = [], set()
    for article in json.loads(response)["articles"]:
        title = unescape(article["title"]).strip()
        description = unescape(article.get("description") or "")
        description = " ".join(re.sub(r"<[^>]*>", "", description).split())
        key = re.sub(r"[\W_]+", "", title.casefold())
        description_key = re.sub(r"[\W_]+", "", description.casefold())
        if not key or key in seen or not description_key or description_key == key:
            continue
        seen.add(key)
        source = article["source"]["name"]
        date = datetime.fromisoformat(article["publishedAt"]).astimezone(CHINA)
        details = description if len(description) <= 120 else description[:120].rstrip() + "…"
        lines.append(
            f"- **{markdown(title)}** · {markdown(source)} · {date:%m-%d}\n\n"
            f"  {markdown(details)}\n\n"
            f"  {link('查看原文', article['url'])}"
        )
        if len(lines) >= config["limit"]:
            break
    return lines


def news(config, now):
    api_key = os.environ.get("GNEWS_API_KEY", "").strip()
    if not api_key:
        return ["尚未配置新闻摘要来源。以下为备用标题：", *news_headlines(config, now)]
    try:
        lines = news_with_details(config, now, api_key)
        if lines:
            return lines
        reason = "近 48 小时暂无带摘要的报道。"
    except Exception as error:
        print(f"{config['name']}摘要获取失败（{type(error).__name__}）", file=sys.stderr)
        reason = "新闻摘要暂不可用。"
    return [reason, *news_headlines(config, now)]


def news_headlines(config, now):
    params = urlencode({
        "q": config["query"] + " when:2d",
        "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans",
    })
    items = rss(f"https://news.google.com/rss/search?{params}")
    lines, seen = [], set()
    for published, title, url, source in items:
        if not now - timedelta(hours=48) <= published <= now:
            continue
        suffix = " - " + source
        if source and title.endswith(suffix):
            title = title[:-len(suffix)]
        key = re.sub(r"[\W_]+", "", title.casefold())
        if not key or key in seen:
            continue
        seen.add(key)
        date = published.astimezone(CHINA).strftime("%m-%d")
        lines.append(f"- {link(title, url)} · {markdown(source or 'Google 新闻')} · {date}")
        if len(lines) >= config["limit"]:
            break
    return lines or ["近 48 小时暂无符合条件的报道。"]


def market(config):
    symbol = quote(config["symbol"], safe="")
    last_error = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            url = f"https://{host}/v8/finance/chart/{symbol}?range=1mo&interval=1d"
            chart = json.loads(request(url))["chart"]["result"][0]
            offset = timezone(timedelta(seconds=chart["meta"]["gmtoffset"]))
            values = chart["indicators"]["quote"][0]["close"]
            quotes = sorted(
                (stamp, value)
                for stamp, value in zip(chart["timestamp"], values)
                if isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            )
            (previous_stamp, previous), (stamp, value) = quotes[-2:]
            date = datetime.fromtimestamp(stamp, offset).strftime("%Y-%m-%d")
            change = (value / previous - 1) * 100
            name = link(config["name"], f"https://finance.yahoo.com/quote/{symbol}/")
            return [f"- {name} · {value:,.2f} · {change:+.2f}% · {date}"]
        except Exception as error:
            last_error = error
    raise RuntimeError("行情源暂不可用") from last_error


def weather(config, now):
    params = urlencode({
        "latitude": config["latitude"], "longitude": config["longitude"],
        "daily": "temperature_2m_min,temperature_2m_max,precipitation_probability_max,weather_code",
        "timezone": "Asia/Shanghai", "forecast_days": 2,
    })
    daily = json.loads(request(f"https://api.open-meteo.com/v1/forecast?{params}"))["daily"]
    i = daily["time"].index(now.date().isoformat())
    code = daily["weather_code"][i]
    conditions = {
        0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
        45: "雾", 48: "冻雾", 51: "小毛毛雨", 53: "毛毛雨", 55: "较强毛毛雨",
        56: "轻微冻毛毛雨", 57: "冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
        66: "轻微冻雨", 67: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "强阵雪",
        95: "雷阵雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
    }
    low, high = (daily[f"temperature_2m_{key}"][i] for key in ("min", "max"))
    probability = daily["precipitation_probability_max"][i]
    rain = f"{probability}%" if probability is not None else "暂无数据"
    return [f"{conditions.get(code, '天气待更新')} · {low:g}–{high:g}°C · 最高降水概率 {rain}",
            "来源 [Open-Meteo](https://open-meteo.com/)"]


def podcast(config, now):
    episodes = [item for item in rss(config["url"]) if item[0] <= now][:7]
    if not episodes:
        return []
    week_slot = (now.date().toordinal() // 7) % len(episodes)
    published, title, url, _ = episodes[week_slot]
    return [
        f"- {markdown(config['name'])} · {link(title, url)} · {published.astimezone(CHINA):%Y-%m-%d}",
        markdown(config["reason"]),
    ]


def collect(pool, name, operation, *args):
    def run():
        try:
            return operation(*args)
        except Exception as error:
            print(f"{name}获取失败（{type(error).__name__}）", file=sys.stderr)
            return None
    return pool.submit(run)


def generate(config, now):
    title = f"微信每日简报 · {now:%Y-%m-%d}"
    with ThreadPoolExecutor(max_workers=10) as pool:
        forecast = collect(pool, "天气", weather, config["weather"], now)
        headlines = [(item["name"], collect(pool, item["name"], news, item, now))
                     for item in config["news"]]
        markets = [(item["name"], collect(pool, item["name"], market, item))
                   for item in config["markets"]]
        podcast_results = [collect(pool, item["name"], podcast, item, now)
                           for item in config["podcasts"]]
    podcasts = [result.result() for result in podcast_results]
    if not any([forecast.result(), *[f.result() for _, f in headlines + markets], *podcasts]):
        raise RuntimeError("所有数据源均不可用，简报未生成。")

    sections = []
    sections.append((f"{config['weather']['name']}天气", forecast.result() or ["天气暂不可用。"]))
    sections.extend((name, f.result() or ["新闻源暂不可用。"]) for name, f in headlines)
    market_lines = [line for name, future in markets
                    for line in (future.result() or [f"- {markdown(name)} · 行情暂不可用"])]
    market_lines.append("来源 Yahoo Finance。按交易所当地日期显示最新日线，与上一条日线比较；盘中可能更新。")
    sections.append(("股市速览", market_lines))
    if podcasts:
        index = now.date().toordinal() % len(podcasts)
        selection = next((item for item in podcasts[index:] + podcasts[:index] if item), None)
    else:
        selection = None
    sections.append(("今日播客", selection or ["播客暂不可用。"]))
    body = "\n\n".join(f"## {heading}\n\n" + "\n\n".join(lines)
                       for heading, lines in sections)
    return title, body


def send(key, title, body):
    if not re.fullmatch(r"SCT[A-Za-z0-9_-]+", key):
        raise RuntimeError("请在 SERVERCHAN_SENDKEY 中配置 Server酱 Turbo 的 SCT 密钥。")
    try:
        response = json.loads(request(
            f"https://sctapi.ftqq.com/{key}.send", {"title": title, "desp": body}
        ))
        if response.get("code") != 0 or response.get("data", {}).get("errno", 0) != 0:
            raise ValueError("推送请求被拒绝")
    except Exception:
        raise RuntimeError("Server酱未确认接收。请先检查发送记录和微信，再决定是否重试。") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="通过 Server酱推送到微信")
    args = parser.parse_args()
    key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if args.send and not key:
        parser.exit(1, "请配置 SERVERCHAN_SENDKEY。\n")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    try:
        title, body = generate(config, datetime.now(CHINA))
        print(f"# {title}\n\n{body}")
        if args.send:
            send(key, title, body)
            print("Server酱已接收推送请求。")
    except RuntimeError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
