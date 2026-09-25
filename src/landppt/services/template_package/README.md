# SVG 模板包生成

模板包已接入全局模板管理、模板选择、页面生成、内容编辑和现有导出入口。数据库迁移 `019` 保留普通模板的 `single` 默认值，新增包版本、项目运行状态、页面状态和内容版本表。

## 使用

1. 启动应用，生成并确认大纲。
2. 在模板选择页面选择“快速模式生成”，点击“添加内置模板包”，或导入包 JSON、使用“AI 创建模板包”。AI 设计保存为草稿，预览并发布后才能选择；模板管理页面也提供包管理入口。
3. 配置配图开关和全篇图片上限，在已发布版本上点击“使用此模板包快速生成”，自动进入现有生成器。生成器顶部显示当前生成方式，并提供快速模式设置、自由设计和 Jev 配置入口。
4. 在编辑器顶部工具栏“快编”旁使用“内容与版式”，修改文字、应用版式、替换图片、锁定页面，或转为自由编辑。该按钮仅在模板包项目中显示，随工具栏布局，不在画布上悬浮。
5. 沿用 PDF、PPTX、放映和讲解视频入口。SVG 原生对象 PPTX 保留可转换的文字和形状，复杂部分使用矢量回退。

模板管理页使用“普通模板”和“快速模式模板包”两个 Tab，分别显示各自的列表与操作。默认进入普通模板；`/global-master-templates?tab=packages` 可直接打开模板包，刷新保留当前 Tab，支持左右方向键及 Home/End 切换。

内置 `editorial` 包包含封面、章节、要点、对比、流程、图文、指标、总结八类版式，共十个组件，画布为 1280 × 720。包不携带任意脚本；额外字段、漏绑正文、未授权图片及排版溢出都会报错。第一版契约使用正文块、指标与配图意图，独立 `chart_data` 尚未支持，提交时会明确拒绝。

## 流程与一致性

- `catalog.py` 管理用户范围、导入、复制、版本草稿、校验、发布和停用。发布后的 manifest 不原地修改；项目固定引用版本，停用不影响已有引用。普通 HTML 模板接口过滤模板包。
- `content_service.py` 每批扩写 1–5 页最终成稿，携带大纲、资料、来源标识和容量；只重试失败页。内容与模型用量持久化。自动修复不允许改动指标、正文数字和来源。
- `selector.py` 先检查容量、字段和图片硬约束，再用 Jev、当前模型或规则选择组件。束搜索携带最近八页的实际组件，考虑连续重复、使用频率和版式家族；批次内的保存/锁定页也计入邻居。允许近似适配的候选参与变化，但不会为了变化选择明显较差的候选。`selection_report` 记录候选数、仅有一个合法候选的页面及回退调用，已显示页面不会自动重排。
- `workflow.py` 分支早于自由 SVG 生成。确定组件后复用图片服务，跳过重复需求规划；绑定失败时尝试同包候选、有限内容修复和自由 SVG 回退。最终失败的页面保持待处理状态。
- `storage.py` 使用用户范围、短事务、页面修订号和运行租约。重复连接及取消续跑复用有效内容、素材和成页；成功提交与积分扣除同事务执行。零余额仍可复用已付费页面。
- 稳定页面 ID 随重排保留。生成期间大纲或资料改变时拒绝旧结果；手动编辑和锁定优先于后台生成。
- 文字和图片编辑先检查渲染，再原子提交，失败保留旧内容与 HTML。内容变化使讲稿和音频失效；只换版式保留音频并标记视频过期。恢复包控制时保存先前页面版本。
- `follow.py` 按数据库修订号推送 SSE，支持重连、取消和中断提示。进度及完成状态同样检查租约，失效任务不能标记项目完成。

快速模式的进度事件使用 `completed` 表示当前已保存且可用的页面数量，阶段说明放在 `message`；普通模式的 `current` 仍表示正在生成的页码。失败页不计入完成数，部分失败不强制显示 100%。SSE 即使阶段说明未变化，也会推送完成数变化。

部分页面失败但任务已结束时，SSE 发送 `complete` + `partial=true`，携带实际成功数和失败页码；数据库阶段仍保持失败，避免后续流程把缺页当成完整结果。前端保留具体失败说明、显示“补生成失败页”，关闭心跳与待执行的重连计时器。终态之后的旧连接回调不再覆盖状态；连接徽标也不覆盖正文错误。浏览器测试回放完整成功、部分失败和错误事件，再推进 35 秒验证不会假报连接丢失。

内容批次中的成功页先落库，再单页重试失败内容；重试期间取消后，恢复会复用已保存的成稿。内容修复超时、响应解析失败或返回非法配图变更时，保留上一版有效成稿，耗尽配置的修复次数后才尝试已启用的自由 SVG 回退。取消、租约冲突和持久化错误不进入模型回退。最终错误保留排版、修复和回退各阶段的原因。读取快照也对可重试的数据库锁冲突执行有限重试。

快速模式的内容扩写、内容修改/修复、模型版式选择和自由 SVG 回退均使用流式模型调用，由服务端汇总完整结果后再执行原有校验。OpenAI 兼容接口发送 `stream=true`，请求末尾的用量信息；必须收到正常结束标记，空响应、截断和网络中断不会作为完整结果保存。Responses 模式也使用流式接口并检查完成状态。超时或取消会关闭 OpenAI HTTP 流，失败仍交给现有有限重试/回退处理，不自动切回非流式。其他供应商复用各自的文本流接口，用量不可用时记录空值而非估算。页面仍按校验完成后逐页显示，不展示半成品 JSON/SVG。

流式请求可减少等待整份响应时触发代理读取超时的情况，但上游必须及时发出数据；若长时间没有首个数据或后续数据，Cloudflare 仍可能返回 524。应用中配置的模型超时继续生效。

## 决策模型设置

“系统配置 → 决策模型配置”（保留原有 `/ai-config#jev-config` 链接）用于类似 Jev 的版式决策，支持自定义完整端点 URL、模型名称、API Key、超时、重试和并发上限（1–16），模板选择页和生成器均保留直达入口。此值控制决策评分请求，并非图片或内容模型的并发。设置按用户保存，读取接口不返回密钥；留空保留已保存密钥，输入新密钥可替换。原有 `jev_*` 配置保持兼容，旧配置默认使用 Jev 协议及原 TypeSafe 地址。

协议可选 Jev / TypeSafe（`state`、`questions`、`answers.noul`）或 OpenAI 兼容 Chat Completions（服务端构造评分提示词，校验每个候选的 0–1 分数）。端点按填写的完整 URL 请求，不自动追加路径。“测试连接”使用当前表单发起一个样例评分请求，无自动重试，不保存配置，不使用用户项目内容；会验证响应结构及分数，返回模型名称和耗时。支持复用已保存的密钥，并分别提示认证失败、HTTP 错误、超时和协议不兼容。测试成功不代表模型在实际材料上的选择质量。

`jev_client.py` 复用连接池，每请求最多 40 个问题。限流、服务端错误、网络超时和非法响应按配置独立重试，采用抖动退避，并参考 `Retry-After`（最多等待 60 秒）；限流等待由同一客户端的并发请求共享。部分批次失败保留成功结果，仅为未完整评分的页面调用后备选择器。取消时等待所有工作任务退出。

数据库写入遇到 SQLite busy/locked、PostgreSQL 序列化失败或死锁时，回滚后有限重试；不重放结果不明的连接错误。租约续期可从短暂数据库错误恢复，续期失败会被主流程检查，过期租约不能复活。SSE 读取短暂失败重试，启动阶段允许 30 秒等待领取租约。只有一个合法组件时仍允许重复，不通过删减内容或绕过锁定伪造多样性。

协议核对：[TypeSafe API](https://docs.typesafe.ai/api)，2026-09-23。使用 `/v1/systemone` 和 `noul` 问题；问题正文明确引用页面及候选，不依赖问题键名。记录响应模型与 token 用量，校验评分类型、范围及完整性，没有虚构 confidence 或固定适配阈值。

## 接口

| 用途 | 入口 |
|---|---|
| 包列表、导入或新版本 | `GET/POST /api/global-master-templates/packages` |
| 安装内置包 | `POST .../packages/builtin` |
| 详情与样例预览 | `GET .../packages/{version_id}`、`.../preview/{component_id}` |
| 校验、发布、停用 | `POST .../packages/{version_id}/{validate,publish,retire}` |
| AI 创建设计 | `POST /api/global-master-templates/packages-generate`（SSE） |
| 决策模型设置 | `GET/PUT .../packages/settings` |
| 测试当前决策配置（不保存） | `POST .../packages/settings/test` |
| 项目选择及状态 | `GET/POST /api/projects/{project_id}/template-package` |
| 内容修改 | `PUT /api/projects/{project_id}/package-pages/{slide_id}/content` |
| 合法版式 | `GET .../package-pages/{slide_id}/layouts` |
| 版式、图片、锁定、自由编辑 | `POST .../package-pages/{slide_id}/action` |
| 自然语言修改内容 | `POST .../package-pages/{slide_id}/edit` |

修改请求携带 `expected_revision`，冲突返回 409。所有资源接口检查登录用户和项目/模板/图片权限。`SlideData.template_id` 仍只用于原 `ppt_templates` 外键。

## 验证与边界

2026-09-25 流式调用改造：`test_package_streaming_completion.py` 新增 12 项全部通过，使用真实 OpenAI SDK 与模拟 HTTP SSE 检查请求参数、碎片 JSON、用量、缺失结束标记、截断、网络错误、超时/取消关闭连接，以及 SVG 回退入口。与现有模型接口和模板包恢复测试合跑为 95 passed、2 failed；两个大纲提示词断言失败已存在于 `artifacts/template-package/pytest-existing.log` 基线。未调用真实 `mapi.landppt.com`，未部署运行镜像。源码挂载部署需重启应用进程；镜像部署需更新包含修改的镜像后重新创建容器。

2026-09-25：快速模式中断恢复与进度修复定向回归 279 passed（模板包、选择器、SVG 流水线、自由模板、降级和无人值守相关测试）。新增覆盖修复失败后的重试/回退、取消不触发回退、同批成功内容提前保存、仅补失败页、真实 SQLite 读锁恢复，以及同阶段下完成数变化。模型响应采用模拟，未调用真实 Jev 或内容服务；本轮 Docker 未运行。

```powershell
uv run --extra dev pytest tests/test_template_package.py tests/test_template_package_integration.py tests/test_svg_page_pipeline.py tests/test_slide_generation_degradation.py tests/test_unattended_mode.py -q
uv run --extra dev pytest tests/test_package_selection_resilience.py tests/test_template_package_integration.py tests/test_template_package.py tests/test_free_template_streaming.py -q
uv run python scripts/template_package_preview.py
uv run python scripts/template_package_smoke.py
uv run python scripts/template_package_smoke.py --ui-only
```

`template_package_smoke.py` 使用隔离 SQLite、真实包 API、Chromium 和现有导出器。产物在 `artifacts/template-package-integration/`：桌面和手机预览、内容编辑截图、10 页 PDF、原生/矢量 PPTX、静默视频及 `report.json`。PPTX 检查页数和可编辑文字对象；PDF 检查逐页可搜索文字。

`--ui-only` 验证完整系统配置页的 Jev 保存与回读、完整模板选择页的模式切换、完整生成器的自动启动，以及内容编辑。生成器接收模拟 SSE 事件，验证入口衔接、2/9 页的阶段提示与部分失败进度，不代表真实模型生成；外部 CDN 在离线测试中禁用。结果写入 `ui-report.json`，页面截图包含 `jev-settings.png`、`catalog.png`、`fast-generator.png` 和 `fast-generator-progress.png`。

模型扩写、Jev、AI 设计及图片服务的自动化测试使用模拟响应，不调用付费模型。SQLite 迁移及幂等性已验证，PostgreSQL 表定义编译已覆盖。生产 PostgreSQL 实机迁移、真实模型效果、付费图片与配音、端到端提速及成本对比仍需配置相应服务后实测。浏览器产物不能替代 PowerPoint/LibreOffice 对导出文件的视觉验收。

2026-09-23 群笔记服务曾不可用；2026-09-24 已恢复访问并同步当前实现范围，详细验证方式以本文件为准。

2026-09-24：选择器与恢复能力定向回归 100 passed；浏览器验证并发 16 的保存/回读及原有入口通过。覆盖近似适配候选的多样性、弱候选排除、跨批次/锁定邻居、超过四路的小矩阵并发、部分失败保留结果、429/504/529/超时/非法响应重试、取消清理、真实 SQLite 写锁恢复、租约失效和 SSE 短暂读取失败。未使用真实 Jev 服务，也未复现用户现场的具体报错。

2026-09-23 验证结果：上述定向回归 231 passed；正式 `tests/` 全量 1042 passed、43 failed、3 skipped。与首批保留的 `artifacts/template-package/pytest-existing.log` 对比，失败用例集合完全相同，没有新增失败。全量运行打印总结后仍有残留进程，已清理；定向回归和浏览器/导出 smoke 均正常退出。Black、isort（black profile）、flake8（忽略与 Black 冲突的 E203/W503 及 E501）、JavaScript 语法检查通过。
