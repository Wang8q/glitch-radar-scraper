# 错价雷达 · Scrapling 桥(GitHub Actions)

用 [Scrapling](https://github.com/D4Vinci/Scrapling) 隐身浏览器抓取反爬重灾区的清仓/促销页
(Walmart.ca、Costco.ca、Canadian Tire、London Drugs,可自行增删),
把「现价+原价」候选喂回错价雷达 Worker,由雷达统一过滤(≥85% off 实物)、核验、推送企业微信/WhatsApp。

## 工作原理

```
GitHub Actions(每 30 分钟)
  → Scrapling 隐身浏览器打开目标清仓页
  → 提取商品卡片:卡片里最大 $ 金额 = 原价,最小 = 现价(折扣不足 2/3 的卡片直接丢)
  → POST /api/ingest(独立令牌)
Worker:同一套 ≥85% off / 实物 / 去重 / 库存核验管线 → 企业微信 + WhatsApp
```

## 接入步骤(约 5 分钟)

1. **GitHub 建仓库**:新建一个仓库,比如 `glitch-radar-scraper`(Private 私有即可,免费额度足够)。

2. **上传本目录全部文件**(保持目录结构,`.github/workflows/scrape.yml` 必须在):
   ```bash
   git init && git add -A && git commit -m "glitch-radar scraper"
   git remote add origin https://github.com/你的用户名/glitch-radar-scraper.git
   git push -u origin main
   ```

3. **配置 3 个 Secrets**(仓库页 → Settings → Secrets and variables → Actions → New repository secret):

   | Secret 名 | 值 |
   |---|---|
   | `RADAR_URL` | `https://glitch-radar.zhijunwang757.workers.dev` |
   | `RADAR_KEY` | `radar-c97fafcaae`(面板口令,备用) |
   | `INGEST_TOKEN` | 爬虫桥令牌(交付时提供,如 `bridge-xxxx`) |

4. **启用定时任务**:仓库 → Actions 标签页 → 选中 "glitch-radar scraper" → Enable → 点一次 "Run workflow" 手动跑一轮验证。

## 怎么判断效果

- Actions 每次运行的日志里,每个目标会打印 `fetch` / `-> N 候选` / Worker 回执(`pushed: N`)。
- 抓到 ≥85% off 的货,微信会直接响。
- 某个目标连续失败(反爬没绕过),日志会显示 HTTP 状态;可以注释掉 `scrape_targets.py` 里 `TARGETS` 对应条目,或改 URL 换别的清仓页。

## 调整目标站

编辑 `scrape_targets.py` 顶部的 `TARGETS` 字典:键是源名(面板/推送里显示),值是该站要抓的清仓/促销页 URL 列表(每轮每目标最多收录 40 条候选)。想加"加拿大前 10 销售网站"里的任何一家,把它的清仓/促销页 URL 加进去即可——提取器是通用的,不需要为每个站写解析器。

## 注意

- 隐身模式(Camoufox)对 Cloudflare/普通 WAF 成功率高,但对 DataDome(London Drugs 这类)与 Kasada 不保证,失败会记录在日志里,不影响其他目标。
- GitHub Actions 的出口 IP 是 Azure 机房,个别站点会对机房 IP 出验证码——同样体现在日志里,换 URL / 降频可以缓解。
- 免费额度:私有仓库每月 2000 分钟,本任务单轮约 3-5 分钟,足够;公开仓库则无限制。
