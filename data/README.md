# data 目录说明

`data/stars.json` 在本仓库 `main` 分支中保留为迁移兼容入口，主要用于以下场景：

- Fork 用户首次运行 GitHub Actions 时，若 `gh-pages` 尚未初始化，可回退读取该文件，但 Fork 后第一次建议手动运行 Action 选择强制重建=true。
- 同步脚本运行过程中会临时使用该文件作为本地数据缓存。

当前推荐的长期数据源与发布目标为：

- `gh-pages/data/stars.json`

说明：

- 常规 Action 流程不会将 `data/stars.json` 提交回 `main`。
- 如果你已完成迁移，可在你自己的仓库中选择删除 `main` 下的 `data/stars.json`。
