#!/usr/bin/env python3
"""Sequential Kubernetes Deployment image upgrades, with fail-closed checks."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


class UpgradeError(Exception):
    pass


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("file", type=Path, nargs="?", help="每行 namespace deployment tag [container]")
    p.add_argument("--context", help="必须显式指定目标 context")
    p.add_argument("--rollback-dir", type=Path, help="回滚指定升级运行目录中的已尝试变更")
    p.add_argument("--execute", action="store_true", help="实际升级；默认仅预检查")
    p.add_argument("--timeout", type=positive, default=300, help="单次发布超时秒数")
    p.add_argument("--stable-seconds", type=positive, default=60)
    p.add_argument("--interval", type=positive, default=5)
    p.add_argument("--health-command", help="可选业务检查 Shell 命令，返回 0 表示成功")
    p.add_argument("--health-timeout", type=positive, default=15)
    p.add_argument("--log-dir", type=Path, default=Path("logs"))
    return p.parse_args()


class Runner:
    def __init__(self, args):
        self.a = args
        self.current = None
        self.changed = []
        self.rows = []
        args.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.directory = args.log_dir / f"upgrade-{stamp}-{os.getpid()}"
        self.directory.mkdir(mode=0o700)
        self.logfile = (self.directory / "upgrade.log").open("x", encoding="utf-8")
        os.chmod(self.directory / "upgrade.log", 0o600)
        self.manifest = self.directory / "run.json"
        self.log(f"日志与快照目录: {self.directory.resolve()}")

    def save_manifest(self, rows):
        temp = self.manifest.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"context": self.a.context, "rows": rows}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.manifest)

    def log(self, message):
        line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
        print(line, flush=True)
        self.logfile.write(line + "\n")
        self.logfile.flush()

    def kubectl(self, *args, json_output=False, timeout=30):
        cmd = ["kubectl", "--context", self.a.context, "--request-timeout=15s", *args]
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise UpgradeError(f"kubectl 执行失败: {exc}") from exc
        if result.returncode:
            raise UpgradeError(f"kubectl {' '.join(args[:4])} 失败: {result.stderr.strip()}")
        if json_output:
            return json.loads(result.stdout)
        return result.stdout.strip()

    def get(self, ns, kind, name=None):
        parts = ["-n", ns, "get", kind]
        if name:
            parts.append(name)
        return self.kubectl(*parts, "-o", "json", json_output=True)

    def read_rows(self):
        rows, seen = [], set()
        for line_no, line in enumerate(self.a.file.read_text(encoding="utf-8-sig").splitlines(), 1):
            parts = line.split("#", 1)[0].split()
            if not parts:
                continue
            if len(parts) not in (3, 4):
                raise UpgradeError(f"第 {line_no} 行需要 3 或 4 列")
            ns, name, tag = parts[:3]
            dns = r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
            if not re.fullmatch(dns, ns) or not re.fullmatch(dns, name):
                raise UpgradeError(f"第 {line_no} 行 namespace/deployment 格式错误")
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
                raise UpgradeError(f"第 {line_no} 行 tag 格式错误")
            if (ns, name) in seen:
                raise UpgradeError(f"重复 Deployment: {ns}/{name}")
            seen.add((ns, name))
            rows.append(dict(ns=ns, name=name, tag=tag, container=parts[3] if len(parts) == 4 else None))
        if not rows:
            raise UpgradeError("输入文件没有升级条目")
        return rows

    @staticmethod
    def healthy_deployment(d):
        spec, status = d["spec"], d.get("status", {})
        count = spec.get("replicas", 1)
        if spec.get("paused") or count <= 0:
            raise UpgradeError("Deployment 已暂停或 replicas=0")
        if status.get("observedGeneration", 0) < d["metadata"].get("generation", 1):
            raise UpgradeError("Deployment controller 尚未处理最新配置")
        for field in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"):
            if status.get(field, 0) != count:
                raise UpgradeError(f"Deployment {field}={status.get(field, 0)}，期望 {count}")
        if status.get("unavailableReplicas", 0):
            raise UpgradeError("Deployment 存在不可用副本")

    def healthy_pods(self, row, deployment):
        ns = row["ns"]
        uid = deployment["metadata"]["uid"]
        rs_uids = {rs["metadata"]["uid"] for rs in self.get(ns, "replicasets")["items"]
                   if any(o.get("uid") == uid and o.get("controller") for o in rs["metadata"].get("ownerReferences", []))}
        pods = [p for p in self.get(ns, "pods")["items"]
                if not p["metadata"].get("deletionTimestamp") and any(
                    o.get("uid") in rs_uids and o.get("controller") for o in p["metadata"].get("ownerReferences", []))]
        if len(pods) != deployment["spec"].get("replicas", 1):
            raise UpgradeError("实际 Pod 数与期望副本数不一致")
        wanted = {c["name"]: c["image"] for c in deployment["spec"]["template"]["spec"]["containers"]}
        restarts = {}
        for pod in pods:
            name = pod["metadata"]["name"]
            status = pod.get("status", {})
            if status.get("phase") != "Running" or not any(
                    c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
                raise UpgradeError(f"Pod {name} 未 Ready")
            actual = {c["name"]: c["image"] for c in pod["spec"]["containers"]}
            states = {c["name"]: c for c in status.get("containerStatuses", [])}
            for container, image in wanted.items():
                state = states.get(container, {})
                if actual.get(container) != image or not state.get("ready") or "running" not in state.get("state", {}):
                    raise UpgradeError(f"Pod {name}/{container} 镜像或运行状态不符合预期")
            restarts[pod["metadata"]["uid"]] = tuple(sorted(
                (c["name"], c.get("restartCount", 0)) for c in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])))
        return restarts

    def business_check(self, row):
        if not self.a.health_command:
            return
        env = dict(os.environ, UPGRADE_CONTEXT=self.a.context, UPGRADE_NAMESPACE=row["ns"],
                   UPGRADE_DEPLOYMENT=row["name"], UPGRADE_CONTAINER=row["container"], UPGRADE_IMAGE=row["new"])
        try:
            # User-supplied command is an explicit opt-in. Do not log its output: it may contain secrets.
            result = subprocess.run(["bash", "-o", "pipefail", "-c", self.a.health_command], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=self.a.health_timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise UpgradeError(f"业务检查无法完成: {exc}") from exc
        if result.returncode:
            raise UpgradeError(f"业务检查失败，退出码 {result.returncode}")

    def preflight(self):
        rows = self.read_rows()
        self.rows = rows
        cluster = self.kubectl("config", "view", "--minify", "-o", "json", json_output=True)
        self.log(f"目标 context={self.a.context}; server={cluster['clusters'][0]['cluster']['server']}")
        for row in rows:
            ns, name = row["ns"], row["name"]
            self.current = row
            d = self.get(ns, "deployment", name)
            self.healthy_deployment(d)
            containers = d["spec"]["template"]["spec"]["containers"]
            if row["container"] is None:
                if len(containers) != 1:
                    raise UpgradeError(f"{ns}/{name} 有多个容器，必须在第 4 列指定 container")
                row["container"] = containers[0]["name"]
            matches = [(i, c) for i, c in enumerate(containers) if c["name"] == row["container"]]
            if len(matches) != 1:
                raise UpgradeError(f"{ns}/{name} 找不到指定容器")
            index, container = matches[0]
            missing_probes = [c["name"] for c in containers if not c.get("readinessProbe")]
            if missing_probes:
                self.log(f"注意：{ns}/{name} 容器 {','.join(missing_probes)} 未配置 readinessProbe；跳过探针存在性要求，仍检查 Kubernetes Pod Ready/容器状态")
            else:
                self.log(f"{ns}/{name} 所有容器均配置 readinessProbe，将检查 Kubernetes Pod Ready 状态")
            old = container["image"]
            if "@" in old:
                raise UpgradeError(f"{ns}/{name} 使用 digest 镜像，不能仅用 tag 安全替换")
            repository = old.rsplit(":", 1)[0] if ":" in old.rsplit("/", 1)[-1] else old
            row.update(old=old, new=f"{repository}:{row['tag']}", uid=d["metadata"]["uid"], index=index,
                       template=d["spec"]["template"], replicas=d["spec"].get("replicas", 1))
            self.healthy_pods(row, d)
            if self.kubectl("-n", ns, "auth", "can-i", "patch", f"deployments.apps/{name}") != "yes":
                raise UpgradeError(f"没有 patch {ns}/{name} 的权限")
            snapshot = self.directory / f"{ns}__{name}.json"
            with snapshot.open("x", encoding="utf-8") as f:
                os.chmod(snapshot, 0o600)
                json.dump(d, f, ensure_ascii=False, indent=2)
            self.log(f"预检查通过 {ns}/{name} container={row['container']}: {old} -> {row['new']}")
        self.current = None
        for row in rows:
            row["state"] = "pending"
        self.save_manifest(rows)
        return rows

    def upgrade(self, row):
        self.current = row
        ns, name = row["ns"], row["name"]
        self.log(f"开始 {ns}/{name}")
        d = self.get(ns, "deployment", name)
        self.healthy_deployment(d)
        if d["metadata"]["uid"] != row["uid"] or d["spec"]["template"] != row["template"] or d["spec"].get("replicas", 1) != row["replicas"]:
            raise UpgradeError("资源在预检查后发生变化，停止升级")
        self.healthy_pods(row, d)
        if row["old"] != row["new"]:
            patch = [dict(op="test", path="/metadata/resourceVersion", value=d["metadata"]["resourceVersion"]),
                     dict(op="test", path=f"/spec/template/spec/containers/{row['index']}/image", value=row["old"]),
                     dict(op="replace", path=f"/spec/template/spec/containers/{row['index']}/image", value=row["new"])]
            # Record before request: a timeout can leave the server-side outcome unknown.
            self.changed.append(row)
            row["state"] = "attempted"
            self.save_manifest(self.rows)
            self.kubectl("-n", ns, "patch", "deployment", name, "--type=json", "-p", json.dumps(patch))
        else:
            self.log("镜像已经是目标版本，仍执行健康验证")
        output = self.kubectl("-n", ns, "rollout", "status", f"deployment/{name}",
                              f"--timeout={self.a.timeout}s", timeout=self.a.timeout + 30)
        self.log(output)
        deadline = time.monotonic() + self.a.stable_seconds
        baseline = None
        while True:
            d = self.get(ns, "deployment", name)
            expected = dict(row["template"])
            expected = json.loads(json.dumps(expected))
            expected["spec"]["containers"][row["index"]]["image"] = row["new"]
            if d["metadata"]["uid"] != row["uid"] or d["spec"]["template"] != expected or d["spec"].get("replicas", 1) != row["replicas"]:
                raise UpgradeError("验证期间 Deployment 被其他操作修改")
            self.healthy_deployment(d)
            restarts = self.healthy_pods(row, d)
            if baseline is not None and restarts != baseline:
                raise UpgradeError("稳定观察期间 Pod 被替换或容器发生重启")
            baseline = restarts
            self.business_check(row)
            if time.monotonic() >= deadline:
                break
            self.log(f"{ns}/{name} 健康检查通过，继续稳定观察")
            time.sleep(min(self.a.interval, max(0, deadline - time.monotonic())))
        self.log(f"成功 {ns}/{name}: {row['new']}")
        row["state"] = "succeeded"
        self.save_manifest(self.rows)
        self.current = None

    def rollback(self, rollback_dir):
        manifest_path = rollback_dir / "run.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("context") != self.a.context:
            raise UpgradeError(f"回滚目录 context={data.get('context')}，与指定 context 不一致")
        rows = [r for r in data.get("rows", []) if r.get("state") in ("attempted", "succeeded")
                and r.get("old") != r.get("new")]
        if not rows:
            raise UpgradeError("该运行目录没有已尝试的镜像变更")
        # Refuse all mutations if any candidate has drifted or been recreated.
        for row in rows:
            d = self.get(row["ns"], "deployment", row["name"])
            if d["metadata"].get("uid") != row["uid"]:
                raise UpgradeError(f"{row['ns']}/{row['name']} UID 已变化，拒绝回滚")
            containers = d["spec"]["template"]["spec"]["containers"]
            if row["index"] >= len(containers) or containers[row["index"]]["name"] != row["container"]:
                raise UpgradeError(f"{row['ns']}/{row['name']} 容器列表已变化，拒绝回滚")
            image = containers[row["index"]]["image"]
            if image not in (row["old"], row["new"]):
                raise UpgradeError(f"{row['ns']}/{row['name']} 当前镜像 {image} 已漂移，拒绝回滚")
            row["rollback_current"] = image
            row["rollback_resource_version"] = d["metadata"]["resourceVersion"]
        changed = []
        for row in reversed(rows):
            self.current = row
            ns, name = row["ns"], row["name"]
            if row["rollback_current"] != row["old"]:
                patch = [dict(op="test", path="/metadata/resourceVersion", value=row["rollback_resource_version"]),
                         dict(op="test", path=f"/spec/template/spec/containers/{row['index']}/image", value=row["new"]),
                         dict(op="replace", path=f"/spec/template/spec/containers/{row['index']}/image", value=row["old"])]
                self.kubectl("-n", ns, "patch", "deployment", name, "--type=json", "-p", json.dumps(patch))
                self.log(f"已恢复镜像 {ns}/{name}: {row['new']} -> {row['old']}")
                changed.append(row)
            else:
                self.log(f"跳过 {ns}/{name}，当前镜像已是原版本")
            self.current = None
        failures = []
        for row in reversed(rows):
            try:
                self.log(self.kubectl("-n", row["ns"], "rollout", "status", f"deployment/{row['name']}",
                                      f"--timeout={self.a.timeout}s", timeout=self.a.timeout + 30))
            except UpgradeError as exc:
                failures.append(f"{row['ns']}/{row['name']}: {exc}")
                self.log(f"回滚发布验证失败 {row['ns']}/{row['name']}: {exc}")
        if failures:
            raise UpgradeError("镜像已尽可能恢复，但以下发布未完成: " + "; ".join(failures))
        self.log("回滚完成；请另行执行业务健康检查")

    def diagnostics(self):
        if self.current:
            row = self.current
            try:
                self.log(self.kubectl("-n", row["ns"], "get", "deployment", row["name"], "-o", "wide"))
            except UpgradeError as exc:
                self.log(f"诊断获取失败: {exc}")
        for row in self.changed:
            cmd = ["kubectl", "--context", self.a.context, "-n", row["ns"], "set", "image",
                   f"deployment/{row['name']}", f"{row['container']}={row['old']}"]
            self.log("需人工确认的恢复命令（请求失败时也可能已生效）: " + shlex.join(cmd))
            self.log("恢复后验证: " + shlex.join(["kubectl", "--context", self.a.context, "-n", row["ns"],
                     "rollout", "status", f"deployment/{row['name']}", f"--timeout={self.a.timeout}s"]))


def main():
    args = arguments()
    if args.rollback_dir is None and args.file is None:
        raise SystemExit("正常升级必须提供清单文件")
    if args.rollback_dir is not None and args.file is not None:
        raise SystemExit("--rollback-dir 模式不接受升级清单文件")
    if args.rollback_dir is not None and args.execute:
        raise SystemExit("--rollback-dir 已表示执行回滚，不要同时指定 --execute")
    if not args.context:
        raise SystemExit("必须显式指定 --context")
    runner = Runner(args)
    try:
        if args.rollback_dir is not None:
            runner.rollback(args.rollback_dir)
            return 0
        rows = runner.preflight()
        if not args.execute:
            runner.log(f"预检查完成，共 {len(rows)} 项；未修改集群。实际升级需添加 --execute")
            return 0
        runner.log(f"开始串行升级，共 {len(rows)} 项")
        for row in rows:
            runner.upgrade(row)
        runner.log("全部升级成功")
        return 0
    except (UpgradeError, OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
        runner.log(f"停止：{exc or '用户中断'}；后续条目不会执行")
        runner.diagnostics()
        return 1
    finally:
        runner.logfile.close()


if __name__ == "__main__":
    sys.exit(main())
