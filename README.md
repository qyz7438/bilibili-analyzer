# B站视频分析器

根据 B 站视频链接或 UP 主信息获取资料，可选下载音频、转录并调用兼容 OpenAI 接口的语言模型，生成候选笔记或 Obsidian 笔记。本仓库只包含程序，不包含账号数据、视频、字幕、日志、笔记或模型权重。

## 使用

```bash
python -m venv .venv
# 激活虚拟环境后：
python -m pip install -r requirements.txt
python main.py --help
```

复制 `config.example.yaml` 为 `config.yaml`，填写自己的模型接口与输出目录。B 站登录 Cookie 仅在需要时自行配置，`config.yaml` 已加入忽略列表。请求 B 站和语言模型接口需要网络；音频处理还需要 ffmpeg，可选转录需要自行安装 `faster-whisper` 并准备模型。默认示例关闭转录。

```bash
python main.py --config config.yaml --url 'https://www.bilibili.com/video/<BV号>' --no-download
```

候选写入和正式笔记写入是不同模式；请先检查配置中的 `workflow` 和输出路径。B 站接口、可访问的字幕和服务配置会影响结果。

WSL 用户应将 `llm.base_url` 配置为 WSL 中实际可访问的服务地址。默认保留原地址；仅在确认 `/etc/resolv.conf` 的 nameserver 确实是目标宿主地址时，才可通过 `BILIBILI_ANALYZER_LLM_RESOLVE_WSL=1` 启用地址替换。

## 测试

```bash
python -m unittest discover -s tests
```

这些检查不请求真实 B 站视频、语言模型服务或音频模型。

## 许可证

本仓库尚未指定许可证。
