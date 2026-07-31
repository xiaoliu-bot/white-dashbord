# 东财真实主力/散户拆分 · 本机定时抓取部署说明

GitHub Actions 海外 runner 访问东方财富被 502 拒绝（地缘封锁），所以 CI 里的东财源会自动跳过，
板块的「主力/散户拆分」只能靠 AKShare 的 55%/28% 估算。

本机在中国大陆（北京 IP），可以直连东财拿到**真实**的主力/中单/小单拆分。下面把这套抓取
注册成 Windows 定时任务，每 5 分钟跑一次，把结果 `api/plate_em.json` 推回仓库；
看板 `index.html` 会自动拉取并叠加，覆盖估算值。

## 文件
- `fetch_eastmoney_plates.py` —— 抓取+匹配+写文件+推送（核心脚本）
- `install_scheduler.ps1` —— 一键注册 Windows 任务计划程序
- `em_config.example.json` —— PAT 模板（复制成 `em_config.json` 后填值，**勿提交**）
- `.gitignore` 已排除 `em_config.json` / `.em_pat`

## 部署步骤
1. 把这些文件放到你本地 `white-dashbord` 仓库的**根目录**（与 `api/` 同级）。
2. 复制模板并填入 PAT（需 `repo` 权限，可用你之前的 `ghp_LS9x…` 或新 PAT）：
   ```powershell
   copy em_config.example.json em_config.json
   # 用记事本打开 em_config.json，把 pat 改成你的真实 PAT
   ```
3. 确认本机有 Python 3.8+ 且 `python` 在 PATH；否则改 `install_scheduler.ps1` 里的 `$Py` 为完整路径。
4. **先手动跑一次**验证能写出并推送：
   ```powershell
   python fetch_eastmoney_plates.py
   ```
   正常会打印每个板块匹配情况，并在仓库根 `api/plate_em.json` 生成文件、推送到 GitHub。
5. 以**管理员身份**打开 PowerShell，cd 到本目录，注册定时任务：
   ```powershell
   .\install_scheduler.ps1
   ```
   任务名 `WhiteDashbord-EastmoneyPlates`：周一~周五 09:00 起每 5 分钟，持续约 6h15m（覆盖交易时段）。

## 验证
- 看板打开后按 F12 看 Console，应出现 `[东财叠加] 命中 N 个板块 · 更新于 …`。
- 气泡图/板块异动的「主力/大户/散户」数值即为东财真实拆分（与 AKShare 估算会有差异，属正常）。

## 排查
- 任务没跑：任务计划程序 → `WhiteDashbord-EastmoneyPlates` → 「历史记录」；脚本本身会打印匹配与推送情况。
- 推送失败：脚本会把 `api/plate_em.json` 留在本地，按提示手动 `git add api/plate_em.json && git commit && git push` 即可。
- 东财接口变动：若某板块打印 `x 未匹配`，多半是东财板块名变了，改 `fetch_eastmoney_plates.py` 里 `TARGETS` 的别名后重跑。
