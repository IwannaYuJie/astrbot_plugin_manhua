# astrbot_plugin_manhua

Generate sequential images from a single prompt, an uploaded reference image, or a replied image.

## Command

- `manhua draw [count] <prompt>`
- `mh draw [count] <prompt>`
- `manhua help`

If the prompt is omitted, upload one image and the plugin will use `image_only_prompt`.

## Workflow

1. If the user uploads or replies to an image, the plugin can treat it as the starting frame.
2. If `show_reference_as_first_frame=true`, that image is returned as frame 1.
3. Every following frame uses the previous frame as the reference image when the edit endpoint is available.
4. The plugin auto-plans a prompt for each frame with an LLM and sends the prompt text together with the image.

## Backend Modes

- `auto`: Try the selected AstrBot provider first, then fall back to plugin-local OpenAI-compatible settings.
- `astrbot_provider`: Reuse API config from a provider already configured in AstrBot.
- `openai_compatible`: Use plugin-local `openai_base_url` + `openai_api_key` + `image_model`.

## Prompt Planning

- `prompt_planner_provider_id`: optional planner provider.
- `use_current_provider_for_prompt_planner`: reuse the current chat provider when planner provider is empty.
- `planner_use_image_context`: send the previous frame image to the planner if the provider supports vision.
- `prompt_output_language`: control the language of the generated prompt text.
