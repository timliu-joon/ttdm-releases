# v138.120.64 环境配置工具

本工具只配置新的冷态交付候选，不启动服务、不访问网络、不改游戏程序、资源、版号或数据库。目标版本固定为 v138.120.64。只能由 WorkBuddy 在部署环境使用。本机正式根与隔离工程继续使用本机地址。

1. 按本次增量与适用基线重建新的完整目标，先核交付的完整目标清单。不要覆盖运行中的旧根。
2. 准备参数：`--root` 为新候选完整根；`--source-manifest` 和 `--source-manifest-sha256` 使用本版交付清单与准确校验值；`--public-address` 为 WorkBuddy 核定的地址；四端口通过 `--reception-port`、`--game-port`、`--world-port`、`--feed-port` 指定，既有值为 6666、10003、12000、18080。
3. 新增必填 `--previous-stable-root`：指向既有 v137 托管的 stable 目录，例如 `D:\Server\releases\v137.119.64\服务端\update-feed\stable`。工具只读该目录，按已消费回执固定 SHA 验证 patch136、patch137，将原字节复制进新候选。不得用本机中性补丁替代，不能改写旧目录，也不重新编码这两个已发布补丁。
4. `--backup-dir` 与 `--receipt-dir` 为两个尚不存在、相互独立且位于候选根之外的绝对目录。工具先验证全部前提再写入，保存修改前文件及完整差异回执。
5. 只有 patch138 做环境转换；它包含原生 FDDA 配置及 setting.wdf 的 IDOM 差异，非环境有效载荷必须完全不变。历史文件身份不符时停止，不放宽校验，不重新生成旧补丁。
6. 配置清单中的客户端 `data/update.ini`、`data/areaserver.xml`，服务端 `config/server.template.json`、可选 `config/server.json`，`update-feed/stable/serverlist.txt`、`patchlist.txt` 与本版 DT 均由工具处理；feed/runtime 清单仅同步派生长度及 SHA。秘密只在云端注入 server.json，保留已有密钥，并按实际字节更新 runtime manifest。
7. 工具处理的是交付初始候选。部署前冷停旧服务、私有备份并迁移现有云端数据库、账号、角色和存档。**不清库，不以候选内初始种子覆盖玩家库；此前一次清库已执行，不得重做。**
8. 生成最新版完整客户端下载包、保留紧邻上一版，托管不可变原生更新链。新版实际业务与下载验收后按现行规范清理更早完整客户端，不删除必要更新链或玩家数据。回传版本、时间、原包身份、环境差异、最终包及各 DT 身份、实际验证结果。

`TEST-IMMUTABLE-HISTORY-RESULT.json` 是离线工具验证，不代表云端部署或玩家业务验收。
