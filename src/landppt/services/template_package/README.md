# SVG 模板包生成

“创建 PPT”页（`/create`、`/scenarios`）的需求输入框下方提供“生成方式”。
选择快速模式后，必须选定一个已发布的模板包；一步创建接口会先校验版本权限与状态，
再把包版本和版式容量参考保存到确认需求，供后续大纲生成使用。
这与项目看板中的需求确认表单共用模板包校验逻辑。快速模式使用 SVG 绘制。
验证入口覆盖 `tests/test_create_package_mode.py`、`tests/test_package_outline_reference.py`；
2026-09-26 已在本地实际 `/create` 页面检查桌面/手机显示、列表加载、必选校验和提交参数，
截图位于 `artifacts/create-package-mode/`。浏览器提交使用拦截验证，未调用模型生成。

## 从本地 PPTX 创建模板包

模板管理 → 快速模式模板包 → **从 PPTX 创建**。上传 PPTX / POTX / PPSX 后，
使用当前用户的 `template_generation` 模型逐页识别标题、副标题、小标题、正文、图片和装饰。
AI 同时指定版式用途及要点分组；同一分组的小标题与正文绑定同一个 blocks 下标。
界面展示 AI 判断理由、容量和可编辑分组，用户确认后导入。每页必须有一个标题。
原主题文字可标为“清除”，只有明确选为“固定”的文字会留在底图。
API 默认 JSON 响应；`?stream=true` 提供 NDJSON 进度、完成或错误事件，浏览器显示阶段、页码和已用时间。
系统生成固定底图和可编辑 SVG 槽位，校验原文、短文和较长内容样例后保存草稿。
在工作区点击 **对照原稿**，检查字体、文字位置和配图层级，再校验并发布。
失败页及原因会单独显示，可以重新上传并只选择失败页修正。

契约 v2 的 `assets` 随版本保存在 manifest 中，PNG/JPEG 按 SHA-256 标识并验真，
不进入用户图库。固定图片通过 `data-asset` 引用；示例截图和配图也随包保存。
导出 v2 使用 ZIP（manifest.json + assets/），导入不向文件系统解压。
旧 JSON 包继续兼容，v1 内容哈希保持不变。固定底图不能通过 AI 编辑改色或移除。
槽位支持 `data-align=left|center|right`、`data-valign=top|middle|bottom`；
图片槽支持受控的 `data-crop=rect|circle|rounded`。

**渲染环境（必需）**：采用独立 LibreOffice 容器，关闭网络、只读根目录，限制内存、
CPU、进程数和转换时间。应用与渲染器通过专用临时目录交换文件，无 HTTP 渲染端口。

生产和开发 compose 已包含渲染服务，无需叠加旧的 compose 文件：

```sh
docker compose -f docker-compose-dev.yaml up -d --build
# 生产环境把第一个文件改为 docker-compose.yml；应用镜像也需包含本次代码。
```

本地 Python 应用先创建 `temp/pptx-render-spool`，然后启动：

```sh
docker compose -f docker-compose-pptx-renderer.yaml up -d --build
```

在应用环境中设置 `LANDPPT_PPTX_RENDER_SPOOL=temp/pptx-render-spool`。
Linux 本地运行时，额外设置 `LANDPPT_RENDER_UID` / `LANDPPT_RENDER_GID`
为应用进程的 UID/GID，确保容器能访问权限为 0700 的临时任务目录。
容器内安装所需字体后重新构建；导入会报告原稿声明但渲染环境缺少的字体。
浏览器端仍可能发生字体替换，因此预览确认不可省略。

当前边界：上传最多 50 MB / 100 页，单包最多 40 个组件；旧 `.ppt` 请先另存为
`.pptx`。非 16:9 原稿等比居中留白。页码字段会从底图删除，隐藏页保持明确的源页号。
小字号正文仍是正文，作为槽位时至少使用 18px。无法匹配 PDF 样式时使用 PPT 中的字号、字体、
粗细、RGB 颜色和文本框位置；缺失属性使用 18px、Noto Sans CJK SC、深色及左对齐的默认值。
这种情况仅显示非阻断提示，继续 AI 解析并保留可替换槽位，不把正文自动固定到底图。
底图渲染后检查可提取文字，只允许保留用户确认的固定文字；组合、图表或母版里残留的未确认文字会阻止该页导入。
此检查无法识别原本就在照片/扫描图里的文字；不支持的文字应先在原稿中转换为普通文本框。
AI 分析提取的文本、坐标和样式，不靠视觉模型重绘原稿。超大或无效字号不再中断提取：
保留原文与原字号，先采用合法槽位字号，将异常原因交给 AI 判断用途。超长正文不截断，
超过绑定字段上限时标记“需调整”。
生成版式时若容量、越界或重叠校验失败，自动调用当前用户的 `template_generation` 模型，
每页最多修复两次。AI 只能调整已存在槽位的位置、尺寸和字号，不能改原文、绑定、素材或底图；
文字保持至少 18px。每次修复后重新验证容量、原文样例及长短样例，只有通过校验的版式进入草稿。
页面显示“AI 修复”、源页码和尝试次数，报告指出哪些页面经过修复。单次模型请求最多等待 180 秒，
客户端断开时取消修复请求。修复可能改变原有布局，需在草稿中对照原稿确认。
AI 输出逐项校验对象 ID、类型及分组。一个分组有多段正文时，保留邻近标题的正文，其余段落成为独立槽位；
没有正文的小标题作为独立短要点，不删除原文字。编号不计入 20 个要点上限；识别结果超过
20 个要点时保留结果并标记“需调整”，选择页默认不勾选该页，不中断其他页面的 AI 分析。
创建和发布仍保留组件容量校验。配图最多 8 个，
超出时较小图片保留在底图并在候选说明中标注，用户仍可调整。数字与字符串形式的相同分组标签统一处理。
每页最多尝试 3 次；模型请求失败与内容校验失败分别提示，重试反馈包含缺少、未知或重复的对象 ID。
失败记录包含页码和尝试次数，仍失败时显示原因，不静默把整页设为固定背景。
动态图片默认关闭，启用后位于底图上方，复杂遮罩和前景装饰需人工检查。
底图保留原有的复杂图形，但这些图形不再是可单独编辑的 PPT 对象。
普通嵌入式 Excel、公式等 OLE 对象（包括 `.bin`）允许导入，作为固定底图内容，
不会将嵌入文件复制进模板包。VBA 宏组件仍会单独提示并拒绝导入。
母版/版式里的固定内容由 LibreOffice 渲染；没有实例页面的 POTX 暂不自动展开其 layouts。
多字体精确测宽、自由多边形裁剪、系统页码槽位及旧 PPT 自动转换尚未支持。

验证：`uv run --extra dev pytest tests/test_template_package_pptx_import.py`。

2026-09-27：已选纯文字版式的内容生成改为逐槽位输出，代码组装 PageContent；不再同时向模型提供
冲突的通用页面 schema。混合的“仅正文 / 小标题+正文”逐位置约束，拒绝多填、漏填及超容量，
不通过删减或合并模型成稿凑结构。大纲参考同时列出逐槽位约束。
旧包的栅格底图不可逆，已经烘焙的原主题正文不能通过重新生成文字移除，需要原 PPT 重新导入。
真实模型和 LibreOffice 验证入口：`python scripts/verify_pptx_ai_import.py --user-id <id>`，
使用指定用户的模型，产生模型调用费用，只写 artifacts，不修改用户项目。
本轮定向回归 135 项通过，JavaScript 语法及新增模块静态检查通过。
已使用真实模型和 LibreOffice 跑通 8pt 正文识别、底图清除及新主题内容渲染；
`scripts/verify_pptx_ai_browser.py` 在本地运行服务中验证进度、分组、草稿保存并回读哈希，
保存桌面/手机截图后删除该测试草稿。产物在 `artifacts/pptx-ai-import/`。
这不是用户原 PPT 的验证，也未重写已有项目的底图或已发布版本。

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
| AI 编辑已有模板包 | `POST .../packages/{version_id}/edit`（SSE，`prompt` 描述增删改要求） |
| 打开或创建编辑草稿 | `POST .../packages/{version_id}/draft` |
| AI 修改或新增单页 | `POST .../packages/{version_id}/components-ai`（SSE） |
| 删除、复制、移动单页 | `DELETE .../components/{id}`；`POST .../components/{id}/{duplicate,move}` |
| 恢复草稿内容（撤销） | `PUT .../packages/{version_id}/manifest` |
| 导出包、删除草稿 | `GET .../packages/{version_id}/export`；`DELETE .../packages/{version_id}` |
| 重命名、删除整个包 | `PATCH/DELETE /api/global-master-templates/package-templates/{template_id}` |
| 决策模型设置 | `GET/PUT .../packages/settings` |
| 测试当前决策配置（不保存） | `POST .../packages/settings/test` |
| 项目选择及状态 | `GET/POST /api/projects/{project_id}/template-package` |
| 内容修改 | `PUT /api/projects/{project_id}/package-pages/{slide_id}/content` |
| 合法版式 | `GET .../package-pages/{slide_id}/layouts` |
| 版式、图片、锁定、自由编辑 | `POST .../package-pages/{slide_id}/action` |
| 自然语言修改内容 | `POST .../package-pages/{slide_id}/edit` |

修改请求携带 `expected_revision`，冲突返回 409。所有资源接口检查登录用户和项目/模板/图片权限。`SlideData.template_id` 仍只用于原 `ppt_templates` 外键。

## 验证与边界

2026-09-27 导入进度可见性：当前状态、耗时和运行指示条移到弹窗底部的固定操作区，避免选页列表很长时滚动后看不到进度；生成中的按钮显示“正在生成…”。`uv run python scripts/verify_pptx_import_progress.py` 使用模拟导入流在桌面和手机验证 10 页列表各滚动位置，以及修复、计时、错误、重试和完成提示。截图保存在 `artifacts/pptx-import-progress/`，没有保存测试草稿。

2026-09-27 自动修复验证：相关回归 61 项通过；真实 LibreOffice + 用户配置模型跑通“超大秘字 + 过窄标题”的提取、AI 识别、槽位修复及完整校验，原文保持不变。浏览器使用模拟进度流验证“AI 修复”高亮、次数和完成状态。产物在 `artifacts/pptx-ai-repair/`，没有保存用户数据库草稿。K8s 第 6 页实测仍失败，第二次模型试图使用 16px 被拒绝；修复是有界尝试，不保证所有原稿自动适配。当前修复只处理可构建组件的槽位几何问题，不绕过残留底图文字、文件安全或内容契约检查。

2026-09-27 AI 导入修复：分组归一化保留多个正文对象，兼容数字分组，修复长分组名生成后缀时的死循环；超出 20 个要点的页面保留识别结果并标为“需调整”。模型请求失败和输出校验失败分别提示，最多尝试三次，取消不重试。使用本地 `k8s从入门到精通 - general.pptx` 和真实配置模型，15 页识别全部完成，结果保存在 `artifacts/pptx-import/k8s/analysis-report.json`。这不代表整包导入成功：后续实际模板校验仅第 15 页通过，其他 14 页仍有槽位容量问题，见同目录 `import-report.json`。降低字号的隔离实验仍有越界和重叠，未应用到正式规则。不得通过把正文固定到底图来绕过容量校验。

2026-09-25：模板包卡片“编辑 / 编辑草稿”打开三栏工作区：页面列表、实时预览、AI 多轮编辑。用户可针对整包或单页描述修改、删除、新增要求，使用当前用户的 `template_generation` 模型，流式调用复用 `ContentService`。整包编辑先规划操作；单页编辑直接处理所选组件。组件通过校验后即推送示例 HTML，在整轮保存前更新预览，未经校验的 SVG 不显示。服务端保留其他组件，逐项校验 SVG、槽位、容量和示例，再校验整包。计划和组件校验失败分别最多修复一次；任一项仍失败就不保存本轮编辑。禁止重复 ID、编辑不存在的版式、清空包和超过 40 个版式。

编辑已发布的自有包先创建下一版本草稿，系统共享包另存为用户草稿；后续各轮继续修改同一草稿，已发布版本及项目固定引用不变。草稿写入检查内容哈希，拒绝覆盖其他窗口的修改；发布、重命名、删除与修改按一致锁顺序执行。工作区支持单页复制、移动、删除、AI 修改/新增，以及本次会话内撤销；对话历史保留在当前窗口，各轮结果自动保存。发布后再次修改才创建新的草稿版本。

卡片“更多”提供重命名、导出 JSON、复制、删除草稿和删除整包；工作区也提供重命名与导出。重命名不会改写已发布 manifest，导出文件使用当前包名且可重新导入。删除被项目引用的模板包时，将其隐藏并停用公开版本，保留项目所需数据；无引用的包才物理删除。

AI 每轮沿用模板创建的积分检查和计费。SSE 每 15 秒发送心跳，断开时取消未完成模型请求；前端只有收到 `complete` 才确认保存，失败时回退临时预览并提示检查草稿。最后保存与网络断开同时发生时可能已保存草稿，因此不会自动重放编辑请求。

相关回归覆盖三类操作、多轮草稿修改、并发冲突、原包与项目引用保护、发布、权限与积分检查、恶意 SVG 拒绝、心跳与取消。浏览器 smoke 使用真实 API 与隔离 SQLite；工作区检查位于 `scripts/template_package_workspace_smoke.py`，由原 smoke 入口调用。模型输出使用模拟响应，未实测付费模型的设计质量。

本轮 `test_template_package_workspace.py`、`test_template_package_editor.py`、`test_template_package_integration.py` 合跑 **60 passed**。`template_package_smoke.py --ui-only` 已验证流式预览中途暂停/断开回退、两轮编辑同一草稿、单页操作与撤销、重命名、实际 JSON 下载、发布和删除后的项目引用保护。桌面/手机截图为 `package-workspace-desktop.png`、`package-workspace-mobile.png`，检查缩略图比例、容器宽度和移动端预览/对话区无重叠。

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
