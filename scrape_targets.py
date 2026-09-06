#!/usr/bin/env python3
# 错价雷达 · GitHub Actions 桥
# Scrapling 隐身浏览器抓取反爬重灾区的清仓/促销页,
# 提取「现价 + 原价」价格对后 POST 回 Worker /api/ingest,由雷达统一过滤与推送。
# 每轮结束会把运行诊断(source=diag)回传,失败原因在雷达面板可查。

import os
import sys
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
        # DataDome 反爬,GHA 出口大概率 307→blocked;保留观察
        'https://www.walmart.ca/en/search?q=clearance',
    ],
    'costco': [
        'https://www.costco.ca/s?langId=-24&keyword=last+chance&sortBy=item_startDate%2Bdesc',
        'https://www.costco.ca/s?langId=-24&keyword=last+chance',
        'https://www.costco.ca/s?langId=-24&keyword=clearance',
    ],
    'canadiantire': [
        'https://www.canadiantire.ca/en/promotions/clearance.html',
        'https://www.canadiantire.ca/en/promotions/clearance.html?page=2',
        'https://www.canadiantire.ca/en/promotions/clearance.html?page=3',
    ],
    'londondrugs': [
        # DataDome 反爬,最难啃;保留观察
        'https://www.londondrugs.com/on-sale',
    ],
    'amazon': [
        # 试试 Amazon.ca:今日 Deals + clearance 低价搜索
        'https://www.amazon.ca/gp/goldbox',
        'https://www.amazon.ca/s?k=clearance&s=price-asc-rank',
    ],
    'simons': [
        'https://www.simons.ca/en/sale',
        'https://www.simons.ca/en/sale?page=2',
    ],
    'sportinglife': [
        'https://sportinglife.ca/collections/sale',
        'https://sportinglife.ca/collections/sale?page=2',
    ],
    'mec': [
        'https://www.mec.ca/en/sale',
        'https://www.mec.ca/en/sale?page=2',
    ],
    'aritzia': [
        'https://www.aritzia.com/en/sale',
        'https://www.aritzia.com/en/sale?page=2',
    ],
    'homehardware': [
        'https://www.homehardware.ca/en/sale',
    ],
    'uniqlo': [
        'https://www.uniqlo.com/ca/en/sale',
    ],
    'hm': [
        'https://www2.hm.com/en_ca/sale.html',
    ],
}

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'


def money_pairs(text):
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


def fetch_page(url):
    """双层降级:隐身浏览器 → curl_cffi 指纹。返回 (page, engine, err)。"""
    try:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=60000)
        status = getattr(page, 'status', 0)
        if status in (200, 304):
            return page, 'stealth', None
        return None, None, f'stealth HTTP {status}'
    except Exception as e:
        stealth_err = f'{type(e).__name__}: {str(e)[:150]}'
    # 降级:curl_cffi 指纹直取(部分 WAF 认 TLS 指纹就放行)
    try:
        from scrapling import Fetcher
        page = Fetcher.fetch(url, impersonate='chrome', timeout=45)
        status = getattr(page, 'status', 0)
        if status in (200, 304):
            return page, 'curl_cffi', None
        return None, None, f'{stealth_err} | curl HTTP {status}'
    except Exception as e:
        return None, None, f'{stealth_err} | curl {type(e).__name__}: {str(e)[:150]}'


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
        print(f'[{name}] fetch {u}')
        page, engine, err = fetch_page(u)
        if page is None:
            errors.append(f'{u} -> {err}')
            continue
        print(f'[{name}] {u} 引擎={engine}')
        batch = extract_cards(page, u)
        print(f'[{name}] {u} -> {len(batch)} 候选')
        for d in batch:
            if len(all_deals) >= 40:
                break
            all_deals.append(d)
        time.sleep(4)
    if errors:
        print(f'[{name}] 注意: {errors}')
    return all_deals, errors


def send(source, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        RADAR_URL + '/api/ingest', data=data, method='POST',
        headers={'content-type': 'application/json', 'x-ingest-token': INGEST_TOKEN, 'user-agent': UA},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        print(f'[{source}] Worker 回执:', r.read().decode()[:200])


def main():
    if not RADAR_URL or not INGEST_TOKEN:
        raise SystemExit('缺少 RADAR_URL / INGEST_TOKEN 环境变量(GitHub Secrets)')

    # matrix 模式:只跑指定的一个目标(GitHub Actions 并行 8-12 台机器)
    only = os.environ.get('ONLY_TARGET', '')
    targets = {only: TARGETS[only]} if only in TARGETS else TARGETS

    # 自检:隐身浏览器是否可用(与目标站无关)
    diag = {}
    try:
        page = StealthyFetcher.fetch('https://example.com', headless=True, timeout=30000)
        diag['selftest'] = 'stealth ok, status ' + str(getattr(page, 'status', 0))
        print('[selftest]', diag['selftest'])
    except Exception as e:
        diag['selftest'] = f'FAIL {type(e).__name__}: {str(e)[:200]}'
        print('[selftest] 失败 →', diag['selftest'])

    failed = 0
    for name, urls in targets.items():
        try:
            deals, errors = scrape_target(name, urls)
            diag[name] = f'{len(deals)} deals' + (('; ' + '; '.join(errors)[:200]) if errors else '')
            deals = deals[:40]
            if deals:
                send(name, {'source': name, 'deals': deals})  # 逐目标发送,超时不丢已抓数据
        except Exception as e:
            diag[name] = f'EXC {type(e).__name__}: {str(e)[:200]}'
            print(f'[{name}] 失败: {diag[name]}')
            failed += 1
        time.sleep(5)

    # 回传诊断(雷达面板可查)
    try:
        send('diag', {'source': 'diag', 'deals': [], 'note': json.dumps(diag, ensure_ascii=False)})
    except Exception as e:
        print('[diag] 回传失败:', str(e)[:160])

    print(f'完成: 目标 {len(targets)} 个, 失败 {failed} 个')


if __name__ == '__main__':
    main()
