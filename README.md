# 墨记 · DPT 文档与笔记助手

面向 Sony Digital Paper 的通用文档工具：按文件夹同步 PDF 与手写批注，结合原文解答笔记，并保留可检索的归档与解读历史。

网页支持新建设备目录与批量移动 PDF；移动前备份、移动后回读校验，并保留原有笔记与解答关联。

## 核心流程

1. 在网页“设备文件”中刷新目录，选择一个或多个同步文件夹并保存。
2. 所选目录及子目录中的 PDF 自动加入同步，后续新放入的文档也会加入。
3. 同步新增或变化的批注，查看原始笔迹、校正转写、解读问题。
4. 范围外的文档不会下载、提取笔记或进入自动解读；已经归档的内容继续保留。

## 运行与部署

需要 Python 3.10+ 和 Poppler 的 `pdftoppm`。模型服务须支持图片输入及 OpenAI 兼容接口。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r app/requirements.txt
cp .env.example .env
# 在 .env 中填写自己的设备地址和部署参数，或启动后在网页配置设备。
.venv/bin/python -m app.server
```

部署到服务器：

```sh
bash scripts/deploy.sh user@your-server
# 或在 .env 中设置 DPT_DEPLOY_HOST 后直接运行：
bash scripts/deploy.sh
```

部署后直接访问脚本输出的局域网网址。设备地址、同步文件夹、模型地址、模型名称、密钥与自动解读开关均可配置。

源码不包含预设设备 IP 或服务器地址。`.env`、配对凭据、文档、数据库和部署私有文件均被 Git 忽略。默认打包不会复制配对密钥；如需迁移本机配对，必须显式使用 `--include-pairing`，并只将生成的私有包传给自己的服务器。

完整说明见 [使用与部署](docs/study-app.md)。历史资料生成脚本保留在仓库中，独立于主程序，见 [旧资料工具说明](docs/legacy-material-tools.md)。

## 验证

```sh
python -m unittest discover -s tests -p 'test_study*.py' -v
node tests/test_study_frontend.cjs
```

## 许可证

本项目使用 [MIT License](LICENSE)。依赖项目各自的许可证保持不变。
