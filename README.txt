每日新闻简报　仓库说明

代码就绪：二〇二六年十一月十一日（本地已完成首次提交，提交号 401b8a5）
目标仓库：qdc123456／-daily-news-brief（公开；名字带一个前导连字符，不影响任何功能）
云端：GitHub Actions 定时工作流，无服务器、无本地常驻
定时：每天北京时间 07:30（UTC 写作 30 23 * * *，实际可能延迟五到三十分钟）
主通道：Server酱（Secret 名 SERVERCHAN_SENDKEY）
备用通道：企业微信群机器人（可选，Secret 名 WEWORK_WEBHOOK_URL）
状态：state.json 由工作流自动回写，提交信息带 [skip ci] 防循环
云端状态：本地代码已全部就绪，网页上传后定时任务即生效，见【壹】


【壹】完成云端上传（两条路径选一）

■ 路径甲　网页拖拽上传（零命令、零令牌，推荐）
　○ 打开仓库首页 https://github.com/qdc123456/-daily-news-brief
　○ 空仓库会显示 uploading an existing file，点它进入上传页（之后也可走 Add file → Upload files）
　○ 打开本地 github-upload 文件夹，全选里面的全部内容拖进上传区，务必保持 .github／workflows 目录结构
　○ 下方填提交信息，点 Commit changes
　○ 完成后回来说一声，助手会验证工作流文件确实在默认分支

■ 路径乙　一键脚本（不想拖拽时用）
　○ 生成 GitHub fine-grained PAT，权限勾 Contents: Read and write
　○ 设环境变量 GITHUB_PAT，在本目录执行 python deploy_to_github.py
　○ 脚本检测到仓库已存在，会自动建 main 分支并上传全部文件
　○ 用完请在 GitHub 撤销该令牌

■ 为什么不能由助手直接写入
　○ WorkBuddy 的 GitHub 连接是只读代理令牌，创建仓库与写入文件都返回 403
　○ 属平台授权范围限制，重连连接或改配置都无法绕过

■ 上传完成后只剩两步（只能本人操作）
　○ 第一步 填 Secret：打开 https://sct.ftqq.com/ 微信扫码拿 SendKey → 仓库 Settings → Secrets and variables → Actions → New repository secret → Name 严格填 SERVERCHAN_SENDKEY
　○ 建议顺手再建 WEWORK_WEBHOOK_URL 作备用通道
　○ 第二步 手动验证：Actions 页 → 每日新闻简报 → Run workflow → 再点一次 Run workflow，日志出现「Server酱推送成功」即成功
　○ 之后交给定时任务，电脑关机不影响
　○ 注意：Secret 保存后不可再查看，只能覆盖；它不会出现在任何日志或代码里

■ 如果第一次跑是红色失败
　○ 看日志最后几行：采集全是零条 → 源不通；全通道失败 → SendKey 没填或填错
　○ 失败内容已进 state.json 的 pending 队列，修好后下一次运行会自动补发


【贰】本次部署与方案文档的差异

■ 《每日新闻推送系统_云端架构方案.txt》是通用设计，主选写的是企业微信群机器人
■ 本次实际按你的选择部署为「Server酱 主通道 + 企业微信备用」
■ 想切换：把 config.yaml 里的 primary_channel 改成 wework 即可，代码不用动
■ Server酱 免费版每日五条，config.yaml 的 max_push_per_day 已设为二，安全


【叁】文件清单

　daily_brief.py　　　主脚本，逐行中文注释
　config.yaml　　　　 源清单、关键词、频率与通道配置
　requirements.txt　 云端自动安装的依赖
　.github／workflows／daily.yml　定时工作流
　state.json　　　　 幂等记录、待补队列、日计数器、源健康分
　.env.example　　　　本地调试用环境变量样例
　使用说明.txt　　　　部署、调试、报错对照
　每日新闻推送系统_云端架构方案.txt　完整架构设计与关键问题处理方向


【肆】三条必须记住的限制

■ GitHub Actions 定时不准时，只承诺「当天送到」，不承诺「七点三十整」
■ 仓库连续六十天没有任何活动，定时会被自动禁用；每两个月手动 Run workflow 一次即可保活（工作流回写 state.json 的提交也算活动）
■ 免费额度与第三方推送政策会变；备用通道与 GitHub 的失败邮件通知是兜底


【伍】许可说明

■ 本仓库内 daily_brief.py 等自研文件可自由使用与修改
■ 方案文档中引用的开源项目各有许可：TrendRadar 为 GPL-3.0、RSSHub 为 AGPL-3.0、newsnow 与 DailyHotApi 为 MIT；本仓库未包含它们的源码，仅作方案参考
