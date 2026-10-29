# Deployment 批量升级

依赖 Bash、Python 3、kubectl，以及可用的 kubeconfig。无需第三方 Python 包。

输入文件每行 `namespace deployment tag [container]`，支持空行、注释、Windows 换行。
严格按文件顺序逐个升级。同一 Deployment 不允许重复；多容器需第 4 列指定目标容器。
保留镜像仓库和名称，只替换 tag；digest 镜像拒绝处理。

```bash
# 查看 context，确认目标集群
kubectl config get-contexts
# 仅预检查，不修改集群（会创建本地日志和快照）
bash batch_upgrade.sh upgrades.example.txt --context your-context
# 生产升级会触发 Pod 替换，可能影响服务；确认清单、集群及发布策略后执行
bash batch_upgrade.sh upgrades.example.txt --context your-context --execute
```

默认单个 Deployment 发布超时 300 秒，完成后观察 60 秒，每 5 秒检查一次。

```bash
bash batch_upgrade.sh upgrades.txt --context your-context --execute \
  --timeout 600 --stable-seconds 120 --interval 5
```

升级前全量检查资源、初始副本健康、Pod 镜像及就绪状态和 patch 权限，保存 Deployment 快照。
执行时重新核对 UID、Pod 模板、replicas，并用 resourceVersion 条件更新，遇到并发变更停止。
每次等待 rollout 完成，再验证副本数、Pod Ready、各业务容器镜像和运行状态。
稳定观察期间 Pod 替换、容器重启、检查失败或超时，均立即停止，退出码为 1。
原镜像已等于目标镜像时不提交变更，仍执行验证。

脚本会对清单中的每个 Deployment 自动检查容器是否配置 readinessProbe：配置了就通过 Kubernetes Pod Ready 状态验证；
没有配置时记录提示并跳过探针存在性要求，但仍检查副本数、Pod Ready、容器运行状态和重启情况。
无 readinessProbe 时 Kubernetes 可能将容器直接视为 Ready，因此这些状态不能证明业务功能正常；
建议结合应用日志或业务指标观察。可选 `--health-command` 会在每个 Deployment 的稳定观察期间执行，返回 0 代表检查成功，
其他退出码或超时即停止。该命令在本机运行，默认超时 15 秒，输出不写入日志；它适用于清单中的全部业务，
可根据环境变量 `UPGRADE_NAMESPACE`、`UPGRADE_DEPLOYMENT` 等选择对应检查逻辑。

```bash
bash batch_upgrade.sh upgrades.txt --context your-context --execute \
  --health-command 'curl --fail --silent --show-error --max-time 10 "https://${UPGRADE_DEPLOYMENT}.${UPGRADE_NAMESPACE}.example.com/health"' \
  --health-timeout 15
```

可用环境变量：`UPGRADE_CONTEXT`、`UPGRADE_NAMESPACE`、`UPGRADE_DEPLOYMENT`、
`UPGRADE_CONTAINER`、`UPGRADE_IMAGE`。检查命令需自行保证只读、无副作用。

日志同时输出终端和 `logs/upgrade-时间-PID/upgrade.log`，可用 `--log-dir` 修改目录。
同目录保存升级前 Deployment JSON 和 `run.json` 执行状态清单；目录权限 700，文件 600。
快照可能包含环境变量等敏感配置，应妥善保管。
日志不自动收集 Events 和容器输出，以免泄露业务敏感数据；失败时记录 Deployment 简要状态。

失败后默认不自动回滚，已完成的升级保留。可用该次运行目录中的 `run.json` 一键恢复所有已尝试的变更，
包括失败时可能已提交的 Deployment；未开始的条目不会被修改。脚本先核对 context、Deployment UID 和当前镜像，
遇到资源重建或镜像漂移会拒绝回滚。通过全部检查后按逆序恢复镜像，再逐个等待 rollout。回滚发布失败时，
脚本仍会完成其余回滚等待并报告失败项。请求超时时可用同一命令重试；已恢复的项目会被跳过。

```bash
bash batch_upgrade.sh --context your-context \\
  --rollback-dir logs/upgrade-20261028-153000-12345
```

运行目录中的 `run.json` 记录每个 Deployment 的状态、原镜像和目标镜像。回滚命令只恢复镜像，不自动恢复
replicas 或其他 Deployment 配置；成功后仍需检查 Ready 状态并执行适合业务的健康验证。
脚本也会在失败日志中列出原镜像恢复命令，供手动恢复。配置快照用于审阅，不建议整体 apply，以免覆盖并发变更。

限制：不验证 tag 是否对应期望 digest，不处理 initContainer 镜像；相同 tag 不会强制重建 Pod。
发布超时是 rollout 等待时限，稳定观察和 API 请求耗时另计。
请避免并行运行升级脚本，并暂停会更改同一 Deployment 的发布系统或自动扩缩容任务。
# upgrades
