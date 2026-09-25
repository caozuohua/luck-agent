# 一期候选依赖与测试基线

使用 Python 3.12。`requirements.txt` 声明直接依赖范围，`requirements.lock` 固定候选版本及下载哈希。
本次先约束到现有测试通过的环境版本，修正 Lark SDK 与 websockets 的冲突，再在空 venv 验证。
不要把已有开发 venv 的“可运行”当作依赖组合合法证明。

PowerShell 干净环境：

```powershell
uv venv --python 3.12 workspace/phase-one-venv
uv pip sync --python workspace/phase-one-venv/Scripts/python.exe --require-hashes requirements.lock
uv pip check --python workspace/phase-one-venv/Scripts/python.exe
pwsh ./scripts/test-local.ps1 -All -Venv workspace/phase-one-venv
```

Linux 安装候选依赖：

```bash
python3.12 -m venv venv
venv/bin/python -m pip install --require-hashes -r requirements.lock
PYTHONPATH= venv/bin/python -m pytest tests/ -q --junitxml=/tmp/luck-agent-tests.xml
```

此文档不是生产部署命令。生产脏目录需先归档审查，Linux 实测与数据库迁移/回退仍是发布门槛。
Docker 使用相同 lock 文件，构建上下文包含生产 runtime 包，排除环境、数据库和私有运行数据。

保守重生成锁文件：

```powershell
uv pip compile requirements.txt --constraint requirements.lock --python-version 3.12 --generate-hashes --output-file workspace/requirements-next.lock
```

审查依赖差异并在新环境验证后才替换 lock；升级特定包时显式调整约束。
最初生成器注释中的 `workspace/baseline-constraints.txt` 是当时环境版本快照，不是安装 lock 的必要文件。
Windows 与 Linux 的包解析已单独核对；跨平台解析通过不等于 Linux 运行验收通过。
