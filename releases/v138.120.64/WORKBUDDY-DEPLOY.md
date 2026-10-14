# v138.120.64 WorkBuddy 部署交接

本版公共交付只含 `ttdm-v137.119.64-to-v138.120.64-update.zip`、清单/说明和经独立审查的部署配置工具。完整客户端与完整服务端快照仅用于本地内容核验，没有公共下载 URL。v137 也没有公开完整客户端或服务端 ZIP，禁止编造基线 URL。

## 1. 冷态重建

1. 使用留存的中性 v137.119.64 原始产品作为基线；若没有该中性完整快照，用已封 v136 完整原包加已发布 v136→v137 增量重建，并先核 v137 完整清单。不能把已配置公网地址或含玩家数据的运行目录当成中性基线。将本版增量应用到全新冷态中性候选，再核 `package-full-manifest.tsv`；要求 `服务端/logs/` 存在且为空。
2. 全量校验和下节环境配置完成后，精确冷停旧服务、私有备份，再将现有玩家数据库、账号、角色和存档迁入新候选。配置前不要迁入玩家库，否则中性清单校验会失败；候选初始种子不得覆盖现有玩家数据。
3. `INC-2026-10-14-02` 唯一清库已经执行，禁止重放。任何部署重试继续采用 `PRESERVE_EXISTING`。

## 2. 公网环境投影

1. 使用同目录精确的 `Configure-Deployment.py`，SHA-256 `32B368ABD09159BC35DA4826E314FC47982255D42C43D6445F232FA6DB1934CA`，按 `Configure-Deployment-README.md` 执行。
2. `--previous-stable-root` 必须指向云端上一版实际托管的 v137 stable 目录。工具只读并逐字节复用已发布的 patch136、patch137；环境差异只允许写入新 patch138 及其派生清单。旧补丁不得重编码、覆盖或替换。
3. 输入候选在投影前保持 loopback。公网地址、端口和会话密钥只在云环境注入；任何密钥、完整配置、玩家数据和原始日志都不得进入 GitHub 回执。

公网配置的准确位置如下；使用同版工具处理，保留版本号与玩家分辨率：

| 文件 | 公网字段 | 默认端口 |
|---|---|---|
| 客户端/听唐渡梦/data/update.ini | ServerList、UpdateURL、NoteFile | 更新 HTTP 18080 |
| 客户端/听唐渡梦/data/areaserver.xml | ReceptionServer.Address/Port、Socket T/N/E Address/Port | 接待 6666、注册登录 10003 |
| 服务端/config/server.template.json → server.json | listen_address、redirect_address、native_feed_listen_address；rcp_port、gcc_port、redirect_port、native_feed_port | 6666、10003、世界 12000、18080 |
| 服务端/update-feed/stable/serverlist.txt | ReceptionServer、Socket T/N/E 地址/端口 | 与客户端一致 |
| 服务端/update-feed/stable/patchlist.txt 与新 patch138.120.64.dt | 补丁 URL；其中 data/update.ini、data/areaserver.xml | 玩家从服务器获取 |

配置后重算 feed-manifest.tsv 与 runtime-manifest*.json 的派生长度/哈希；旧 patch136/137 按前述原字节复用，不能改写。

## 3. 发布与保留

1. 玩家从云端 feed 获取原生更新，不从 GitHub 获取玩家补丁。GitHub 只是本次增量和交接元数据通道。
2. 云端生成并托管 v138 最新完整客户端，同时保留紧邻上一版 v137 完整客户端作为回退。新版下载与业务验收通过前不得清理旧版。
3. 验收通过后只清理更早的完整客户端副本；patch136、patch137、patch138 等必要更新链继续保留，数据库与玩家数据继续保护。
4. 记录新客户端实际下载、旧客户端实际自动更新、第二次启动不重复、注册登录进入、重登位置、礼包专员商店、两版完整客户端库存和清理结果。未实际执行的项目保持 `NOT_RUN`。

本机正式验收、公共文件准备、GitHub 发布、云端部署、玩家原生更新和另一电脑验收是不同阶段。当前文档不声称 GitHub、云端或另一电脑已经通过。
