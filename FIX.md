# FIX: Douyin API 403 a_bogus 签名过期

**日期**: 2026-09-15
**现象**: 抖音视频下载全部返回 HTTP 403 Forbidden，错误指向 `a_bogus` 参数无效
**根因**: F2 v0.0.1.7 内置的 ABogus 算法 (v0.0.2, 2024-06) 生成的签名已过期，抖音服务端拒绝该签名
**修复方式**: 新增 Playwright 浏览器兜底——当 F2 返回 403 时，用真实 Firefox 浏览器的 JS 上下文发起 API 请求，浏览器自动生成正确的 a_bogus

---

## 排查过程

### 1. 确认问题现象

重启 Docker 服务后，bot 日志持续报 403：

```
ERROR  HTTP状态错误：Client error '403 Forbidden' for url
       'https://www.douyin.com/aweme/v1/web/aweme/detail/?...&a_bogus=...'
```

URL 中的 `a_bogus` 值由 F2 的 `ABogusManager` 算法生成。

### 2. 分析 a_bogus 生成链路

```
DouyinHandler.fetch_one_video(aweme_id)
  → DouyinCrawler.fetch_post_detail(params)
    → ABogusManager.model_2_endpoint(UA, endpoint, params_dict)
      → BrowserFpGen.generate_fingerprint("Edge")  # 固定生成 Edge 指纹
      → ABogus(fp, UA).generate_abogus(param_str)  # SM3 + RC4 + 自定义 Base64
    → httpx GET (带 a_bogus 参数)
```

关键问题：
- `abogus.py` 版本 0.0.2，最后更新 2024-06-16
- `BrowserFpGen` 固定生成 **Edge/Win32** 指纹，但 bot 实际使用 **Firefox/Linux**
- F2 自 2024-12-31 (v0.0.1.7) 后无新版本发布

### 3. 排除 cookie 问题

从 Docker volume 的 Firefox profile 提取了 59 个抖音 cookie（含 sessionid、sid_guard、msToken），通过 `validate_cookie()` 验证有效。问题确认在签名算法而非认证状态。

### 4. 确定修复方案

| 方案 | 可靠性 | 维护成本 |
|------|--------|----------|
| 更新 F2 abogus 算法 | 低（抖音频繁更新） | 高 |
| **Playwright 浏览器兜底** | **高（用抖音自身 JS）** | **低** |
| 第三方 API (douyin.wtf) | 高 | 外部依赖 |
| DOM 爬取 | 高 | 慢、脆弱 |

选择 Playwright 方案：利用已有的持久化 Firefox profile，在浏览器页面上下文中执行 `fetch()`，浏览器的 JS 拦截器自动添加正确的 a_bogus。

---

## 实施内容

### 新增文件

**`playwright_douyin_fetcher.py`** — Playwright 浏览器兜底模块

- 持有单例 Firefox 浏览器实例（复用 `~/.douyin_email_bot/firefox_profile`）
- `fetch_aweme_detail(aweme_id, cookie, user_agent)`:
  1. 确保浏览器页面存活，注入 cookie
  2. 导航到 douyin.com 建立 JS 上下文
  3. 在页面中执行 `fetch(aweme_detail_api_url, {credentials: 'include'})`
  4. 浏览器 JS 拦截器自动添加正确的 a_bogus
  5. 返回解析后的 JSON

### 修改文件

**`douyin_downloader.py`**

1. 新增 `_PlaywrightVideoData` 类 — 包装 Playwright 返回的原始 JSON，提供与 F2 `PostDetailFilter` 兼容的 `_to_raw()` / `_to_dict()` 接口
2. 新增 `_playwright_fetch_video_data()` — 异步调用 Playwright fetcher 的辅助函数
3. 修改 `_download_async_bound()` — 捕获 F2 的 403 异常，自动降级到 Playwright
4. 修改 `_validate_douyin_metadata_bound()` — 验证路径同样支持 Playwright 降级

降级流程：
```
F2 fetch_one_video(aweme_id)
    ↓ 403
日志: "F2 metadata fetch got 403, falling back to Playwright"
    ↓
Playwright fetch_aweme_detail(aweme_id, cookie, ua)
    ↓
_PlaywrightVideoData 包装 → 继续正常下载流程
```

---

## 验证

- 39 个现有单元测试全部通过
- 直接调用 `fetch_aweme_detail("7685351048295464354", cookie)` 成功返回：
  - aweme_id、author、desc、media_type 正确
  - 3 个 play_addr URL
  - 6 个 bit_rate 条目（720p/540p 多档）
- Docker 容器已重建并运行正常
- 真实端到端下载未测试（等新邮件触发）

---

## F2 403 追踪

每次触发 Playwright 降级时，会在 `logs/f2_403_fallback.log` 追加一行：

```
2026-09-15T15:30:00.123456	7685351048295464354
```

格式：`ISO时间戳\taweme_id`。用于判断 F2 是否已彻底失效，决定是否完全移除 F2。

查看统计：
```bash
wc -l logs/f2_403_fallback.log           # 总降级次数
tail -20 logs/f2_403_fallback.log        # 最近 20 条
```

---

## 注意事项

- Playwright 浏览器实例在 bot 进程内常驻，首次请求需要导航到 douyin.com（~15s），后续请求复用页面
- a_bogus 算法仍可能随抖音更新失效，但 Playwright 方案使用抖音自身 JS，理论上无需维护
- 如果浏览器 profile 的 cookie 过期，仍需通过 Web Login 或 `get_cookie.py` 刷新
