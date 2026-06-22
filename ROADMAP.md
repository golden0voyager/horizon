# Horizon 架构升级路线图 (Future Roadmap)

本文档记录了 Horizon 信息聚合系统的后续优化方向与扩展设想。

## 1. AI 决策层 (AI Brain)
- [x] **多级供应商 Fallback**：支持 `HORIZON_AI_PROVIDER=gemini,openrouter`，自动应对 API 故障。
- [ ] **混合模型策略 (Hybrid Logic)**：
    - 使用极廉价模型（如 Llama 3 8B）进行海量初筛。
    - 仅对高分（Scored ≥ 8.5）内容使用昂贵模型（如 Claude 3.5/Gemini 1.5 Pro）进行深度总结。
- [ ] **多模态读图 (Vision)**：支持 Telegram/Twitter 的图片识别与摘要。
- [ ] **全文增强 (Full-text Enrichment)**：高分链接自动触发 Jina Reader 或 Firecrawl 进行网页全文爬取再总结。

## 2. 采集源扩展 (Data Sources)
- [ ] **Twitter 采集方案优化**：寻找比 Apify 更稳健的代理或镜像方案（如 Nitter/SocialData）。
- [ ] **音视频支持**：集成 YouTube/Podcast RSS，利用 Whisper 进行语音转文字总结。
- [ ] **学术论文监控**：集成 arXiv RSS，自动翻译并解析 AI 领域的前沿论文。

## 3. 推送与交互 (Output & UI)
- [ ] **飞书消息卡片 (Feishu Card)**：将纯文本推送升级为带封面、按钮和评分标签的交互式卡片。
- [ ] **自动分类标签**：AI 自动为内容打标（如 `#AI`, `#Market`, `#Crypto`），并在 GitHub Pages 上分类显示。
- [ ] **每周精选 (Weekly Digest)**：自动汇总本周分值最高、被点击最多的内容。

## 4. 知识管理 (Memory & RAG)
- [ ] **本地向量库集成**：将往期总结向量化，使 AI 在分析新消息时具备“历史视野”。
- [ ] **重复内容去重 (Smart Deduplication)**：跨平台识别同一热点事件，合并推送。

## 5. 跨项目集成 (Project Synergy)
- [ ] **集成 x_digest 采集引擎**：
    - 使用 Playwright + Cookie 方案替换 Apify，实现 0 成本、高并发采集。
    - 支持多账号 (x_cookies_*.json) 负载均衡。
- [ ] **引入“深度洞察”管线**：将 x_digest 的 `insights.py` 逻辑植入 Horizon，提升日报的行业分析深度。
- [ ] **72h 滚动情报池**：引入滚动缓存机制，确保推文信息不遗漏。
- [ ] **供应商降级链整合**：合并两者的 AI 调度逻辑，实现多供应商自动 Fallback。

---
*Last updated: 2026-05-08*
