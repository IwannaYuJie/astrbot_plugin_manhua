# AstrBot 连续分镜插件

根据单条提示词、上传的参考图，或回复中的图片，生成连续分镜图片。

## 命令

- `manhua draw [count] <prompt>`
- `mh draw [count] <prompt>`
- `manhua help`

如果不填写 `prompt`，可以直接上传一张图片，插件会使用 `image_only_prompt` 作为默认提示词。

## 工作流程

1. 用户上传图片或回复一张图片时，插件可以将其作为起始画面。
2. 如果 `show_reference_as_first_frame=true`，这张参考图会直接作为第 1 帧返回。
3. 在支持图片编辑接口时，后续每一帧都会使用上一帧作为参考图继续生成。
4. 插件会使用 LLM 自动为每一帧规划提示词，并将提示词与图片一同发送给绘图模型。
5. 如果某一帧因超时、响应格式异常等原因失败，插件会按 `frame_retry_attempts` 设定重试；最终仍失败时跳过该帧并继续后续帧。
6. 插件会复用 HTTP 客户端连接；如果运行时取消任务或重载插件，当前生成流程会尽快响应取消。

## 后端模式

- `auto`：优先使用当前选择的 AstrBot provider，失败后再回退到插件内配置的 OpenAI 兼容接口。
- `astrbot_provider`：复用 AstrBot 中已经配置好的 provider 接口信息。
- `openai_compatible`：使用插件内单独配置的 `openai_base_url`、`openai_api_key` 和 `image_model`。

## 提示词规划

- `prompt_planner_provider_id`：可选的提示词规划 provider。
- `use_current_provider_for_prompt_planner`：当未单独指定规划 provider 时，复用当前会话 provider。
- `planner_use_image_context`：如果 provider 支持视觉能力，则把上一帧图片一并发送给规划器。
- `prompt_output_language`：控制生成提示词文本的输出语言。

## 容错行为

- `frame_retry_attempts`：控制单帧生成失败后的重试次数，默认重试 2 次。
- `temp_file_ttl_seconds`：控制插件临时目录里历史生成图片的保留时长，默认 86400 秒；每次执行命令前会清理过期文件，设为 0 或负数可关闭。
- 规划器返回非纯 JSON 时，插件会从文本中提取首个合法 JSON 对象，避免多段大括号内容导致整帧解析失败。
- 图片编辑接口失败时，会自动回退到仅文本生成，而不是直接中断整段分镜任务。
