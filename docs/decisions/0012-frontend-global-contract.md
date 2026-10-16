# 0012 前端不做 ES 模块化，改为「可机器校验的全局契约」

状态：生效（替代优化计划中原定的「前端 ES 模块化」条目）

## 决策

`static/*.js` 保持**经典 `<script defer>` 脚本**，不改成 ES 模块、不引入打包器。
作为交换，把「页面内所有脚本共享一个全局作用域」这份隐式契约变成**可机器校验的约束**，
由 `tests/test_fitness_functions.py` 三个守卫强制：

- `TestInlineHandlerWiring.test_inline_handlers_resolve_to_page_globals`
  —— 页面 HTML 与其脚本中每个内联处理器（`onclick="foo()"` 等，见 `_INLINE_HANDLER`）
  调用的名字，必须是该页面脚本里的顶层声明（`_globals_of`）。
- `TestGlobalShadowing.test_no_global_symbol_shadowing`
  —— 局部声明/循环变量/回调参数不得遮蔽同名全局（历史上有 15 处遮蔽 `t()`，
  其中 2 处引爆线上：口试收尾按钮不出现、概览页后半段不渲染）。
- `TestGlobalShadowing.test_global_extractor_covers_all_declaration_forms`
  —— 自检全局提取器覆盖 `function` / `async function` / `const|let|var` / `window.x =` / `class`。

## 原因

- **收益与风险不成比例。** 全量转换要处理 3 个页面、12 个脚本文件、约 390 个顶层声明、
  251 处内联处理器（166 个不同名字）。改成模块后，模块作用域不再自建全局，
  这 166 个入口和其余跨文件互调都必须显式 `import` 或显式挂 `window`，
  一次性改动面≈整个前端。
- **没有 JS 测试基建来兜底。** 本项目零第三方依赖、无构建步骤、无 JS 测试框架
  （ADR 0001；按 AGENTS.md 不引入主路径依赖）。模块化重构属于「无法被现有测试证伪」的改动，
  一旦破坏，只能靠人工点检发现——正是本 ADR 要消除的失败模式。
- **模块化解决的是「隐式」，而隐式可以被检查出来。** 真实痛点不是「没用 import」，
  而是「拼错/遮蔽后静默失效」。上面三个守卫直接覆盖该类缺陷，成本是几十行测试，
  且新增脚本自动纳入（`_page_scopes()` 直接解析 `<script src>`，不维护清单）。
- **顺带修掉一个真实盲点。** 原 `_DECL_TOP` 只认 `function`/`const|let|var`，
  所有 `async function foo()`（以及 `window.x =`、`class`）都不在全局表里，
  遮蔽检测对它们**静默失效**；`_DECL_NESTED` 也不认嵌套函数声明。已修正并加自检。

## 后果

- 前端继续零构建：直接双击/静态托管即可运行，改动可热刷新，无需 `npm`。
- 「全局函数」是页面级公共 API，**新增内联处理器必须保证名字是该页顶层声明**，
  否则测试失败（堵死「死按钮」这类静默失效）。
- 内联处理器只认**首个调用名**（`onclick="a(); b()"` 只校验 `a`），
  多语句处理器里的后续调用不在守卫范围内——如需覆盖，应拆成单个函数调用。
- 若将来真要模块化（例如前端规模再翻倍、或需要 tree-shaking），
  必须先补齐 **JS 侧测试基建**，本 ADR 才可被推翻：届时状态改为「已废弃」并链接新决策。
