#!/usr/bin/env python3
# 错价雷达 · GitHub Actions 桥
# Scrapling 隐身浏览器抓取反爬重灾区的清仓/促销页,
# 提取「现价 + 原价」价格对后 POST 回 Worker /api/ingest,由雷达统一过滤(≥85% off 实物)与推送。
#
# 原理:浏览器渲染后的商品卡片里,最大的 $ 金额 = 原价,最小 = 现价;
#       折扣不足 2/3 的卡片直接丢弃(不可能过 85% 线),噪音极低。

import os
import re
import json
import time
import html as html_lib
import urllib.request
import urllib.parse

from scrapling import StealthyFetcher

RADAR_URL = os.environ.get('RADAR_URL', '').rstrip('/')
INGEST_TOKEN = os.environ.get('INGEST_TOKEN', '')

MONEY = re.compile(r'\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)')
ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]{10,600}?)</a>', re.I)
CARD_SELECTORS = [
    '[data-item-id]',
    '[data-product-id]',
    'div[data-testid*="product"]',
    'li[data-testid*="product"]',
    '.product-card',
    '.product-tile',
    '.product-item',
    'article',
    'li',
]

TARGETS = {
    'walmart': [
        'https://www.walmart.ca/en/collection/clearance-3898',
        'https://www.walmart.ca/en/search?q=clearance',
    ],
    'costco': [
        'https://www.costco.ca/Clearance.html',
    ],
    'canadiantire': [
        'https://www.canadiantire.ca/en/promotional/sale.html',
        'https://www.canadiantire.ca/en/promotional/clearance.html',
    ],
    'londondrugs': [
        'https://www.londondrugs.com/on-sale',
    ],
}


def money_pairs(text):
    """卡片文本 → (现价, 原价)。金额过多(>8)视为非商品卡丢弃。"""
    raw = [float(m.replace(',', '')) for m in MONEY.findall(text)]
    vals = sorted({v for v in raw if 0.3 <= v <= 10000})
    if len(vals) < 2 or len(vals) > 8:
        return None, None
    price, old = vals[0], vals[-1]
    if price <= 0 or old / price < 3:
        return None, None
    return price, old


def make_deal(text, href, base_url):
    price, old = money_pairs(text)
    if price is None:
        return None
    title = html_lib.unescape(text).strip().replace('\n', ' ')
    title = re.sub(r'\s+', ' ', title)[:120]
    if not href:
        return None
    url = urllib.parse.urljoin(base_url, href.split('#')[0])
    if not url.startswith('http'):
        return None
    return {'title': title or url, 'url': url, 'price': price, 'oldPrice': old}


def strip_tags(s):
    return html_lib.unescape(re.sub(r'<[^>]*>', ' ', s))


def get_html(page):
    for attr in ('body', 'html_content', 'html'):
        try:
            v = getattr(page, attr)
            if callable(v):
                v = v()
            if isinstance(v, str) and len(v) > 500:
                return v
        except Exception:
            pass
    return ''


def extract_cards(page, page_url):
    """主路径:DOM 卡片级联;兜底:锚点块正则。返回 deals 列表。"""
    html = get_html(page)
    if not html:
        return []

    deals, seen = [], set()
    for sel in CARD_SELECTORS:
        try:
            els = page.css(sel)
        except Exception:
            continue
        cards = []
        for el in els:
            try:
                t = el.text or strip_tags(getattr(el, 'body', '') or '')
            except Exception:
                continue
            if len(MONEY.findall(t)) >= 2:
                cards.append((el, t))
        if len(cards) < 4:
            continue
        for el, t in cards:
            href = None
            try:
                a = el.css_first('a')
                if a is not None:
                    href = (a.attrib or {}).get('href')
            except Exception:
                pass
            d = make_deal(t, href, page_url)
            if d and d['url'] not in seen:
                seen.add(d['url'])
                deals.append(d)
        if deals:
            break
    if deals:
        return deals

    # 兜底:锚点块
    for m in ANCHOR.finditer(html):
        href = html_lib.unescape(m.group(1))
        text = strip_tags(m.group(2))
        d = make_deal(text, href, page_url)
        if d and d['url'] not in seen:
            seen.add(d['url'])
            deals.append(d)
    return deals


def scrape_target(name, urls):
    all_deals, errors = [], []
    for u in urls:
        try:
            print(f'[{name}] fetch {u}')
            page = StealthyFetcher.fetch(u, headless=True, network_idle=True, timeout=60000)
            status = getattr(page, 'status', 0)
            if status not in (200, 304):
                errors.append(f'{u} -> HTTP {status}')
                continue
            batch = extract_cards(page, u)
            print(f'[{name}] {u} -> {len(batch)} 候选')
            for d in batch:
                if len(all_deals) >= 40:
                    break
                all_deals.append(d)
        except Exception as e:
            errors.append(f'{u} -> {type(e).__name__}: {str(e)[:120]}')
        time.sleep(4)
    if errors:
        print(f'[{name}] 注意: {errors}')
    return all_deals, errors


def send(source, deals):
    data = json.dumps({'source': source, 'deals': deals}).encode()
    req = urllib.request.Request(
        RADAR_URL + '/api/ingest', data=data, method='POST',
        headers={'content-type': 'application/json', 'x-ingest-token': INGEST_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        print(f'[{source}] Worker 回执:', r.read().decode()[:200])


def main():
    if not RADAR_URL or not INGEST_TOKEN:
        raise SystemExit('缺少 RADAR_URL / INGEST_TOKEN 环境变量(GitHub Secrets)')
    failed = 0
    for name, urls in TARGETS.items():
        try:
            deals, errors = scrape_target(name, urls)
            if errors and not deals:
                failed += 1
            deals = deals[:40]
            if deals:
                send(name, deals)  # 逐目标发送:即使后面超时,已抓到的数据不丢
        except Exception as e:
            print(f'[{name}] 失败: {type(e).__name__}: {str(e)[:160]}')
            failed += 1
        time.sleep(5)
    print(f'完成: 目标 {len(TARGETS)} 个, 失败 {failed} 个')
    if failed >= len(TARGETS):
        raise SystemExit(1)  # 全军覆没才报警,部分失败属正常


if __name__ == '__main__':
    main()
